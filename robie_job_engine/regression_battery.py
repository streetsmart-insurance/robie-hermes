"""PR + post-deploy regression battery. Previously seen failures only.

This battery only guarantees previously seen failures have not come back.
Simulator passed means nothing we have already seen is wrong, never that
nothing is wrong. A Test all-clear is never a Production all-clear.
Every Production NEW failure mode gets a named deterministic scenario
before the incident is closed. That is how the simulator grows.

Detection is automatic. Merge, Production flip, and hermes-gateway restart
stay human-gated (Jake Approve / Carlo Confirm). GitHub-hosted runners never
drive live EZLynx. Replay refuses Production env and live Hermes job-db paths.

A NEW fail (not a known-accepted Test replay outcome) posts to the Robie
Chat space as the Chat APP and may open a draft PR. It does not @robie,
bind, email the insured, merge, or deploy. INCONCLUSIVE gaps are never green.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from .quote_replay import (
    is_live_hermes_path,
    refuse_production_targets,
    run_quote_replay,
)
from .regression_scenarios import (
    ACTION_GATE_CHAT,
    ASCEND_ACCESSIBLE_NAME_CHAT,
    ASCEND_AGENCY_FEE_CHAT,
    ASCEND_AUDIT_CHAT,
    ASCEND_CUSTOMER_TYPE_CHAT,
    ASCEND_LISTBOX_CHAT,
    ASCEND_ROLES_CHAT,
    ASCEND_SPINNER_CHAT,
    ASCEND_TOO_SOON_CHAT,
    CONCAT_PATH_CHAT,
    FALSE_SUCCESS_CHAT,
    FOLLOW_TAB_CHAT,
    HITL_RESUME_CHAT,
    HITL_TONE_CHAT,
    PLAYWRIGHT_CDP_CHAT,
    PLAYWRIGHT_SILENT_CHAT,
    UNVERIFIED_UNMASK_CHAT,
    run_named_scenarios,
)
from .runtime_env import PRODUCTION_ENV_NAMES, ProductionGuardError, current_robie_env


REPO_ROOT = Path(__file__).resolve().parents[1]
PARITY_PATH = REPO_ROOT / "deploy" / "regression_battery" / "parity.json"
DEFAULT_CHAT_SPACE = "spaces/AAQAZbLJO78"
SCOPE = (
    "This battery only guarantees previously seen failures have not come back. "
    "Simulator passed means nothing we have already seen is wrong, never that "
    "nothing is wrong. Known scenarios did not regress."
)
SEEN_CLEAR_TEXT = (
    "previously seen failures have not come back on this runner "
    "(not a Production all-clear)"
)
HUMAN_GATE = (
    "Owners: Carlo Ferrara (StreetSmart) and Jake (StreetSmartJake). "
    "Jake Approves, Carlo Confirms, Dusty pings if it sits. "
    "Do not merge from this hook. Do not flip Production. "
    "Do not restart hermes-gateway."
)
SIGNATURE_MARKER = "regression-signature:"
FLAKY_EVIDENCE = (
    "timeout",
    "timed out",
    "network",
    "connection reset",
    "connection refused",
    "temporary failure",
    "eagain",
    "unavailable",
)
ISOLATED_UNSET = (
    "ROBIE_ENV",
    "ROBIE_JOB_DB",
    "ROBIE_ARTIFACT_ROOT",
    "ROBIE_BROWSER_CDP_URL",
    "ROBIE_QUOTE_PDF",
    "ROBIE_SKILL_SYNC_ROOT",
)
LIVE_PRODUCTION_JOB_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
LIVE_PRODUCTION_ARTIFACTS = "/opt/streetsmart-hermes/robie-job-engine/data/artifacts"
TEST_JOB_DB = "/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db"
TEST_ARTIFACTS = "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts"

LOGIC_JOB_TYPE_GATE = (
    "scripts/check-job-type-gate.py",
    "check",
)
LOGIC_UNITTEST = ("-m", "unittest", "discover", "-s", "tests", "-v")
LOGIC_PYTEST_MODULES = (
    "tests/test_carrier_directory.py",
    "tests/test_ezlynx_poller.py",
    "tests/test_magellan_client.py",
    "tests/test_playwright_route_fixtures.py",
    "tests/test_productivity_engine.py",
    "tests/test_video_to_skill.py",
)
PYTEST_ONLY_MODULES = frozenset(
    Path(item).stem for item in LOGIC_PYTEST_MODULES
)

# Replay outcomes that are accepted on Test / isolated runners. A different
# outcome for the same id is a NEW fail and must Chat + draft.
KNOWN_ACCEPTED_REPLAY = {
    "quote-replay:missing-pdf": frozenset({"BLOCKED"}),
    "quote-replay:production-env": frozenset({"REFUSED"}),
    "quote-replay:live-hermes-job-db": frozenset({"REFUSED"}),
    "quote-replay:test-paths-require-test-env": frozenset({"REFUSED"}),
}
HEALTHY_OUTCOMES = frozenset({"HEALTHY"})
INCONCLUSIVE_OUTCOME = "INCONCLUSIVE"


def _chat_space() -> str:
    for key in (
        "ROBIE_REGRESSION_CHAT_SPACE",
        "ROBIE_PREFLIGHT_CHAT_SPACE",
        "ROBIE_OPS_CHAT_SPACE",
        "GOOGLE_CHAT_HOME_CHANNEL",
    ):
        value = os.environ.get(key, "").strip()
        if value.startswith("spaces/"):
            return value
    return DEFAULT_CHAT_SPACE


def load_parity_catalog(path: Path | None = None) -> dict[str, Any]:
    """Living Test/Production diff list. Update when deploy or HITL shows drift."""
    target = Path(path or PARITY_PATH)
    return json.loads(target.read_text(encoding="utf-8"))


def parity_gap_results(
    catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Known permission/secret/browser gaps. INCONCLUSIVE, never green."""
    data = catalog if catalog is not None else load_parity_catalog()
    results: list[dict[str, Any]] = []
    for row in data.get("diffs") or []:
        gap_id = str(row.get("id") or "").strip()
        if not gap_id:
            continue
        evidence = str(row.get("cannot_prove_on_test") or row.get("test") or "")
        results.append(
            {
                "id": gap_id,
                "kind": "parity",
                "ok": False,
                "outcome": INCONCLUSIVE_OUTCOME,
                "evidence": evidence,
                "gap": row.get("gap"),
            }
        )
    return results


def destroyed_latest_with_older_enabled_is_healthy(
    *,
    newest_state: str,
    newest_enabled_version: str | None,
    alert: bool,
) -> bool:
    """DESTROYED Secret Manager latest + older ENABLED is HEALTHY. Do not alert."""
    return (
        str(newest_state or "").upper() == "DESTROYED"
        and bool(str(newest_enabled_version or "").strip())
        and not alert
    )


def run_secret_health_scenario() -> dict[str, Any]:
    """Deterministic leftover-DESTROYED case. Never reads Secret Manager payloads."""
    from types import SimpleNamespace

    from .login_secret_health import summarize_secret_versions

    versions = [
        SimpleNamespace(name="secrets/ezlynx-password/versions/1", state="ENABLED", create_time=1.0),
        SimpleNamespace(name="secrets/ezlynx-password/versions/2", state="DESTROYED", create_time=2.0),
    ]
    summary = summarize_secret_versions(versions, secret_id="ezlynx-password")
    healthy = destroyed_latest_with_older_enabled_is_healthy(
        newest_state=str(summary.get("newest_state") or ""),
        newest_enabled_version=summary.get("newest_enabled_version"),
        alert=bool(summary.get("alert")),
    )
    return {
        "id": "login-secret:destroyed-latest-enabled-older",
        "kind": "replay",
        "ok": healthy,
        "outcome": "HEALTHY" if healthy else "ALERT",
        "evidence": (
            f"ENABLED {summary.get('newest_enabled_version')}; "
            f"{summary.get('newest_version')} is DESTROYED leftover"
        ),
    }


def normalize_evidence(evidence: str) -> str:
    text = str(evidence or "").strip().splitlines()[0] if evidence else ""
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*", "<ts>", text)
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{3,}\b", "<n>", text)
    return " ".join(text.casefold().split())


def evidence_is_flaky(evidence: str) -> bool:
    folded = str(evidence or "").casefold()
    return any(token in folded for token in FLAKY_EVIDENCE)


def failure_signature(item: dict[str, Any]) -> str:
    """Stable scenario id + outcome + normalized evidence. Required before draft."""
    payload = "|".join(
        (
            str(item.get("id") or "").strip(),
            str(item.get("outcome") or "").strip(),
            normalize_evidence(str(item.get("evidence") or "")),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def default_work_dir() -> Path:
    raw = os.environ.get("ROBIE_REGRESSION_WORK_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    candidate = REPO_ROOT / ".robie-durable-test" / "regression-battery"
    if is_live_hermes_path(candidate):
        return Path("/var/lib/robie-regression-battery")
    return candidate


def isolated_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Drop Production job-db / env so the suite cannot inherit live paths."""
    env = dict(base if base is not None else os.environ)
    for key in ISOLATED_UNSET:
        env.pop(key, None)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        (":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else ""
    )
    return env


def logic_job_type_argv(python: str | None = None) -> list[str]:
    exe = python or sys.executable
    return [exe, str(REPO_ROOT / LOGIC_JOB_TYPE_GATE[0]), LOGIC_JOB_TYPE_GATE[1]]


def logic_unittest_argv(python: str | None = None) -> list[str]:
    exe = python or sys.executable
    return [exe, *LOGIC_UNITTEST]


def logic_pytest_argv(python: str | None = None) -> list[str]:
    exe = python or sys.executable
    return [exe, "-m", "pytest", *LOGIC_PYTEST_MODULES, "-q"]


def pytest_available() -> bool:
    # The suite invokes ``sys.executable -m pytest``.  A pytest console script
    # on PATH may belong to a different interpreter (as it does on the Test
    # VM), so PATH presence alone cannot make the module runnable.
    return _can_import_pytest()


def _can_import_pytest() -> bool:
    try:
        import pytest  # noqa: F401

        return True
    except ImportError:
        return False


def _run_step(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]],
    name: str,
) -> dict[str, Any]:
    proc = runner(
        argv,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    output = (proc.stdout or "") + ("\n" if proc.stderr else "") + (proc.stderr or "")
    ok = int(proc.returncode) == 0
    return {
        "id": name,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "returncode": int(proc.returncode),
        "evidence": output[-4000:],
    }


def discover_unittest_suite(
    tests_dir: Path,
    *,
    include_pytest_modules: bool,
    top_level_dir: Path | None = None,
) -> Any:
    """Load test_*.py. Skip pytest-only modules when pytest is not installed."""
    import unittest

    tests_dir = Path(tests_dir)
    for entry in (str(tests_dir), str(tests_dir.parent)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    extra: dict[str, str] = {}
    if top_level_dir is not None:
        extra["top_level_dir"] = str(top_level_dir)
    for path in sorted(tests_dir.glob("test_*.py")):
        if not include_pytest_modules and path.stem in PYTEST_ONLY_MODULES:
            continue
        suite.addTests(
            loader.discover(str(tests_dir), pattern=path.name, **extra)
        )
    return suite


def run_unittest_skipping_pytest(*, repo_root: Path | None = None) -> int:
    import unittest

    root = Path(repo_root or REPO_ROOT)
    tests_dir = root / "tests"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    suite = discover_unittest_suite(tests_dir, include_pytest_modules=False)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def run_logic_suite(
    *,
    repo_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    python: str | None = None,
    include_pytest: bool | None = None,
) -> list[dict[str, Any]]:
    """Job-type gate + unittest discover + Phase 3 pytest (if present)."""
    root = Path(repo_root or REPO_ROOT)
    run = runner or subprocess.run
    env = isolated_env()
    # Logic tests are synthetic and may execute from an extracted release
    # beneath either live installation tree.  Mark the subprocess as TEST so
    # durable-path guards permit only the isolated fixtures they create; test
    # cases that exercise Production refusal set PRODUCTION explicitly.
    env["ROBIE_ENV"] = "TEST"
    env["ROBIE_SKILL_SYNC_ROOT"] = str(
        root / ".robie-durable-test" / "regression-skill-sync-empty"
    )
    env["PYTHONPATH"] = str(root) + (
        (":" + os.environ["PYTHONPATH"]) if os.environ.get("PYTHONPATH") else ""
    )
    runtime_only = os.environ.get("ROBIE_RUNTIME_ONLY_RELEASE") == "1"
    want_pytest = (
        False
        if runtime_only
        else pytest_available() if include_pytest is None else include_pytest
    )
    completed = [
        _run_step(
            logic_job_type_argv(python),
            cwd=root,
            env=env,
            runner=run,
            name="job-type-gate",
        )
    ]
    if runtime_only:
        return completed
    if want_pytest:
        completed.append(
            _run_step(
                logic_unittest_argv(python),
                cwd=root,
                env=env,
                runner=run,
                name="unittest-discover",
            )
        )
        completed.append(
            _run_step(
                logic_pytest_argv(python),
                cwd=root,
                env=env,
                runner=run,
                name="pytest-phase3",
            )
        )
        return completed
    # Production / verify hosts may lack pytest. Do not import those modules.
    exe = python or sys.executable
    skip_argv = [
        exe,
        "-c",
        "from robie_job_engine.regression_battery import run_unittest_skipping_pytest; "
        "raise SystemExit(run_unittest_skipping_pytest())",
    ]
    completed.append(
        _run_step(
            skip_argv,
            cwd=root,
            env=env,
            runner=run,
            name="unittest-discover",
        )
    )
    return completed


def _refuse_outcome(exc: BaseException) -> str:
    if isinstance(exc, ProductionGuardError):
        return "REFUSED"
    return f"ERROR:{type(exc).__name__}"


def run_replay_scenarios(
    *,
    work_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """In-process Test replay / known-failure scenarios. No live EZLynx."""
    base = Path(work_dir or default_work_dir())
    if is_live_hermes_path(base):
        raise ProductionGuardError(
            f"refusing regression replay work dir on live Hermes path: {base}"
        )
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Scenario databases contain deterministic identifiers. Reusing them makes
    # a second verifier run collide with the first run's conversation links,
    # recording states, and artifact paths. Keep the durable base (including
    # notification history) but isolate every replay execution beneath it.
    root = Path(tempfile.mkdtemp(prefix="run-", dir=str(base)))
    db = str(root / "jobs.db")
    artifacts = str(root / "artifacts")
    results: list[dict[str, Any]] = []

    with patch.dict(os.environ, {}, clear=False):
        for key in ISOLATED_UNSET:
            os.environ.pop(key, None)
        # This is a synthetic Test-only replay.  The durable verifier root may
        # itself live under /opt/streetsmart-hermes-test, whose path guard
        # correctly requires an explicit TEST environment.
        os.environ["ROBIE_ENV"] = "TEST"
        missing = run_quote_replay(
            quote_pdf=None, db_path=db, artifact_root=artifacts
        )
    results.append(
        {
            "id": "quote-replay:missing-pdf",
            "kind": "replay",
            "ok": missing.get("outcome") == "BLOCKED",
            "outcome": str(missing.get("outcome") or "UNKNOWN"),
            "evidence": str(missing.get("reason") or ""),
        }
    )

    def _guard(scenario_id: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            outcome = "UNEXPECTED"
            evidence = "guard did not refuse"
        except ProductionGuardError as exc:
            outcome = "REFUSED"
            evidence = str(exc)
        except Exception as exc:  # noqa: BLE001 — classify, do not swallow into PASS
            outcome = _refuse_outcome(exc)
            evidence = str(exc)
        results.append(
            {
                "id": scenario_id,
                "kind": "replay",
                "ok": outcome in KNOWN_ACCEPTED_REPLAY.get(scenario_id, ()),
                "outcome": outcome,
                "evidence": evidence,
            }
        )

    def production_env() -> None:
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            refuse_production_targets(db, artifacts)

    def live_hermes_job_db() -> None:
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            refuse_production_targets(LIVE_PRODUCTION_JOB_DB, artifacts)

    def test_paths_require_test_env() -> None:
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            refuse_production_targets(TEST_JOB_DB, TEST_ARTIFACTS)

    _guard("quote-replay:production-env", production_env)
    _guard("quote-replay:live-hermes-job-db", live_hermes_job_db)
    _guard("quote-replay:test-paths-require-test-env", test_paths_require_test_env)
    results.append(run_secret_health_scenario())
    # Every named scenario is part of the same synthetic Test replay. Keep the
    # explicit environment through those scenarios as well; otherwise bounded
    # Test wiring correctly fails closed after the quote-only context exits.
    with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
        results.extend(run_named_scenarios(work_dir=root / "named"))
    return results


def classify_results(
    results: list[dict[str, Any]],
    *,
    known_accepted: dict[str, frozenset[str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    accepted = known_accepted if known_accepted is not None else KNOWN_ACCEPTED_REPLAY
    new_failures: list[dict[str, Any]] = []
    known: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    inconclusive: list[dict[str, Any]] = []
    healthy: list[dict[str, Any]] = []
    for item in results:
        outcome = str(item.get("outcome") or "")
        allowed = accepted.get(str(item.get("id") or ""), frozenset())
        if outcome == INCONCLUSIVE_OUTCOME:
            inconclusive.append(item)
            continue
        if outcome in HEALTHY_OUTCOMES or (
            str(item.get("id") or "").startswith("login-secret:")
            and item.get("ok")
        ):
            healthy.append(item)
            passed.append(item)
            continue
        if item.get("ok") and (not allowed or outcome in allowed):
            passed.append(item)
            continue
        if outcome in allowed:
            known.append(item)
            continue
        new_failures.append(item)
    if new_failures:
        verdict = "NEW_FAIL"
    elif inconclusive:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "SEEN_CLEAR"
    return {
        "new_failures": new_failures,
        "known_accepted": known,
        "passed": passed,
        "inconclusive": inconclusive,
        "healthy": healthy,
        "verdict": verdict,
    }


def format_new_failure_chat(
    new_failures: list[dict[str, Any]],
    *,
    trigger: str,
) -> str:
    """Short Chat APP post. Never @robie. Never a Production all-clear."""
    lines = [
        "ROBIE regression battery — NEW fail",
        f"trigger: {trigger}",
        SCOPE,
    ]
    for item in new_failures:
        evidence = str(item.get("evidence") or "no evidence").strip().splitlines()
        snippet = evidence[0] if evidence else "no evidence"
        sig = failure_signature(item)
        lines.append(
            f"- {item.get('id')}: {item.get('outcome')} ({snippet[:240]}) "
            f"{SIGNATURE_MARKER}{sig}"
        )
        item_id = str(item.get("id") or "")
        if item_id.startswith("false-success:"):
            lines.append(FALSE_SUCCESS_CHAT)
        if item_id.startswith("hitl-resume:"):
            lines.append(HITL_RESUME_CHAT)
        if item_id.startswith("recording:follow-live-playwright-tab"):
            lines.append(FOLLOW_TAB_CHAT)
        if item_id.startswith("ascend:"):
            lines.append(ASCEND_AUDIT_CHAT)
        if item_id.startswith("artifact-path:"):
            lines.append(CONCAT_PATH_CHAT)
        if item_id.startswith("hitl-tone:"):
            lines.append(HITL_TONE_CHAT)
        if item_id.startswith("ascend-roles:"):
            lines.append(ASCEND_ROLES_CHAT)
        if item_id.startswith("ascend-new-program:"):
            if "accessible-name" in item_id:
                lines.append(ASCEND_ACCESSIBLE_NAME_CHAT)
            else:
                lines.append(ASCEND_SPINNER_CHAT)
        if item_id.startswith("ascend-create:"):
            if "listbox" in item_id or "unique-listbox" in item_id:
                lines.append(ASCEND_LISTBOX_CHAT)
            elif "too-soon" in item_id or "zero-element" in item_id:
                lines.append(ASCEND_TOO_SOON_CHAT)
            else:
                lines.append(ASCEND_AGENCY_FEE_CHAT)
        if item_id.startswith("ascend-customer-type:"):
            lines.append(ASCEND_CUSTOMER_TYPE_CHAT)
        if item_id.startswith("action-gate:"):
            lines.append(ACTION_GATE_CHAT)
        if item_id.startswith("playwright-silent:"):
            lines.append(PLAYWRIGHT_SILENT_CHAT)
        if item_id.startswith("playwright-cdp:"):
            lines.append(PLAYWRIGHT_CDP_CHAT)
        if item_id.startswith("unverified-unmask:"):
            lines.append(UNVERIFIED_UNMASK_CHAT)
    lines.append(HUMAN_GATE)
    text = "\n".join(lines)
    if "@robie" in text.casefold():
        raise ValueError("regression Chat must not @robie")
    if "production all-clear" in text.casefold() and "not a production all-clear" not in text.casefold():
        raise ValueError("regression Chat must not claim a Production all-clear")
    return text


def format_inconclusive_chat(
    gaps: list[dict[str, Any]],
    *,
    trigger: str,
) -> str:
    """One note that a gap is INCONCLUSIVE. Never green. Never @robie."""
    lines = [
        "ROBIE regression battery — INCONCLUSIVE",
        f"trigger: {trigger}",
        "Not a Production all-clear. Not green.",
        SCOPE,
    ]
    for item in gaps:
        lines.append(
            f"- {item.get('id')}: cannot prove {item.get('evidence')}"
        )
    text = "\n".join(lines)
    if "@robie" in text.casefold():
        raise ValueError("regression Chat must not @robie")
    return text


def _post_new_failure(
    text: str,
    *,
    poster: Callable[..., Any] | None,
) -> bool:
    if "@robie" in text.casefold():
        raise ValueError("regression Chat must not @robie")
    space = _chat_space()
    if not space.startswith("spaces/"):
        return False
    send = poster
    if send is None:
        from .chat_app_post import post_as_chat_app

        send = post_as_chat_app
    send(space, text)
    return True


def build_draft_pr_argv(*, title: str, body: str) -> list[str]:
    """gh pr create --draft only. Never merge. Never deploy."""
    return [
        "gh",
        "pr",
        "create",
        "--draft",
        "--title",
        title,
        "--body",
        body,
    ]


def assert_draft_argv_safe(argv: list[str]) -> list[str]:
    """Inspect command tokens only. Title/body may mention merge as a human gate."""
    if list(argv[:3]) != ["gh", "pr", "create"]:
        raise ValueError("draft PR hook must use gh pr create")
    if "--draft" not in argv:
        raise ValueError("draft PR hook must pass --draft")
    skip_value = False
    command_tokens: list[str] = []
    for index, part in enumerate(argv):
        if skip_value:
            skip_value = False
            continue
        if part in {"--title", "--body", "--head", "--base", "--label"}:
            skip_value = True
            command_tokens.append(part)
            continue
        if index >= 3 and not str(part).startswith("-"):
            continue
        command_tokens.append(part)
    folded = " ".join(command_tokens).casefold()
    if "--merge" in command_tokens or " merge " in f" {folded} ":
        raise ValueError("draft PR hook cannot merge")
    for flag in command_tokens:
        if not str(flag).startswith("-"):
            continue
        text = flag.casefold()
        if any(token in text for token in ("merge", "deploy", "restart")):
            raise ValueError(f"draft PR hook forbids flag {flag}")
    return argv


def draftable_failures(new_failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only deterministic NEW fails with a stable scenario id + signature."""
    ready: list[dict[str, Any]] = []
    for item in new_failures:
        if not str(item.get("id") or "").strip():
            continue
        if evidence_is_flaky(str(item.get("evidence") or "")):
            continue
        if not failure_signature(item):
            continue
        ready.append(item)
    return ready


def draft_pr_payload(
    new_failures: list[dict[str, Any]],
    *,
    trigger: str,
) -> dict[str, str]:
    signatures = [failure_signature(item) for item in new_failures]
    title = "ROBIE regression: NEW fail (do not merge)"
    body = (
        format_new_failure_chat(new_failures, trigger=trigger)
        + "\n\nThis draft is detection only. "
        + HUMAN_GATE
        + "\n"
        + SCOPE
        + "\n"
        + " ".join(f"{SIGNATURE_MARKER}{sig}" for sig in signatures)
    )
    if "@robie" in body.casefold():
        raise ValueError("draft PR body must not @robie")
    return {
        "title": title,
        "body": body,
        "draft": "true",
        "signature": signatures[0] if signatures else "",
        "signatures": ",".join(signatures),
    }


def find_open_draft_for_signature(
    signature: str,
    *,
    finder: Callable[[str], dict[str, Any] | None] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any] | None:
    if not signature:
        return None
    if finder is not None:
        return finder(signature)
    if shutil.which("gh") is None:
        return None
    run = runner or subprocess.run
    query = f"{SIGNATURE_MARKER}{signature} draft:true state:open"
    proc = run(
        ["gh", "pr", "list", "--search", query, "--json", "number,title,url"],
        check=False,
        capture_output=True,
        text=True,
    )
    if int(proc.returncode) != 0:
        return None
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not rows:
        return None
    return dict(rows[0])


def comment_on_existing_draft(
    existing: dict[str, Any],
    *,
    body: str,
    commenter: Callable[[dict[str, Any], str], Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    if commenter is not None:
        result = commenter(existing, body)
        return {"commented": True, "opened": False, "draft": True, "result": result}
    number = str(existing.get("number") or "").strip()
    if not number:
        return {"commented": False, "opened": False, "draft": True, "skipped": "no PR number"}
    argv = ["gh", "pr", "comment", number, "--body", body]
    if "merge" in argv[:4]:
        raise ValueError("comment hook cannot merge")
    if shutil.which("gh") is None:
        return {"commented": False, "opened": False, "draft": True, "skipped": "gh not available", "argv": argv}
    run = runner or subprocess.run
    proc = run(argv, check=False, capture_output=True, text=True)
    return {
        "commented": int(proc.returncode) == 0,
        "opened": False,
        "draft": True,
        "argv": argv,
        "returncode": int(proc.returncode),
    }


def open_draft_fix_pr(
    new_failures: list[dict[str, Any]],
    *,
    trigger: str,
    opener: Callable[[dict[str, str]], Any] | None = None,
    finder: Callable[[str], dict[str, Any] | None] | None = None,
    commenter: Callable[[dict[str, Any], str], Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    ready = draftable_failures(new_failures)
    if not ready:
        return {
            "opened": False,
            "draft": True,
            "skipped": "no deterministic signature (flaky or missing scenario id)",
        }
    payload = draft_pr_payload(ready, trigger=trigger)
    existing = find_open_draft_for_signature(
        payload.get("signature") or "", finder=finder, runner=runner
    )
    if existing:
        comment = comment_on_existing_draft(
            existing, body=payload["body"], commenter=commenter, runner=runner
        )
        comment["payload"] = payload
        comment["existing"] = existing
        return comment
    if opener is not None:
        result = opener(payload)
        return {"opened": True, "draft": True, "payload": payload, "result": result}
    argv = assert_draft_argv_safe(
        build_draft_pr_argv(title=payload["title"], body=payload["body"])
    )
    if shutil.which("gh") is None:
        return {
            "opened": False,
            "draft": True,
            "skipped": "gh not available",
            "argv": argv,
            "payload": payload,
        }
    run = runner or subprocess.run
    proc = run(argv, check=False, capture_output=True, text=True)
    return {
        "opened": int(proc.returncode) == 0,
        "draft": True,
        "argv": argv,
        "payload": payload,
        "returncode": int(proc.returncode),
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def _note_once(
    key: str,
    *,
    store: dict[str, bool] | None,
    persist_path: Path | None = None,
) -> bool:
    """Return True if this key has not been noted yet. Used for one Chat."""
    seen = store if store is not None else {}
    if persist_path is not None and persist_path.is_file() and not seen:
        try:
            loaded = json.loads(persist_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                seen.update({str(k): bool(v) for k, v in loaded.items()})
        except (OSError, json.JSONDecodeError):
            pass
    if seen.get(key):
        return False
    seen[key] = True
    if persist_path is not None:
        persist_path.parent.mkdir(parents=True, exist_ok=True)
        persist_path.write_text(json.dumps(seen, sort_keys=True), encoding="utf-8")
    return True


def run_regression_battery(
    *,
    trigger: str = "ci",
    notify: bool = False,
    draft_pr: bool = False,
    logic_runner: Callable[[], list[dict[str, Any]]] | None = None,
    replay_runner: Callable[[], list[dict[str, Any]]] | None = None,
    parity_runner: Callable[[], list[dict[str, Any]]] | None = None,
    poster: Callable[..., Any] | None = None,
    pr_opener: Callable[[dict[str, str]], Any] | None = None,
    pr_finder: Callable[[str], dict[str, Any] | None] | None = None,
    pr_commenter: Callable[[dict[str, Any], str], Any] | None = None,
    known_accepted: dict[str, frozenset[str]] | None = None,
    include_pytest: bool | None = None,
    chat_once: dict[str, bool] | None = None,
    chat_once_path: Path | None = None,
) -> dict[str, Any]:
    """Run logic suite + replay catalog. Chat/draft only on NEW fails."""
    if current_robie_env() in PRODUCTION_ENV_NAMES and trigger == "replay-live":
        raise ProductionGuardError("refusing live replay: ROBIE_ENV is Production")
    logic = list(logic_runner() if logic_runner is not None else run_logic_suite(
        include_pytest=include_pytest
    ))
    replay = list(
        replay_runner() if replay_runner is not None else run_replay_scenarios()
    )
    parity = list(
        parity_runner() if parity_runner is not None else parity_gap_results()
    )
    classified = classify_results(
        logic + replay + parity, known_accepted=known_accepted
    )
    new_failures = classified["new_failures"]
    inconclusive = classified["inconclusive"]
    message = (
        format_new_failure_chat(new_failures, trigger=trigger)
        if new_failures
        else None
    )
    posted = False
    post_error = None
    draft: dict[str, Any] | None = None
    if new_failures and notify:
        try:
            posted = _post_new_failure(message or "", poster=poster)
        except Exception as exc:
            post_error = f"{type(exc).__name__}: {exc}"
        if draft_pr:
            try:
                draft = open_draft_fix_pr(
                    new_failures,
                    trigger=trigger,
                    opener=pr_opener,
                    finder=pr_finder,
                    commenter=pr_commenter,
                )
            except Exception as exc:
                draft = {
                    "opened": False,
                    "draft": True,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    elif inconclusive and notify:
        note_key = "inconclusive:" + ",".join(
            str(item.get("id") or "") for item in inconclusive
        )
        persist = chat_once_path
        if persist is None and chat_once is None:
            persist = default_work_dir() / "inconclusive-noted.json"
        if _note_once(note_key, store=chat_once, persist_path=persist):
            try:
                posted = _post_new_failure(
                    format_inconclusive_chat(inconclusive, trigger=trigger),
                    poster=poster,
                )
            except Exception as exc:
                post_error = f"{type(exc).__name__}: {exc}"
    verdict = classified["verdict"]
    return {
        "ok": not new_failures,
        "green": verdict == "SEEN_CLEAR",
        "verdict": verdict,
        "trigger": trigger,
        "results": logic + replay + parity,
        "new_failures": new_failures,
        "known_accepted": classified["known_accepted"],
        "passed": classified["passed"],
        "inconclusive": inconclusive,
        "healthy": classified["healthy"],
        "chat_posted": posted,
        "message": message,
        "chat_post_error": post_error,
        "draft_pr": draft,
        "human_gate": HUMAN_GATE,
        "scope": SCOPE,
        "seen_clear_text": SEEN_CLEAR_TEXT,
    }


def maybe_detach(argv: list[str]) -> None:
    """Double-fork so ExecStartPost does not block hermes-gateway."""
    if "--detach" not in argv and os.environ.get("ROBIE_REGRESSION_DETACH") != "1":
        return
    if os.fork() != 0:
        os._exit(0)
    os.setsid()
    if os.fork() != 0:
        os._exit(0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROBIE regression battery. Logic tests + Test replay catalog. "
            "NEW fails post to Chat without @robie and may open a draft PR. "
            "Previously seen failures only. Does not merge or deploy."
        )
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Run the PR battery and exit non-zero on NEW fails. No Chat.",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Chat + draft-PR hook on NEW fails (post-deploy).",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="Double-fork before running so systemd ExecStartPost returns.",
    )
    parser.add_argument("--trigger", default="")
    return parser.parse_args(argv)


def failure_log_item(item: dict[str, Any]) -> dict[str, Any]:
    """Bounded diagnostic detail for private CI logs; never hide the cause."""
    return {
        "id": item.get("id"),
        "outcome": item.get("outcome"),
        "returncode": item.get("returncode"),
        "evidence": str(item.get("evidence") or "")[-4000:],
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    raw = list(argv if argv is not None else sys.argv[1:])
    maybe_detach(raw)
    trigger = args.trigger or ("post-deploy" if args.notify else "ci")
    report = run_regression_battery(
        trigger=trigger,
        notify=bool(args.notify),
        draft_pr=bool(args.notify),
    )
    print(json.dumps(
        {
            "ok": report["ok"],
            "green": report.get("green"),
            "verdict": report.get("verdict"),
            "trigger": report["trigger"],
            "scope": report.get("scope"),
            "seen_clear_text": report.get("seen_clear_text"),
            "new_failures": [
                failure_log_item(item)
                for item in report["new_failures"]
            ],
            "known_accepted": [
                {"id": item.get("id"), "outcome": item.get("outcome")}
                for item in report["known_accepted"]
            ],
            "inconclusive": [
                {"id": item.get("id"), "outcome": item.get("outcome")}
                for item in report.get("inconclusive") or []
            ],
            "chat_posted": report["chat_posted"],
            "draft_pr_opened": bool((report.get("draft_pr") or {}).get("opened")),
            "draft_pr_commented": bool((report.get("draft_pr") or {}).get("commented")),
        },
        sort_keys=True,
    ))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
