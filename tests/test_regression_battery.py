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
    PARITY_PATH,
    PYTEST_ONLY_MODULES,
    SCOPE,
    SEEN_CLEAR_TEXT,
    SIGNATURE_MARKER,
    assert_draft_argv_safe,
    discover_unittest_suite,
    build_draft_pr_argv,
    classify_results,
    destroyed_latest_with_older_enabled_is_healthy,
    evidence_is_flaky,
    failure_signature,
    format_inconclusive_chat,
    format_new_failure_chat,
    isolated_env,
    load_parity_catalog,
    logic_job_type_argv,
    logic_pytest_argv,
    logic_unittest_argv,
    open_draft_fix_pr,
    parity_gap_results,
    pytest_available,
    run_regression_battery,
    run_logic_suite,
    run_replay_scenarios,
    run_secret_health_scenario,
)
from robie_job_engine.regression_scenarios import (
    ACTION_GATE_CHAT,
    ASCEND_ACCESSIBLE_NAME_CHAT,
    ASCEND_AGENCY_FEE_CHAT,
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
    I_DID_IT_PROSE,
    PLAYWRIGHT_CDP_CHAT,
    PLAYWRIGHT_SILENT_CHAT,
    SAME_DAY_RULE,
    SCENARIOS_PATH,
    close_new_failure_incident,
    IncidentCloseError,
    load_scenario_catalog,
    run_action_gate_scenario,
    run_agency_fee_default_scenario,
    run_ascend_locator_audit_scenario,
    run_concat_job_id_eb96f620_scenario,
    run_customer_type_lob_scenario,
    run_false_success_scenario,
    run_follow_live_playwright_tab_scenario,
    run_hitl_resume_scenarios,
    run_hitl_tone_scenario,
    run_named_scenarios,
    run_accessible_name_scenario,
    run_same_day_scenario_rule,
    run_sender_not_robie_ai_scenario,
    run_spinner_timing_scenario,
    run_too_soon_zero_element_scenario,
    run_unique_listbox_option_scenario,
    run_wait_spinner_scenario,
    run_zero_playwright_tool_row_scenario,
    run_cdp_json_list_fixture_scenario,
)
from robie_job_engine.worker_contract import claims_unverified_destination_progress
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
        self.assertIn("Dusty pings if it sits", SOURCE)
        self.assertIn("previously seen failures have not come back", SOURCE)
        self.assertIn("nothing we have already seen is wrong", SOURCE)
        self.assertIn("INCONCLUSIVE", SOURCE)
        self.assertIn("not a Production all-clear", SOURCE)
        self.assertIn("how the simulator grows", SOURCE)
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
                "ROBIE_SKILL_SYNC_ROOT": "/opt/streetsmart-hermes/.hermes/drive-skills",
                "PATH": "/usr/bin",
            }
        )
        self.assertNotIn("ROBIE_ENV", env)
        self.assertNotIn("ROBIE_JOB_DB", env)
        self.assertNotIn("ROBIE_ARTIFACT_ROOT", env)
        self.assertNotIn("ROBIE_SKILL_SYNC_ROOT", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_logic_suite_marks_only_its_synthetic_subprocess_as_test(self):
        seen: list[tuple[str, str]] = []

        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def runner(*args, **kwargs):
            del args
            seen.append(
                (
                    kwargs["env"].get("ROBIE_ENV", ""),
                    kwargs["env"].get("ROBIE_SKILL_SYNC_ROOT", ""),
                )
            )
            return Result()

        results = run_logic_suite(
            repo_root=ROOT,
            runner=runner,
            include_pytest=False,
        )
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual([item[0] for item in seen], ["TEST", "TEST"])
        self.assertTrue(
            all(item[1].endswith("regression-skill-sync-empty") for item in seen)
        )

    def test_unittest_discover_skips_pytest_only_modules_without_importing_them(self):
        self.assertEqual(
            PYTEST_ONLY_MODULES,
            {
                "test_carrier_directory",
                "test_ezlynx_poller",
                "test_playwright_route_fixtures",
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
            skip_body = (
                "raise AssertionError('pytest-only module must not be imported')\n"
            )
            for name in PYTEST_ONLY_MODULES:
                (tests_dir / f"{name}.py").write_text(skip_body, encoding="utf-8")
            suite = discover_unittest_suite(
                tests_dir,
                include_pytest_modules=False,
                top_level_dir=tests_dir,
            )
            self.assertEqual(suite.countTestCases(), 1)

    def test_pytest_console_script_from_another_interpreter_is_not_available(self):
        with patch(
            "robie_job_engine.regression_battery._can_import_pytest",
            return_value=False,
        ), patch(
            "robie_job_engine.regression_battery.shutil.which",
            return_value="/usr/local/bin/pytest",
        ):
            self.assertFalse(pytest_available())


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
            parity_runner=lambda: [],
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
            parity_runner=lambda: [],
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
        self.assertIn("previously seen failures have not come back", posted[0][1])
        self.assertNotIn("nothing is wrong", posted[0][1].replace(SCOPE, ""))
        self.assertIn("Dusty pings if it sits", opened[0]["body"])
        self.assertIn("Carlo Ferrara", opened[0]["body"])
        self.assertIn(SIGNATURE_MARKER, opened[0]["body"])
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
    def test_missing_pdf_replay_is_explicitly_test_scoped(self):
        observed_env: list[str] = []

        def missing_pdf(**kwargs):
            del kwargs
            observed_env.append(os.environ.get("ROBIE_ENV", ""))
            return {"outcome": "BLOCKED", "reason": "no pdf"}

        with durable_temporary_directory() as tmp, patch(
            "robie_job_engine.regression_battery.run_quote_replay",
            side_effect=missing_pdf,
        ), patch(
            "robie_job_engine.regression_battery.run_named_scenarios",
            return_value=[],
        ):
            run_replay_scenarios(work_dir=Path(tmp) / "replay")
        self.assertEqual(observed_env, ["TEST"])

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
        self.assertEqual(
            ids["login-secret:destroyed-latest-enabled-older"]["outcome"], "HEALTHY"
        )
        self.assertEqual(ids["same-day:named-scenario-before-close"]["outcome"], "PASS")
        self.assertEqual(
            ids["hitl-resume:re-lease-after-gateway-restart"]["outcome"], "PASS"
        )
        self.assertEqual(ids["hitl-resume:no-second-job"]["outcome"], "PASS")
        self.assertEqual(ids["hitl-resume:no-retry-leftover-failed"]["outcome"], "PASS")
        self.assertIn(
            ids["false-success:complete-prose-zero-evidence"]["outcome"],
            {"UNVERIFIED", "FAILED"},
        )
        self.assertEqual(ids["ascend:locator-and-artifact-audit"]["outcome"], "PASS")
        self.assertEqual(ids["artifact-path:concat-job-id-eb96f620"]["outcome"], "PASS")
        self.assertEqual(ids["hitl-tone:dry-playwright-blocked"]["outcome"], "PASS")
        self.assertEqual(ids["ascend-roles:sender-not-robie-ai"]["outcome"], "PASS")
        self.assertEqual(ids["ascend-new-program:wait-spinner"]["outcome"], "PASS")
        self.assertEqual(ids["ascend-new-program:accessible-name"]["outcome"], "PASS")
        self.assertEqual(ids["ascend-new-program:spinner-timing"]["outcome"], "PASS")
        self.assertEqual(ids["ascend-create:agency-fee-default"]["outcome"], "PASS")
        self.assertEqual(ids["ascend-create:too-soon-zero-element"]["outcome"], "PASS")
        self.assertEqual(ids["ascend-create:unique-listbox-option"]["outcome"], "PASS")
        self.assertEqual(ids["ascend-customer-type:lob"]["outcome"], "PASS")
        self.assertEqual(ids["recording:follow-live-playwright-tab"]["outcome"], "PASS")
        self.assertEqual(
            ids["playwright-silent:zero-tool-rows-1df9740b"]["outcome"], "PASS"
        )
        self.assertEqual(ids["playwright-cdp:json-list-fixture"]["outcome"], "PASS")
        self.assertTrue(all(item["ok"] for item in results))
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            with self.assertRaises(ProductionGuardError):
                refuse_production_targets("jobs.db", "artifacts")
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            with self.assertRaises(ProductionGuardError):
                refuse_production_targets(LIVE_PRODUCTION_JOB_DB, "artifacts")

    def test_replay_catalog_is_repeatable_in_the_same_durable_base(self):
        with durable_temporary_directory() as tmp:
            base = Path(tmp) / "replay"
            first = run_replay_scenarios(work_dir=base)
            second = run_replay_scenarios(work_dir=base)
        self.assertTrue(all(item["ok"] for item in first))
        self.assertTrue(all(item["ok"] for item in second))
        self.assertEqual(
            [item["id"] for item in first],
            [item["id"] for item in second],
        )

    def test_replay_refuses_live_hermes_work_dir(self):
        with self.assertRaises(ProductionGuardError):
            run_replay_scenarios(
                work_dir=Path("/opt/streetsmart-hermes/robie-job-engine/data")
            )


class ParityAndScopeTests(unittest.TestCase):
    def test_parity_catalog_is_maintained_and_never_green(self):
        catalog = load_parity_catalog()
        self.assertIn("when a deploy or HITL shows drift", catalog["maintain"])
        self.assertGreaterEqual(len(catalog["diffs"]), 3)
        gaps = {item["id"]: item for item in parity_gap_results(catalog)}
        self.assertIn("parity:secrets", gaps)
        self.assertEqual(gaps["parity:secrets"]["outcome"], "INCONCLUSIVE")
        self.assertFalse(gaps["parity:secrets"]["ok"])
        posted: list[str] = []
        once: dict[str, bool] = {}
        report = run_regression_battery(
            trigger="test-vm",
            notify=True,
            draft_pr=True,
            logic_runner=_logic_pass,
            replay_runner=lambda: [],
            poster=lambda space, text: posted.append(text),
            pr_opener=lambda payload: (_ for _ in ()).throw(
                AssertionError("INCONCLUSIVE must not open a draft PR")
            ),
            chat_once=once,
        )
        self.assertTrue(report["ok"])
        self.assertFalse(report["green"])
        self.assertEqual(report["verdict"], "INCONCLUSIVE")
        self.assertEqual(len(posted), 1)
        self.assertIn("INCONCLUSIVE", posted[0])
        self.assertIn("Not a Production all-clear", posted[0])
        self.assertIn(SCOPE, posted[0])
        self.assertNotIn("@robie", posted[0].casefold())
        again = run_regression_battery(
            trigger="test-vm",
            notify=True,
            draft_pr=True,
            logic_runner=_logic_pass,
            replay_runner=lambda: [],
            poster=lambda space, text: posted.append(text),
            pr_opener=lambda payload: (_ for _ in ()).throw(
                AssertionError("second INCONCLUSIVE must stay quiet")
            ),
            chat_once=once,
        )
        self.assertEqual(len(posted), 1)
        self.assertFalse(again["green"])
        state = (ROOT / "CURRENT_STATE.md").read_text(encoding="utf-8")
        release = (ROOT / "RELEASE_PROCESS.md").read_text(encoding="utf-8")
        for text in (state, release):
            flat = " ".join(text.replace("**", "").split())
            self.assertIn("HITL shows drift", flat)
            self.assertIn("not a Production all-clear", flat)
            self.assertIn("previously seen failures have not come back", flat)
            self.assertIn("INCONCLUSIVE", flat)
            self.assertIn("how the simulator grows", flat)
            self.assertIn("named deterministic scenario", flat)
            self.assertIn("PR 35", flat)
            self.assertIn("automatic simulator", flat)
            self.assertIn("do not call 36 the simulator", flat)
            self.assertIn("Dusty walks the live site", flat)
            self.assertIn("just trying it", flat)
            self.assertIn("N=1", flat)
            self.assertIn("follow-tab", flat.casefold())
        self.assertTrue(PARITY_PATH.is_file())
        self.assertTrue(SCENARIOS_PATH.is_file())

    def test_destroyed_latest_with_older_enabled_is_healthy_and_quiet(self):
        self.assertTrue(
            destroyed_latest_with_older_enabled_is_healthy(
                newest_state="DESTROYED",
                newest_enabled_version="versions/1",
                alert=False,
            )
        )
        self.assertFalse(
            destroyed_latest_with_older_enabled_is_healthy(
                newest_state="DESTROYED",
                newest_enabled_version=None,
                alert=True,
            )
        )
        posted: list[str] = []
        opened: list[dict] = []
        report = run_regression_battery(
            trigger="post-deploy",
            notify=True,
            draft_pr=True,
            logic_runner=_logic_pass,
            replay_runner=lambda: [run_secret_health_scenario()],
            parity_runner=lambda: [],
            poster=lambda space, text: posted.append(text),
            pr_opener=lambda payload: opened.append(payload),
        )
        self.assertTrue(report["ok"])
        self.assertTrue(report["green"])
        self.assertEqual(report["verdict"], "SEEN_CLEAR")
        self.assertEqual(report["seen_clear_text"], SEEN_CLEAR_TEXT)
        self.assertIn("not a Production all-clear", report["seen_clear_text"])
        self.assertEqual(posted, [])
        self.assertEqual(opened, [])
        self.assertEqual(report["healthy"][0]["outcome"], "HEALTHY")

    def test_flaky_evidence_does_not_open_a_draft_pr(self):
        self.assertTrue(evidence_is_flaky("connection reset while waiting"))
        posted: list[str] = []
        opened: list[dict] = []
        report = run_regression_battery(
            trigger="post-deploy",
            notify=True,
            draft_pr=True,
            logic_runner=_logic_pass,
            replay_runner=lambda: [
                {
                    "id": "quote-replay:simulated-new",
                    "ok": False,
                    "outcome": "FAILED",
                    "evidence": "TimeoutError: page.goto timed out after 30000",
                }
            ],
            parity_runner=lambda: [],
            poster=lambda space, text: posted.append(text),
            pr_opener=lambda payload: opened.append(payload),
        )
        self.assertFalse(report["ok"])
        self.assertEqual(len(posted), 1)
        self.assertEqual(opened, [])
        self.assertEqual((report.get("draft_pr") or {}).get("opened"), False)
        self.assertIn("flaky", str((report.get("draft_pr") or {}).get("skipped") or "").casefold())

    def test_same_signature_comments_on_existing_draft_instead_of_opening_another(self):
        first = _new_replay_fail()[0]
        second = dict(first)
        self.assertEqual(failure_signature(first), failure_signature(second))
        comments: list[tuple[dict, str]] = []
        opened: list[dict] = []
        result = open_draft_fix_pr(
            [first],
            trigger="post-deploy",
            opener=lambda payload: opened.append(payload) or {"url": "new"},
            finder=lambda signature: {
                "number": 99,
                "title": "ROBIE regression: NEW fail (do not merge)",
                "url": "https://github.com/streetsmart-insurance/robie-hermes/pull/99",
            },
            commenter=lambda existing, body: comments.append((existing, body)),
        )
        self.assertFalse(result["opened"])
        self.assertTrue(result["commented"])
        self.assertEqual(opened, [])
        self.assertEqual(comments[0][0]["number"], 99)
        self.assertIn("Dusty pings if it sits", comments[0][1])
        self.assertIn("Carlo Ferrara", comments[0][1])
        self.assertIn(SIGNATURE_MARKER, comments[0][1])
        text = format_inconclusive_chat(
            [{"id": "parity:secrets", "evidence": "Production login-secret ENABLED versions"}],
            trigger="test-vm",
        )
        self.assertIn("Not a Production all-clear", text)
        self.assertNotIn("@robie", text.casefold())


class SameDayHitlAndFalseSuccessTests(unittest.TestCase):
    def test_cannot_close_new_failure_mode_without_named_scenario(self):
        self.assertIn("before we call the incident closed", SAME_DAY_RULE)
        self.assertIn("how the simulator grows", SAME_DAY_RULE)
        catalog = load_scenario_catalog()
        self.assertIn("how the simulator grows", catalog["rule"])
        incidents = {row["id"]: row for row in catalog["incidents"]}
        for job_id in ("09d69760", "da53765b", "6cf6f6ae"):
            self.assertEqual(
                incidents[job_id]["scenario"],
                "hitl-resume:re-lease-after-gateway-restart",
            )
        for job_id in ("30777947", "c31f9c69"):
            self.assertEqual(
                incidents[job_id]["scenario"],
                "false-success:complete-prose-zero-evidence",
            )
        self.assertEqual(
            incidents["artifact-concat-job-id"]["scenario"],
            "ascend:locator-and-artifact-audit",
        )
        self.assertEqual(
            incidents["eb96f620"]["scenario"],
            "artifact-path:concat-job-id-eb96f620",
        )
        self.assertEqual(
            incidents["hitl-cowboy-tone"]["scenario"],
            "hitl-tone:dry-playwright-blocked",
        )
        self.assertEqual(
            incidents["ascend-roles-robie-ai"]["scenario"],
            "ascend-roles:sender-not-robie-ai",
        )
        self.assertEqual(
            incidents["eb96f620-vision-spinner"]["scenario"],
            "ascend-new-program:wait-spinner",
        )
        self.assertEqual(
            incidents["f7653a85"]["scenario"],
            "ascend-new-program:accessible-name",
        )
        self.assertEqual(
            incidents["ascend-customer-type-lob"]["scenario"],
            "ascend-customer-type:lob",
        )
        self.assertEqual(
            incidents["pawiva-spinner-seconds"]["scenario"],
            "ascend-new-program:spinner-timing",
        )
        self.assertEqual(
            incidents["pawiva-agency-fee-default"]["scenario"],
            "ascend-create:agency-fee-default",
        )
        self.assertEqual(
            incidents["ascend-import-document-too-soon"]["scenario"],
            "ascend-create:too-soon-zero-element",
        )
        self.assertEqual(
            incidents["38c0fa79"]["scenario"],
            "ascend-create:unique-listbox-option",
        )
        for job_id in ("807f8920", "468d1575"):
            self.assertEqual(
                incidents[job_id]["scenario"],
                "recording:follow-live-playwright-tab",
            )
        self.assertEqual(
            incidents["test-gate-skipped-807f8920-38c0fa79"]["scenario"],
            "action-gate:test-pass-required-before-production",
        )
        self.assertEqual(
            incidents["1df9740b"]["scenario"],
            "playwright-silent:zero-tool-rows-1df9740b",
        )
        with self.assertRaises(IncidentCloseError):
            close_new_failure_incident(
                {"id": "new-today", "new_failure_mode": True, "closed": False}
            )
        closed = close_new_failure_incident(
            {
                "id": "new-today",
                "new_failure_mode": True,
                "scenario": "false-success:complete-prose-zero-evidence",
            }
        )
        self.assertTrue(closed["closed"])
        self.assertEqual(run_same_day_scenario_rule()["outcome"], "PASS")

    def test_follow_live_playwright_tab_scenario_fails_if_recording_stays_on_listing(self):
        with durable_temporary_directory() as tmp:
            result = run_follow_live_playwright_tab_scenario(
                work_dir=Path(tmp) / "follow-tab"
            )
        self.assertTrue(result["ok"], result.get("evidence"))
        self.assertEqual(result["id"], "recording:follow-live-playwright-tab")
        self.assertEqual(result["outcome"], "PASS")
        text = format_new_failure_chat(
            [
                {
                    "id": "recording:follow-live-playwright-tab",
                    "outcome": "FAILED",
                    "evidence": "recorder stayed on listing",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(FOLLOW_TAB_CHAT, text)
        self.assertIn("807f8920", text)
        self.assertNotIn("@robie", text.casefold())

    def test_action_gate_scenario_refuses_production_chat_without_test_pass(self):
        with durable_temporary_directory() as tmp:
            result = run_action_gate_scenario(work_dir=Path(tmp) / "action-gate")
        self.assertTrue(result["ok"], result.get("evidence"))
        self.assertEqual(result["id"], "action-gate:test-pass-required-before-production")
        self.assertEqual(result["outcome"], "PASS")
        text = format_new_failure_chat(
            [
                {
                    "id": "action-gate:test-pass-required-before-production",
                    "outcome": "FAILED",
                    "evidence": "Production Chat started Ascend",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(ACTION_GATE_CHAT, text)
        self.assertIn("807f8920", text)
        self.assertIn("38c0fa79", text)
        self.assertNotIn("@robie", text.casefold())

    def test_zero_playwright_tool_row_and_cdp_fixture_scenarios_pass(self):
        with durable_temporary_directory() as tmp:
            silent = run_zero_playwright_tool_row_scenario(
                work_dir=Path(tmp) / "playwright-silent"
            )
            cdp = run_cdp_json_list_fixture_scenario(
                work_dir=Path(tmp) / "playwright-cdp"
            )
        self.assertTrue(silent["ok"], silent.get("evidence"))
        self.assertEqual(silent["id"], "playwright-silent:zero-tool-rows-1df9740b")
        self.assertTrue(cdp["ok"], cdp.get("evidence"))
        self.assertEqual(cdp["id"], "playwright-cdp:json-list-fixture")
        silent_text = format_new_failure_chat(
            [
                {
                    "id": "playwright-silent:zero-tool-rows-1df9740b",
                    "outcome": "FAILED",
                    "evidence": "UNVERIFIED hide",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(PLAYWRIGHT_SILENT_CHAT, silent_text)
        self.assertIn("1df9740b", silent_text)
        self.assertNotIn("@robie", silent_text.casefold())
        cdp_text = format_new_failure_chat(
            [
                {
                    "id": "playwright-cdp:json-list-fixture",
                    "outcome": "FAILED",
                    "evidence": "no checkpoint",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(PLAYWRIGHT_CDP_CHAT, cdp_text)

    def test_ascend_locator_audit_scenario_is_ci_only_and_passes(self):
        with durable_temporary_directory() as tmp:
            result = run_ascend_locator_audit_scenario(
                work_dir=Path(tmp) / "ascend-named"
            )
        self.assertTrue(result["ok"], result.get("evidence"))
        self.assertEqual(result["id"], "ascend:locator-and-artifact-audit")
        self.assertFalse(result.get("finance_agreement"))

    def test_eb96f620_concat_path_and_hitl_tone_scenarios_pass(self):
        with durable_temporary_directory() as tmp:
            path_result = run_concat_job_id_eb96f620_scenario(
                work_dir=Path(tmp) / "eb96-named"
            )
        self.assertTrue(path_result["ok"], path_result.get("evidence"))
        self.assertEqual(path_result["id"], "artifact-path:concat-job-id-eb96f620")
        tone = run_hitl_tone_scenario()
        self.assertTrue(tone["ok"], tone.get("evidence"))
        self.assertEqual(tone["id"], "hitl-tone:dry-playwright-blocked")
        text = format_new_failure_chat(
            [
                {
                    "id": "artifact-path:concat-job-id-eb96f620",
                    "outcome": "FAILED",
                    "evidence": "concat folder accepted",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(CONCAT_PATH_CHAT, text)
        self.assertIn("eb96f620", text)
        tone_text = format_new_failure_chat(
            [
                {
                    "id": "hitl-tone:dry-playwright-blocked",
                    "outcome": "FAILED",
                    "evidence": "cowboy HITL posted",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(HITL_TONE_CHAT, tone_text)
        roles = run_sender_not_robie_ai_scenario()
        self.assertTrue(roles["ok"], roles.get("evidence"))
        spinner = run_wait_spinner_scenario()
        self.assertTrue(spinner["ok"], spinner.get("evidence"))
        accessible = run_accessible_name_scenario()
        self.assertTrue(accessible["ok"], accessible.get("evidence"))
        self.assertEqual(accessible["id"], "ascend-new-program:accessible-name")
        roles_text = format_new_failure_chat(
            [
                {
                    "id": "ascend-roles:sender-not-robie-ai",
                    "outcome": "FAILED",
                    "evidence": "Robie AI left on Producer",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(ASCEND_ROLES_CHAT, roles_text)
        spinner_text = format_new_failure_chat(
            [
                {
                    "id": "ascend-new-program:wait-spinner",
                    "outcome": "FAILED",
                    "evidence": "clicked caret",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(ASCEND_SPINNER_CHAT, spinner_text)
        accessible_text = format_new_failure_chat(
            [
                {
                    "id": "ascend-new-program:accessible-name",
                    "outcome": "FAILED",
                    "evidence": "plus-exact locator accepted",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(ASCEND_ACCESSIBLE_NAME_CHAT, accessible_text)
        customer = run_customer_type_lob_scenario()
        self.assertTrue(customer["ok"], customer.get("evidence"))
        customer_text = format_new_failure_chat(
            [
                {
                    "id": "ascend-customer-type:lob",
                    "outcome": "FAILED",
                    "evidence": "left Commercial default",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(ASCEND_CUSTOMER_TYPE_CHAT, customer_text)
        timing = run_spinner_timing_scenario()
        self.assertTrue(timing["ok"], timing.get("evidence"))
        self.assertEqual(timing["id"], "ascend-new-program:spinner-timing")
        fee = run_agency_fee_default_scenario()
        self.assertTrue(fee["ok"], fee.get("evidence"))
        self.assertEqual(fee["id"], "ascend-create:agency-fee-default")
        too_soon = run_too_soon_zero_element_scenario()
        self.assertTrue(too_soon["ok"], too_soon.get("evidence"))
        self.assertEqual(too_soon["id"], "ascend-create:too-soon-zero-element")
        too_soon_text = format_new_failure_chat(
            [
                {
                    "id": "ascend-create:too-soon-zero-element",
                    "outcome": "FAILED",
                    "evidence": "0-element lookup accepted",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(ASCEND_TOO_SOON_CHAT, too_soon_text)
        timing_text = format_new_failure_chat(
            [
                {
                    "id": "ascend-new-program:spinner-timing",
                    "outcome": "FAILED",
                    "evidence": "seconds not logged",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(ASCEND_SPINNER_CHAT, timing_text)
        fee_text = format_new_failure_chat(
            [
                {
                    "id": "ascend-create:agency-fee-default",
                    "outcome": "FAILED",
                    "evidence": "default not logged",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(ASCEND_AGENCY_FEE_CHAT, fee_text)
        listbox = run_unique_listbox_option_scenario()
        self.assertTrue(listbox["ok"], listbox.get("evidence"))
        self.assertEqual(listbox["id"], "ascend-create:unique-listbox-option")
        listbox_text = format_new_failure_chat(
            [
                {
                    "id": "ascend-create:unique-listbox-option",
                    "outcome": "FAILED",
                    "evidence": "selector not unique",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(ASCEND_LISTBOX_CHAT, listbox_text)
        self.assertIn("38c0fa79", listbox_text)

    def test_hitl_resume_re_leases_same_job_and_does_not_retry_leftover(self):
        with durable_temporary_directory() as tmp:
            results = {
                item["id"]: item
                for item in run_hitl_resume_scenarios(work_dir=Path(tmp) / "hitl")
            }
        self.assertTrue(results["hitl-resume:re-lease-after-gateway-restart"]["ok"])
        self.assertTrue(results["hitl-resume:no-second-job"]["ok"])
        self.assertTrue(results["hitl-resume:no-retry-leftover-failed"]["ok"])
        self.assertIn("09d69760", results["hitl-resume:re-lease-after-gateway-restart"]["evidence"])

    def test_false_success_complete_prose_stays_unverified_and_names_class_in_chat(self):
        self.assertTrue(claims_unverified_destination_progress(I_DID_IT_PROSE))
        with durable_temporary_directory() as tmp:
            result = run_false_success_scenario(work_dir=Path(tmp) / "false-success")
        self.assertTrue(result["ok"])
        self.assertIn(result["outcome"], {"UNVERIFIED", "FAILED"})
        text = format_new_failure_chat(
            [
                {
                    "id": "false-success:complete-prose-zero-evidence",
                    "outcome": "COMPLETE",
                    "evidence": "I did it with 0 destination evidence",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(FALSE_SUCCESS_CHAT, text)
        self.assertIn("30777947", text)
        self.assertIn("c31f9c69", text)
        self.assertIn("UNVERIFIED or FAILED", text)
        self.assertNotIn("@robie", text.casefold())
        hitl_text = format_new_failure_chat(
            [
                {
                    "id": "hitl-resume:re-lease-after-gateway-restart",
                    "outcome": "FAILED",
                    "evidence": "second job started",
                }
            ],
            trigger="post-deploy",
        )
        self.assertIn(HITL_RESUME_CHAT, hitl_text)
        self.assertIn("Do not RETRY leftover failed ids", hitl_text)
        self.assertNotIn("@robie", hitl_text.casefold())

    def test_named_scenarios_refuse_live_hermes_and_stay_quiet_when_passing(self):
        with self.assertRaises(Exception):
            run_named_scenarios(
                work_dir=Path("/opt/streetsmart-hermes/robie-job-engine/data")
            )
        posted: list[str] = []
        opened: list[dict] = []
        with durable_temporary_directory() as tmp:
            report = run_regression_battery(
                trigger="post-deploy",
                notify=True,
                draft_pr=True,
                logic_runner=_logic_pass,
                replay_runner=lambda: run_named_scenarios(work_dir=Path(tmp) / "named"),
                parity_runner=lambda: [],
                poster=lambda space, text: posted.append(text),
                pr_opener=lambda payload: opened.append(payload),
            )
        self.assertTrue(report["ok"])
        self.assertEqual(posted, [])
        self.assertEqual(opened, [])
        self.assertNotIn("@robie", str(report).casefold())


if __name__ == "__main__":
    unittest.main()
