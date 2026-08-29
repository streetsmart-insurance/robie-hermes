"""Chat and Job Engine cannot silently disagree about the loaded SHA.

Pointer-only is the PR 17 hole. These tests fail if a Chat-only dest can
stay stale while both zip pointers name the new SHA.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.deploy_truth import (
    CHAT_RUNTIME_FILES,
    DONE_BANNER,
    FLIP_RECORD_FILENAME,
    INSTALL_PROOF_KIND,
    NOT_DONE_BANNER,
    PROOF_FILENAME,
    USER_OWNED_SKILLS,
    ZIP_LOAD_MARKER,
    ZIP_ONLY_FILES,
    DeployTruthError,
    dest_matches_zip,
    file_md5,
    flip_release_pointers,
    is_pointer_only_live,
    is_zip_load_shim,
    load_recorded_flip,
    official_install,
    parse_timestamp,
    prove_chat_runtime_matches_zip,
    prove_gateway_after_flip,
    prove_official_install,
    resolve_job_engine_root,
    resolve_write_guard_path,
    zip_load_shim_source,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.production_preflight import (
    CHECK_CHAT_RUNTIME,
    check_chat_runtime,
)


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "scripts" / "install-official-release.sh"
DEPLOY_TRUTH = ROOT / "robie_job_engine" / "deploy_truth.py"
CURRENT_STATE = ROOT / "CURRENT_STATE.md"
SHA_OLD = "aaaaaaaaaaaa"
SHA_NEW = "bbbbbbbbbbbb"


def _copy_zip_sources(release_root: Path) -> None:
    for item in CHAT_RUNTIME_FILES:
        src = ROOT / item.zip_relpath
        dest = release_root / item.zip_relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
    (release_root / "robie_job_engine").mkdir(parents=True, exist_ok=True)
    (release_root / "robie_job_engine" / "__init__.py").write_text(
        '"""test zip Job Engine marker"""\nMARKER = "zip-job-engine"\n',
        encoding="utf-8",
    )
    guard = ROOT / "robie_job_engine" / "playwright_write_guard.py"
    (release_root / "robie_job_engine" / "playwright_write_guard.py").write_bytes(
        guard.read_bytes()
    )
    helper = ROOT / "robie_job_engine" / "gemini_field_helper.py"
    (release_root / "robie_job_engine" / "gemini_field_helper.py").write_bytes(
        helper.read_bytes()
    )


def _write_stale_adapter(dest: Path, *, missing_heartbeat: bool = True) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = (
        '"""stale Production .hermes Chat adapter (PR 17 hole)."""\n'
        "ADAPTER_SHA = %r\n" % SHA_OLD
    )
    if missing_heartbeat:
        body += "# heartbeat helper was never called from this dest\n"
    dest.write_text(body, encoding="utf-8")


def _write_stale_tool(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        '"""stale .hermes playwright_tool; no zip-load shim."""\nSTALE = True\n',
        encoding="utf-8",
    )


def _seed_stale_hermes(hermes_home: Path) -> None:
    for item in CHAT_RUNTIME_FILES:
        dest = hermes_home / item.dest_relpath
        if "adapter" in item.name:
            _write_stale_adapter(dest)
        else:
            _write_stale_tool(dest)


def _layout(tmp: str, sha: str = SHA_NEW) -> dict[str, Path]:
    opt = Path(tmp) / "opt"
    release = opt / "releases" / sha / f"robie-hermes-{sha}"
    hermes = opt / ".hermes"
    release.mkdir(parents=True, exist_ok=True)
    hermes.mkdir(parents=True, exist_ok=True)
    _copy_zip_sources(release)
    _seed_stale_hermes(hermes)
    skill = hermes / "skills" / "ascend-finance" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("user-owned Loom ascend-finance — do not overwrite\n", encoding="utf-8")
    return {
        "opt": opt,
        "release": release,
        "hermes": hermes,
        "skill": skill,
    }


def _jobs_db(opt: Path) -> str:
    path = str(opt / "jobs.db")
    JobStore(path)
    return path


def _after_flip(flip_at: datetime | None = None) -> dict[str, object]:
    flipped = flip_at or datetime.now(timezone.utc)
    return {
        "flip_at": flipped,
        "gateway_active_enter": flipped + timedelta(seconds=5),
    }


class ManifestContractTests(unittest.TestCase):
    def test_chat_runtime_manifest_excludes_skills_and_includes_adapter_tools(self):
        names = {item.name for item in CHAT_RUNTIME_FILES}
        rels = " ".join(item.dest_relpath for item in CHAT_RUNTIME_FILES)
        zips = " ".join(item.zip_relpath for item in CHAT_RUNTIME_FILES)
        self.assertIn("google-chat-adapter", names)
        self.assertIn("playwright-tool", names)
        self.assertIn("playwright-write-guard", names)
        self.assertIn("gemini-field-tool", names)
        zip_only = {item.name for item in ZIP_ONLY_FILES}
        self.assertIn("gemini-field-helper", zip_only)
        self.assertNotIn("skills", rels)
        self.assertNotIn("ascend-finance", rels)
        self.assertNotIn("SKILL.md", zips)
        self.assertEqual(USER_OWNED_SKILLS, ("ascend-finance",))

    def test_sources_never_git_pull_or_print_environ_secrets(self):
        for path in (DEPLOY_TRUTH, INSTALL_SCRIPT):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r'(?m)^\s*git\s+pull\b')
            self.assertNotIn('["git", "pull"]', text)
            self.assertNotIn("git pull origin", text)
        module = DEPLOY_TRUTH.read_text(encoding="utf-8")
        self.assertNotIn("os.environ.copy()", module)
        self.assertNotIn("print(os.environ", module)
        self.assertNotIn("access_secret_version", module)
        script = INSTALL_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("python3 -m robie_job_engine.deploy_truth install", script)
        self.assertIn("never runs git pull", script)
        mode = INSTALL_SCRIPT.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_current_state_defines_live_as_pointers_plus_chat_load_path(self):
        text = CURRENT_STATE.read_text(encoding="utf-8")
        self.assertIn("Pointer-only is not live", text)
        self.assertIn("Chat load path equals that zip", text)
        self.assertIn("gateway_progress", text)
        self.assertIn("ActiveEnterTimestamp", text)
        self.assertIn("ascend-finance", text)
        self.assertIn("scripts/install-official-release.sh", text)
        self.assertIn("only supported Production flip", text)
        self.assertIn("install_proof", text)
        self.assertIn("Empty CDP target list", text)
        self.assertIn("destination-verified evidence rows > 0", text)
        self.assertIn("**not** deployed to `hermes-poc-01`", text)
        script = INSTALL_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("ONLY supported Production flip", script)
        self.assertIn("Long typed SSH commands are not the install path", script)


class StaleChatFileFailsTests(unittest.TestCase):
    def test_stale_chat_adapter_is_not_live_when_pointers_match_new_sha(self):
        """PR 17 hole: pointers say the new SHA, Chat dest is still old."""
        with durable_temporary_directory() as tmp:
            paths = _layout(tmp)
            flip_release_pointers(
                opt_root=paths["opt"], release_root=paths["release"]
            )
            proof = prove_official_install(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
                sha=SHA_NEW,
            )
            self.assertTrue(proof["pointers"]["ok"])
            self.assertFalse(proof["chat_runtime"]["ok"])
            self.assertFalse(proof["ok"])
            self.assertFalse(proof["done"])
            self.assertIn("google-chat-adapter", proof["chat_runtime"]["stale"])
            self.assertTrue(
                is_pointer_only_live(
                    opt_root=paths["opt"],
                    release_root=paths["release"],
                    hermes_home=paths["hermes"],
                    sha=SHA_NEW,
                )
            )
            adapter = paths["hermes"] / (
                "hermes-agent/plugins/platforms/google_chat/adapter.py"
            )
            adapter_text = adapter.read_text()
            self.assertNotIn("start_generic_chat_job_heartbeat", adapter_text)
            self.assertNotIn(ZIP_LOAD_MARKER, adapter_text)
            self.assertIn(SHA_OLD, adapter_text)

    def test_stale_playwright_tool_fails_even_if_adapter_was_copied(self):
        with durable_temporary_directory() as tmp:
            paths = _layout(tmp)
            adapter_item = next(
                item for item in CHAT_RUNTIME_FILES if item.name == "google-chat-adapter"
            )
            dest = paths["hermes"] / adapter_item.dest_relpath
            dest.write_bytes((paths["release"] / adapter_item.zip_relpath).read_bytes())
            flip_release_pointers(
                opt_root=paths["opt"], release_root=paths["release"]
            )
            chat = prove_chat_runtime_matches_zip(
                release_root=paths["release"], hermes_home=paths["hermes"]
            )
            self.assertFalse(chat["ok"])
            self.assertIn("playwright-tool", chat["stale"])

    def test_preflight_chat_runtime_is_no_when_adapter_is_stale(self):
        with durable_temporary_directory() as tmp:
            paths = _layout(tmp)
            flip_release_pointers(
                opt_root=paths["opt"], release_root=paths["release"]
            )
            result = check_chat_runtime(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["name"], CHECK_CHAT_RUNTIME)
            self.assertIn("stale", result["evidence"])


class OfficialInstallProofTests(unittest.TestCase):
    def test_test_gateway_name_is_used_in_proof(self):
        flipped = datetime.now(timezone.utc)
        proof = prove_gateway_after_flip(
            flip_at=flipped,
            active_enter=flipped + timedelta(seconds=1),
            gateway_unit="robie-gateway",
        )
        self.assertTrue(proof["ok"])
        self.assertIn("robie-gateway ActiveEnterTimestamp", proof["evidence"])

    def test_official_install_writes_shims_flips_pointers_and_prints_done(self):
        with durable_temporary_directory() as tmp:
            paths = _layout(tmp)
            skill_before = paths["skill"].read_text(encoding="utf-8")
            skill_mtime = paths["skill"].stat().st_mtime_ns
            db = _jobs_db(paths["opt"])
            payload = official_install(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
                sha=SHA_NEW,
                db_path=db,
                **_after_flip(),
            )
            self.assertTrue(payload["done"])
            self.assertTrue(payload["live"])
            self.assertEqual(payload["banner"], DONE_BANNER)
            self.assertFalse(payload["git_pull"])
            self.assertFalse(payload["bind"])
            self.assertFalse(payload["restart_gateway"])
            self.assertFalse(payload["authorizes_complete"])
            proof = prove_official_install(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
                sha=SHA_NEW,
                db_path=db,
                persist_row=False,
                **_after_flip(),
            )
            self.assertTrue(proof["ok"])
            self.assertTrue(proof["live"])
            row = payload["proof"]["install_proof_row"]
            self.assertEqual(row["kind"], INSTALL_PROOF_KIND)
            self.assertNotEqual(row["status"], JobStatus.COMPLETE.value)
            stored = JobStore(db).get_checkpoint(row["job_id"], INSTALL_PROOF_KIND)
            self.assertTrue(stored["live"])
            self.assertFalse(stored["authorizes_complete"])
            self.assertFalse(stored["destination_verified"])
            self.assertTrue(stored["chat_busy_is_not_live"])
            self.assertFalse(
                is_pointer_only_live(
                    opt_root=paths["opt"],
                    release_root=paths["release"],
                    hermes_home=paths["hermes"],
                    sha=SHA_NEW,
                )
            )
            adapter = paths["hermes"] / (
                "hermes-agent/plugins/platforms/google_chat/adapter.py"
            )
            self.assertTrue(
                is_zip_load_shim(
                    adapter.read_text(encoding="utf-8"),
                    "integrations/google_chat/adapter.py",
                )
            )
            self.assertEqual(paths["skill"].read_text(encoding="utf-8"), skill_before)
            self.assertEqual(paths["skill"].stat().st_mtime_ns, skill_mtime)
            self.assertTrue((paths["release"] / PROOF_FILENAME).is_file())
            preflight = check_chat_runtime(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
            )
            self.assertTrue(preflight["ok"])

    def test_bytes_match_copy_also_counts_as_equal_to_zip(self):
        with durable_temporary_directory() as tmp:
            paths = _layout(tmp)
            for item in CHAT_RUNTIME_FILES:
                dest = paths["hermes"] / item.dest_relpath
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((paths["release"] / item.zip_relpath).read_bytes())
            flip_release_pointers(
                opt_root=paths["opt"], release_root=paths["release"]
            )
            chat = prove_chat_runtime_matches_zip(
                release_root=paths["release"], hermes_home=paths["hermes"]
            )
            self.assertTrue(chat["ok"])
            dest_modes = {
                row["mode"] for row in chat["files"] if row.get("dest")
            }
            self.assertEqual(dest_modes, {"bytes-match"})
            self.assertIn("zip-path", {row["mode"] for row in chat["files"]})
            adapter = paths["hermes"] / (
                "hermes-agent/plugins/platforms/google_chat/adapter.py"
            )
            zip_adapter = paths["release"] / "integrations/google_chat/adapter.py"
            self.assertEqual(file_md5(adapter), file_md5(zip_adapter))

    def test_diverged_skill_does_not_fail_chat_runtime_proof(self):
        with durable_temporary_directory() as tmp:
            paths = _layout(tmp)
            official_install(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
                sha=SHA_NEW,
                db_path=_jobs_db(paths["opt"]),
                **_after_flip(),
            )
            paths["skill"].write_text(
                "Carlo edited ascend-finance after the zip\n", encoding="utf-8"
            )
            extra = paths["hermes"] / "skills" / "robie-playwright-browser" / "SKILL.md"
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_text("stale skill text on purpose\n", encoding="utf-8")
            proof = prove_official_install(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
                sha=SHA_NEW,
                persist_row=False,
                **_after_flip(),
            )
            self.assertTrue(proof["ok"])
            self.assertIn("ascend-finance is not proof", proof["chat_runtime"]["skills"])

    def test_install_refuses_gateway_restart_and_skill_dests(self):
        with durable_temporary_directory() as tmp:
            paths = _layout(tmp)
            with self.assertRaisesRegex(DeployTruthError, "restart hermes-gateway"):
                official_install(
                    opt_root=paths["opt"],
                    release_root=paths["release"],
                    hermes_home=paths["hermes"],
                    sha=SHA_NEW,
                    restart_gateway=True,
                )
            with self.assertRaisesRegex(DeployTruthError, "skill"):
                zip_load_shim_source("deploy/hermes/skills/ascend-finance/SKILL.md")

    def test_pointer_and_chat_match_without_gateway_after_flip_is_not_live(self):
        with durable_temporary_directory() as tmp:
            paths = _layout(tmp)
            db = _jobs_db(paths["opt"])
            flipped = datetime.now(timezone.utc)
            with self.assertRaisesRegex(DeployTruthError, "not after flip|ActiveEnterTimestamp missing"):
                official_install(
                    opt_root=paths["opt"],
                    release_root=paths["release"],
                    hermes_home=paths["hermes"],
                    sha=SHA_NEW,
                    db_path=db,
                    flip_at=flipped,
                    gateway_active_enter=flipped - timedelta(seconds=30),
                )
            proof = prove_official_install(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
                sha=SHA_NEW,
                flip_at=flipped,
                gateway_active_enter=flipped - timedelta(seconds=30),
                persist_row=False,
            )
            self.assertTrue(proof["pointers"]["ok"])
            self.assertTrue(proof["chat_runtime"]["ok"])
            self.assertFalse(proof["live"])
            self.assertFalse(proof["ok"])
            self.assertFalse(proof["authorizes_complete"])
            recorded = load_recorded_flip(
                opt_root=paths["opt"], release_root=paths["release"]
            )
            self.assertIsNotNone(recorded)
            self.assertEqual(
                parse_timestamp(recorded["flip_at"]), parse_timestamp(flipped)
            )
            self.assertTrue((paths["release"] / FLIP_RECORD_FILENAME).is_file())
            self.assertTrue((paths["release"] / PROOF_FILENAME).is_file())
            unfinished = prove_official_install(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
                sha=SHA_NEW,
                flip_at=flipped,
                gateway_active_enter=flipped - timedelta(seconds=30),
                db_path=db,
                persist_row=True,
            )
            self.assertFalse(unfinished["live"])
            self.assertIsNotNone(unfinished["install_proof_row"])
            self.assertFalse(unfinished["install_proof_row"]["data"]["live"])
            self.assertNotEqual(
                unfinished["install_proof_row"]["status"], JobStatus.COMPLETE.value
            )
            later = prove_official_install(
                opt_root=paths["opt"],
                release_root=paths["release"],
                hermes_home=paths["hermes"],
                sha=SHA_NEW,
                gateway_active_enter=flipped + timedelta(seconds=8),
                db_path=db,
                persist_row=True,
            )
            self.assertTrue(later["live"])
            self.assertTrue(later["ok"])
            self.assertIsNotNone(later["install_proof_row"])
            self.assertNotEqual(
                later["install_proof_row"]["status"], JobStatus.COMPLETE.value
            )

            proved = subprocess.run(
                [
                    "python3",
                    "-m",
                    "robie_job_engine.deploy_truth",
                    "prove",
                    "--opt-root",
                    str(paths["opt"]),
                    "--release-root",
                    str(paths["release"]),
                    "--hermes-home",
                    str(paths["hermes"]),
                    "--sha",
                    SHA_NEW,
                    "--db",
                    db,
                    "--gateway-active-enter",
                    (flipped + timedelta(seconds=8)).isoformat(),
                ],
                cwd=str(ROOT),
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proved.returncode, 0, proved.stderr)
            refreshed = json.loads(
                (paths["release"] / PROOF_FILENAME).read_text(encoding="utf-8")
            )
            self.assertTrue(refreshed["done"])
            self.assertTrue(refreshed["live"])
            self.assertTrue(refreshed["proof"]["live"])
            self.assertEqual(refreshed["sha"], SHA_NEW)
            self.assertIn("verified_at", refreshed)
            self.assertFalse(refreshed["authorizes_complete"])
            self.assertIn("overlays", refreshed)

    def test_cli_install_is_not_done_until_proof(self):
        with durable_temporary_directory() as tmp:
            paths = _layout(tmp)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            prove = subprocess.run(
                [
                    "python3",
                    "-m",
                    "robie_job_engine.deploy_truth",
                    "prove",
                    "--opt-root",
                    str(paths["opt"]),
                    "--release-root",
                    str(paths["release"]),
                    "--hermes-home",
                    str(paths["hermes"]),
                    "--sha",
                    SHA_NEW,
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(prove.returncode, 2)
            self.assertIn(NOT_DONE_BANNER, prove.stdout)
            flipped = datetime.now(timezone.utc)
            entered = flipped + timedelta(seconds=5)
            db = _jobs_db(paths["opt"])
            installed = subprocess.run(
                [
                    "bash",
                    str(INSTALL_SCRIPT),
                    "install",
                    "--opt-root",
                    str(paths["opt"]),
                    "--release-root",
                    str(paths["release"]),
                    "--hermes-home",
                    str(paths["hermes"]),
                    "--sha",
                    SHA_NEW,
                    "--db",
                    db,
                    "--flip-at",
                    flipped.isoformat(),
                    "--gateway-active-enter",
                    entered.isoformat(),
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertIn(DONE_BANNER, installed.stdout)
            self.assertNotIn("git pull", installed.stdout)


class ZipLoadAndPathTests(unittest.TestCase):
    def test_shim_execs_zip_file_not_the_stale_dest_body(self):
        with durable_temporary_directory() as tmp:
            zip_root = Path(tmp) / "zip"
            dest = Path(tmp) / "adapter.py"
            src = zip_root / "integrations" / "google_chat" / "adapter.py"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("LOADED_FROM = 'zip'\n", encoding="utf-8")
            dest.write_text(zip_load_shim_source("integrations/google_chat/adapter.py"))
            env = os.environ.copy()
            env["ROBIE_CANONICAL_JOB_ENGINE_ROOT"] = str(zip_root)
            ns: dict[str, object] = {"__name__": "shim_test"}
            compiled = compile(dest.read_text(encoding="utf-8"), str(dest), "exec")
            old = os.environ.get("ROBIE_CANONICAL_JOB_ENGINE_ROOT")
            os.environ["ROBIE_CANONICAL_JOB_ENGINE_ROOT"] = str(zip_root)
            try:
                exec(compiled, ns)
            finally:
                if old is None:
                    os.environ.pop("ROBIE_CANONICAL_JOB_ENGINE_ROOT", None)
                else:
                    os.environ["ROBIE_CANONICAL_JOB_ENGINE_ROOT"] = old
            self.assertEqual(ns.get("LOADED_FROM"), "zip")
            self.assertEqual(ns.get("__file__"), str(src))

    def test_write_guard_prefers_zip_over_stale_hermes_sibling(self):
        with durable_temporary_directory() as tmp:
            zip_root = Path(tmp) / "zip"
            hermes_tool = Path(tmp) / "hermes" / "playwright_tool.py"
            hermes_guard = hermes_tool.with_name("playwright_write_guard.py")
            zip_guard = zip_root / "robie_job_engine" / "playwright_write_guard.py"
            zip_guard.parent.mkdir(parents=True, exist_ok=True)
            hermes_tool.parent.mkdir(parents=True, exist_ok=True)
            (zip_root / "robie_job_engine" / "__init__.py").write_text(
                "", encoding="utf-8"
            )
            zip_guard.write_text("ZIP_GUARD = True\n", encoding="utf-8")
            hermes_guard.write_text("STALE_GUARD = True\n", encoding="utf-8")
            hermes_tool.write_text("# tool\n", encoding="utf-8")
            found = resolve_write_guard_path(
                env={"ROBIE_CANONICAL_JOB_ENGINE_ROOT": str(zip_root)},
                here=hermes_tool,
            )
            self.assertEqual(found.resolve(), zip_guard.resolve())
            self.assertIn("ZIP_GUARD", found.read_text(encoding="utf-8"))
            self.assertNotIn("STALE_GUARD", found.read_text(encoding="utf-8"))
            root = resolve_job_engine_root(
                env={"ROBIE_CANONICAL_JOB_ENGINE_ROOT": str(zip_root)},
                here=hermes_tool,
            )
            self.assertEqual(root, zip_root)

    def test_dest_matches_zip_reports_inode_and_md5(self):
        with durable_temporary_directory() as tmp:
            zip_file = Path(tmp) / "adapter.py"
            dest = Path(tmp) / "dest.py"
            zip_file.write_text("from-zip\n", encoding="utf-8")
            dest.write_text("stale\n", encoding="utf-8")
            row = dest_matches_zip(dest, zip_file, "integrations/google_chat/adapter.py")
            self.assertFalse(row["ok"])
            self.assertEqual(row["mode"], "stale")
            self.assertIsInstance(row["inode"], int)
            dest.write_text(zip_file.read_text(encoding="utf-8"), encoding="utf-8")
            row = dest_matches_zip(dest, zip_file, "integrations/google_chat/adapter.py")
            self.assertTrue(row["ok"])
            self.assertEqual(row["mode"], "bytes-match")


class CompleteLawAndBootstrapTests(unittest.TestCase):
    def test_live_proof_never_authorizes_complete_or_reads_secrets(self):
        module = DEPLOY_TRUTH.read_text(encoding="utf-8")
        self.assertIn('"authorizes_complete": False', module)
        self.assertIn("destination_evidence_required_for_complete", module)
        self.assertNotIn("access_secret_version", module)
        from robie_job_engine.release_gate import production_release_decision

        self.assertEqual(production_release_decision(), "FAIL")

    def test_bootstrap_opens_one_page_or_prints_inconclusive(self):
        bootstrap = (ROOT / "ezlynx_login_bootstrap.py").read_text(encoding="utf-8")
        self.assertIn('print("INCONCLUSIVE")', bootstrap)
        self.assertIn("new_page", bootstrap)
        self.assertIn("session is not fine", bootstrap)
        self.assertNotIn("session is fine", bootstrap)
        self.assertNotIn("systemctl restart", bootstrap)
        self.assertNotIn("print(secret(", bootstrap)


if __name__ == "__main__":
    unittest.main()
