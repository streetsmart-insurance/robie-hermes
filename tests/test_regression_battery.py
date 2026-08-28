"""Regression battery: PR logic suite, post-deploy notify, human-gated draft PR."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.quote_replay import refuse_production_targets
from robie_job_engine.regression_battery import (
    DEFAULT_CHAT_SPACE,
    HUMAN_GATE,
    KNOWN_ACCEPTED_REPLAY,
    LIVE_PRODUCTION_JOB_DB,
    LOGIC_PYTEST_MODULES,
    PYTEST_ONLY_MODULES,
    assert_draft_argv_safe,
    discover_unittest_suite,
    build_draft_pr_argv,
    classify_results,
    format_new_failure_chat,
    isolated_env,
    logic_job_type_argv,
    logic_pytest_argv,
    logic_unittest_argv,
    open_draft_fix_pr,
    run_regression_battery,
    run_replay_scenarios,
)
from robie_job_engine.test_runtime import ProductionGuardError


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "robie_job_engine" / "regression_battery.py").read_text(
    encoding="utf-8"
)
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
DROP_IN = (
    ROOT / "deploy/systemd/zz-hermes-gateway-job-engine-path.conf"
).read_text(encoding="utf-8")
SERVICE = (
    ROOT / "deploy/systemd/robie-regression-battery.service"
).read_text(encoding="utf-8")
VERIFY = (ROOT / "scripts" / "verify-release.sh").read_text(encoding="utf-8")


def _logic_pass() -> list[dict]:
    return [
        {
            "id": "job-type-gate",
            "kind": "logic",
            "ok": True,
            "outcome": "PASS",
            "evidence": "ok",
        },
        {
            "id": "unittest-discover",
            "kind": "logic",
            "ok": True,
            "outcome": "PASS",
            "evidence": "ok",
        },
        {
            "id": "pytest-phase3",
            "kind": "logic",
            "ok": True,
            "outcome": "PASS",
            "evidence": "ok",
        },
    ]


def _new_replay_fail() -> list[dict]:
    return [
        {
            "id": "quote-replay:simulated-new",
            "kind": "replay",
            "ok": False,
            "outcome": "FAILED",
            "evidence": "unexpected verifier miss",
        }
    ]


class ContractTests(unittest.TestCase):
    def test_ci_still_runs_logic_suite_on_pull_requests(self):
        self.assertIn("pull_request:", CI)
        self.assertIn("jobs:", CI)
        self.assertIn("  test:", CI)
        self.assertIn("  canonical-paths:", CI)
        self.assertIn("contents: read", CI)
        self.assertIn("robie_job_engine.regression_battery --ci", CI)
        self.assertNotIn("contents: write", CI)
        self.assertNotIn("pull-requests: write", CI)
        self.assertNotIn("gh pr merge", CI)
        folded = "\n".join(
            line for line in CI.splitlines() if "permissions:" in line or "contents:" in line
        )
        self.assertIn("contents: read", folded)
        unittest_argv = logic_unittest_argv()
        self.assertIn("unittest", " ".join(unittest_argv))
        self.assertIn("discover", " ".join(unittest_argv))
        gate_argv = logic_job_type_argv()
        self.assertIn("check-job-type-gate.py", " ".join(gate_argv))
        self.assertIn("check", gate_argv)
        pytest_argv = logic_pytest_argv()
        for module in LOGIC_PYTEST_MODULES:
            self.assertIn(module, pytest_argv)
        self.assertIn("unittest", SOURCE)
        self.assertIn("discover", SOURCE)
        self.assertIn("check-job-type-gate.py", SOURCE)
        self.assertIn("test_carrier_directory.py", SOURCE)

    def test_source_and_units_cannot_merge_or_deploy(self):
        folded = SOURCE.casefold()
        self.assertNotIn("gh pr merge", folded)
        self.assertNotIn("systemctl restart", folded)
        self.assertNotIn("hermes-poc-01", folded)
        self.assertNotIn("git pull", folded)
        self.assertIn("must not @robie", folded)
        self.assertNotIn("access_secret_version", folded)
        self.assertIn("--draft", SOURCE)
        self.assertIn("StreetSmartJake", SOURCE)
        self.assertIn("Carlo Confirm", SOURCE)
        self.assertIn("Type=oneshot", SERVICE)
        self.assertIn("-m robie_job_engine.regression_battery --notify", SERVICE)
        self.assertIn("ROBIE_REGRESSION_CHAT_SPACE=spaces/AAQAZbLJO78", SERVICE)
        self.assertIn("ROBIE_REGRESSION_WORK_DIR=/var/lib/robie-regression-battery", SERVICE)
        self.assertNotIn("systemctl restart", SERVICE)
        self.assertNotIn("ROBIE_JOB_DB=/opt/streetsmart-hermes/robie-job-engine/data/jobs.db", SERVICE)
        self.assertNotIn("@robie", SERVICE.casefold())
        self.assertIn(
            "-m robie_job_engine.regression_battery --notify --detach",
            DROP_IN,
        )
        self.assertIn(
            "-m robie_job_engine.production_preflight",
            DROP_IN,
        )
        self.assertNotIn("systemctl restart", DROP_IN)
        self.assertIn("regression_battery --ci", VERIFY)

    def test_isolated_env_drops_production_job_db(self):
        env = isolated_env(
            {
                "ROBIE_ENV": "PRODUCTION",
                "ROBIE_JOB_DB": LIVE_PRODUCTION_JOB_DB,
                "ROBIE_ARTIFACT_ROOT": "/opt/streetsmart-hermes/robie-job-engine/data/artifacts",
                "PATH": "/usr/bin",
            }
        )
        self.assertNotIn("ROBIE_ENV", env)
        self.assertNotIn("ROBIE_JOB_DB", env)
        self.assertNotIn("ROBIE_ARTIFACT_ROOT", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_unittest_discover_skips_pytest_only_modules_without_importing_them(self):
        self.assertEqual(
            PYTEST_ONLY_MODULES,
            {
                "test_carrier_directory",
                "test_ezlynx_poller",
                "test_video_to_skill",
            },
        )
        with durable_temporary_directory() as tmp:
            tests_dir = Path(tmp) / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            (tests_dir / "test_ok.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_a(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (tests_dir / "test_carrier_directory.py").write_text(
                "raise AssertionError('pytest-only module must not be imported')\n",
                encoding="utf-8",
            )
            suite = discover_unittest_suite(
                tests_dir,
                include_pytest_modules=False,
                top_level_dir=tests_dir,
            )
            self.assertEqual(suite.countTestCases(), 1)


class ClassifierAndNotifyTests(unittest.TestCase):
    def test_known_accepted_replay_does_not_post_or_draft(self):
        posted: list[tuple[str, str]] = []
        opened: list[dict] = []
        report = run_regression_battery(
            trigger="post-deploy",
            notify=True,
            draft_pr=True,
            logic_runner=_logic_pass,
            replay_runner=lambda: [
                {
                    "id": "quote-replay:missing-pdf",
                    "kind": "replay",
                    "ok": False,
                    "outcome": "BLOCKED",
                    "evidence": "no quote PDF path provided",
                }
            ],
            poster=lambda space, text: posted.append((space, text)),
            pr_opener=lambda payload: opened.append(payload) or {"url": "unused"},
        )
        self.assertTrue(report["ok"])
        self.assertEqual(report["new_failures"], [])
        self.assertEqual(len(report["known_accepted"]), 1)
        self.assertEqual(posted, [])
        self.assertEqual(opened, [])
        self.assertFalse(report["chat_posted"])

    def test_new_replay_failure_posts_chat_without_at_robie_and_opens_draft_pr(self):
        posted: list[tuple[str, str]] = []
        opened: list[dict] = []
        report = run_regression_battery(
            trigger="post-deploy",
            notify=True,
            draft_pr=True,
            logic_runner=_logic_pass,
            replay_runner=_new_replay_fail,
            poster=lambda space, text: posted.append((space, text)),
            pr_opener=lambda payload: opened.append(payload)
            or {"url": "https://github.com/streetsmart-insurance/robie-hermes/pull/999"},
        )
        self.assertFalse(report["ok"])
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], DEFAULT_CHAT_SPACE)
        self.assertIn("ROBIE regression battery — NEW fail", posted[0][1])
        self.assertIn("quote-replay:simulated-new", posted[0][1])
        self.assertIn("unexpected verifier miss", posted[0][1])
        self.assertNotIn("@robie", posted[0][1].casefold())
        self.assertIn("StreetSmartJake", posted[0][1])
        self.assertIn("Carlo Confirm", posted[0][1])
        self.assertTrue(report["chat_posted"])
        self.assertEqual(len(opened), 1)
        self.assertIn("do not merge", opened[0]["title"].casefold())
        self.assertEqual(opened[0]["draft"], "true")
        self.assertNotIn("@robie", opened[0]["body"].casefold())
        self.assertIn("StreetSmartJake", opened[0]["body"])
        self.assertTrue((report.get("draft_pr") or {}).get("draft"))
        self.assertTrue((report.get("draft_pr") or {}).get("opened"))

    def test_format_and_draft_argv_cannot_merge_or_mention_robie(self):
        text = format_new_failure_chat(
            _new_replay_fail(), trigger="post-deploy"
        )
        self.assertNotIn("@robie", text.casefold())
        self.assertIn(HUMAN_GATE, text)
        argv = build_draft_pr_argv(title="ROBIE regression: NEW fail (do not merge)", body=text)
        assert_draft_argv_safe(argv)
        self.assertEqual(argv[:4], ["gh", "pr", "create", "--draft"])
        with self.assertRaises(ValueError):
            assert_draft_argv_safe(["gh", "pr", "merge", "123"])
        with self.assertRaises(ValueError):
            assert_draft_argv_safe(
                ["gh", "pr", "create", "--draft", "--merge", "--title", "x"]
            )
        opened = []
        result = open_draft_fix_pr(
            _new_replay_fail(),
            trigger="ci",
            opener=lambda payload: opened.append(payload) or {"draft": True},
        )
        self.assertTrue(result["opened"])
        self.assertTrue(result["draft"])
        self.assertEqual(len(opened), 1)

    def test_logic_failure_is_new_even_when_replay_is_accepted(self):
        classified = classify_results(
            [
                {
                    "id": "unittest-discover",
                    "ok": False,
                    "outcome": "FAILED",
                    "evidence": "AssertionError",
                },
                {
                    "id": "quote-replay:missing-pdf",
                    "ok": True,
                    "outcome": "BLOCKED",
                    "evidence": "no pdf",
                },
            ]
        )
        self.assertEqual(
            [item["id"] for item in classified["new_failures"]],
            ["unittest-discover"],
        )
        self.assertIn("quote-replay:missing-pdf", KNOWN_ACCEPTED_REPLAY)


class ReplayGuardTests(unittest.TestCase):
    def test_replay_catalog_accepts_known_outcomes_and_refuses_production(self):
        with durable_temporary_directory() as tmp:
            results = run_replay_scenarios(work_dir=Path(tmp) / "replay")
        ids = {item["id"]: item for item in results}
        self.assertEqual(ids["quote-replay:missing-pdf"]["outcome"], "BLOCKED")
        self.assertEqual(ids["quote-replay:production-env"]["outcome"], "REFUSED")
        self.assertEqual(ids["quote-replay:live-hermes-job-db"]["outcome"], "REFUSED")
        self.assertEqual(
            ids["quote-replay:test-paths-require-test-env"]["outcome"], "REFUSED"
        )
        self.assertTrue(all(item["ok"] for item in results))
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            with self.assertRaises(ProductionGuardError):
                refuse_production_targets("jobs.db", "artifacts")
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with self.assertRaises(ProductionGuardError):
                refuse_production_targets(LIVE_PRODUCTION_JOB_DB, "artifacts")

    def test_replay_refuses_live_hermes_work_dir(self):
        with self.assertRaises(ProductionGuardError):
            run_replay_scenarios(
                work_dir=Path("/opt/streetsmart-hermes/robie-job-engine/data")
            )


if __name__ == "__main__":
    unittest.main()
