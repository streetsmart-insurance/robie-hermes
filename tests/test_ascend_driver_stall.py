"""Ascend driver allowlist, stall probe, and Prod unit interpreter."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from robie_job_engine import ascend_notice_driver as driver
from robie_job_engine import ascend_notice_triage as triage
from robie_job_engine.ascend_driver_stall import (
    append_run_record,
    compact_run_record,
    count_actionable,
    evaluate_stall,
    evaluate_stall_file,
)

ROOT = Path(__file__).resolve().parents[1]
PROD_MAILBOXES = (
    "carlo@streetsmart.insurance",
    "jake@streetsmart.insurance",
    "robie@streetsmart.insurance",
    "hello@streetsmart.insurance",
    "accounting@streetsmart.insurance",
    "sandy@streetsmart.insurance",
    "zeus@streetsmart.insurance",
    "certificates@streetsmart.insurance",
    "daniela@streetsmart.insurance",
    "angie@streetsmart.insurance",
    "ana@streetsmart.insurance",
    "jazmin@streetsmart.insurance",
    "jackie@streetsmart.insurance",
    "taylor@streetsmart.insurance",
    "steffany@streetsmart.insurance",
    "amber@streetsmart.insurance",
    "ashley@streetsmart.insurance",
    "jimmy@streetsmart.insurance",
    "matthew@streetsmart.insurance",
    "karla@streetsmart.insurance",
    "mitchell@streetsmart.insurance",
    "eimy@streetsmart.insurance",
    "alejandro@streetsmart.insurance",
    "mike@streetsmart.insurance",
    "andrea@streetsmart.insurance",
)
VENV_PYTHON = "/opt/streetsmart-hermes/venv/bin/python"
HERMES_PYTHON = "/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python"
STATE_DIR = "/var/lib/robie-ascend-notice-driver"


def _health():
    path = ROOT / "scripts" / "robie_health_check.py"
    spec = importlib.util.spec_from_file_location("robie_health_check_stall", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live(actionable: int, done: int = 0, **extra) -> dict:
    seen = extra.pop("notices_seen", actionable)
    record = {
        "dry_run": False,
        "notices_seen": seen,
        "done": done,
        "ignored": extra.pop("ignored", 0),
        "unrecognized": extra.pop("unrecognized", 0),
        "actionable_seen": actionable,
        "deduped": extra.pop("deduped", 0),
    }
    record.update(extra)
    return record


class MailboxAllowlistTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(
            os.environ,
            {
                "ASCEND_DRIVER_MAILBOX": "",
                "ASCEND_DRIVER_MAILBOXES": "",
                "ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES": "",
            },
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_default_is_the_prod_twenty_five(self):
        self.assertEqual(driver.DEFAULT_MAILBOXES, PROD_MAILBOXES)
        self.assertEqual(len(driver.DEFAULT_MAILBOXES), 25)
        self.assertEqual(driver.resolve_mailboxes(), list(PROD_MAILBOXES))
        self.assertEqual(
            driver.ALLOWED_MAILBOXES,
            frozenset(mailbox.casefold() for mailbox in PROD_MAILBOXES),
        )

    def test_staff_mailbox_is_allowed_and_outsider_is_refused(self):
        self.assertEqual(
            driver.resolve_mailboxes(mailbox="Certificates@StreetSmart.Insurance"),
            ["Certificates@StreetSmart.Insurance"],
        )
        with self.assertRaises(driver.MailboxAllowlistError) as caught:
            driver.resolve_mailboxes(
                mailboxes="carlo@streetsmart.insurance,outsider@example.com"
            )
        self.assertIn("outsider@example.com", str(caught.exception))

    def test_extra_mailbox_override_is_explicit(self):
        os.environ["ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES"] = "1"
        self.assertEqual(
            driver.resolve_mailboxes(mailbox="outsider@example.com"),
            ["outsider@example.com"],
        )


class SenderFilterTests(unittest.TestCase):
    def test_fixture_senders_are_exactly_the_allowlist(self):
        fixture_dir = ROOT / "tests" / "fixtures" / "ascend_notices"
        paths = sorted(fixture_dir.glob("*.json"))
        self.assertEqual(len(paths), 56)
        senders = set()
        for path in paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            senders.add(str(data["from"]).casefold())
        self.assertEqual(
            senders, {sender.casefold() for sender in driver.ASCEND_NOTICE_SENDERS}
        )
        self.assertEqual(
            driver.ascend_sender_filter(),
            "from:(no-reply@useascend.com OR accounting@useascend.com OR "
            "support@useascend.com)",
        )
        self.assertEqual(
            driver.DEFAULT_QUERY,
            "is:unread newer_than:2d " + driver.ascend_sender_filter(),
        )

    def test_unfiltered_and_non_ascend_queries_gain_the_clause(self):
        clause = driver.ascend_sender_filter()
        with patch.dict(os.environ, {"ASCEND_DRIVER_QUERY": "is:unread newer_than:2d"}):
            narrowed = driver.configured_query()
        self.assertEqual(narrowed, f"is:unread newer_than:2d {clause}")
        github = driver.configured_query("is:unread from:notifications@github.com")
        self.assertEqual(
            github, f"is:unread from:notifications@github.com {clause}"
        )
        mixed = driver.configured_query(
            "from:notifications@github.com OR from:support@useascend.com"
        )
        self.assertEqual(
            mixed,
            f"(from:notifications@github.com OR from:support@useascend.com) {clause}",
        )
        self.assertEqual(driver.ensure_ascend_sender_filter("  "), driver.DEFAULT_QUERY)
        self.assertEqual(driver.configured_query(driver.DEFAULT_QUERY), driver.DEFAULT_QUERY)

    def test_ascend_sender_and_domain_queries_stay_narrow(self):
        narrow = "is:unread from:accounting@useascend.com"
        self.assertEqual(driver.configured_query(narrow), narrow)
        domain = "is:unread newer_than:2d from:useascend.com"
        self.assertEqual(driver.configured_query(domain), domain)
        quoted = 'from:"support@useascend.com"'
        self.assertEqual(driver.configured_query(quoted), quoted)


class NoticeClassificationLockTests(unittest.TestCase):
    def test_journal_families_classify_and_refund_stays_ignored(self):
        expected = {
            "payment_confirmation_copy": triage.PAYMENT_CONFIRMATION,
            "refund_initiated": triage.REFUND,
            "refund_to_customer": triage.REFUND,
            "underwriting_request": triage.UNDERWRITING,
            "underwriting_counteroffer": triage.UNDERWRITING,
            "processing_payment": triage.PROCESSING_PAYMENT,
            "past_due_payment": triage.LATE_PAYMENT,
            "payment_failed": triage.LATE_PAYMENT,
            "intent_to_cancel_copy": triage.INTENT_TO_CANCEL,
        }
        fixture_dir = ROOT / "tests" / "fixtures" / "ascend_notices"
        seen: set[str] = set()
        for path in sorted(fixture_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            family = data["family"]
            if family not in expected:
                continue
            seen.add(family)
            notice_type = triage.classify_notice(data["subject"], data["text_plain"])
            self.assertEqual(notice_type, expected[family], path.name)
            self.assertNotEqual(notice_type, triage.UNKNOWN, path.name)
        self.assertEqual(seen, set(expected))
        self.assertIn(triage.REFUND, triage.IGNORE_TYPES)
        for filing in (
            triage.PAYMENT_CONFIRMATION,
            triage.UNDERWRITING,
            triage.PROCESSING_PAYMENT,
            triage.LATE_PAYMENT,
            triage.INTENT_TO_CANCEL,
        ):
            self.assertNotIn(filing, triage.IGNORE_TYPES)


class StallVerdictTests(unittest.TestCase):
    def test_ignored_and_unrecognized_are_not_actionable(self):
        summary = {
            "notices_seen": 4,
            "ignored": 1,
            "results": [
                {
                    "status": "ignored",
                    "reason": "ignored",
                    "detail": {"notice_type": "msa"},
                },
                {
                    "status": "skipped",
                    "reason": "needs_human_review: Unrecognized Ascend notice type",
                    "detail": {"notice_type": "unknown"},
                },
                {
                    "status": "skipped",
                    "reason": "applicant_unresolved: none",
                    "detail": {"notice_type": "cancellation"},
                },
                {
                    "status": "done",
                    "reason": "ok",
                    "detail": {"notice_type": "late_payment"},
                },
            ],
        }
        actionable, unrecognized = count_actionable(summary)
        self.assertEqual(unrecognized, 1)
        self.assertEqual(actionable, 2)

    def test_four_live_stalls_alert(self):
        verdict = evaluate_stall([_live(3) for _ in range(4)])
        self.assertEqual(verdict["status"], "ALERT")
        self.assertIn("done=0", verdict["detail"])
        self.assertEqual(verdict["runs_found"], 4)

    def test_success_on_the_latest_live_run_is_quiet(self):
        records = [_live(3) for _ in range(3)] + [_live(3, done=2)]
        verdict = evaluate_stall(records)
        self.assertEqual(verdict["status"], "OK")
        self.assertEqual(verdict["detail"], "ascend driver not stalled")

    def test_unrecognized_and_ignored_only_runs_are_quiet(self):
        records = [
            _live(0, notices_seen=8, ignored=3, unrecognized=5) for _ in range(4)
        ]
        verdict = evaluate_stall(records)
        self.assertEqual(verdict["status"], "OK")

    def test_dry_runs_and_fatal_starts_do_not_fill_the_window(self):
        records = [
            _live(4),
            _live(4),
            {"dry_run": True, "notices_seen": 9, "done": 0, "actionable_seen": 9},
            {"dry_run": False, "fatal": "RuntimeError: boom"},
            _live(4),
        ]
        verdict = evaluate_stall(records)
        self.assertEqual(verdict["status"], "OK")
        self.assertEqual(verdict["runs_found"], 3)
        self.assertIn("need 4", verdict["detail"])

    def test_fewer_than_four_live_runs_is_quiet(self):
        verdict = evaluate_stall([_live(2), _live(2)])
        self.assertEqual(verdict["status"], "OK")
        self.assertEqual(verdict["runs_found"], 2)

    def test_phrase_in_reason_key_is_excluded_and_bare_human_review_is_not(self):
        excluded = {
            "dry_run": False,
            "notices_seen": 4,
            "done": 0,
            "ignored": 0,
            "skipped_by_reason": {
                "needs_human_review: Unrecognized Ascend notice type": 4
            },
        }
        quiet = evaluate_stall([excluded for _ in range(4)])
        self.assertEqual(quiet["status"], "OK")
        self.assertEqual(quiet["last_runs"][0]["actionable_seen"], 0)
        still_actionable = {
            "dry_run": False,
            "notices_seen": 4,
            "done": 0,
            "ignored": 0,
            "skipped_by_reason": {"needs_human_review": 4},
        }
        alert = evaluate_stall([still_actionable for _ in range(4)])
        self.assertEqual(alert["status"], "ALERT")

    def test_already_filed_notices_are_healthy(self):
        # Tonight's first live shape: 6 seen, 4 already filed by the API
        # poller, 2 ignored, nothing new to post.
        tonight = _live(
            4,
            done=0,
            notices_seen=6,
            ignored=2,
            deduped=4,
            skipped_by_reason={"api_already_filed": 4},
        )
        verdict = evaluate_stall([tonight for _ in range(4)])
        self.assertEqual(verdict["status"], "OK")
        self.assertEqual(verdict["detail"], "ascend driver not stalled")
        self.assertEqual(verdict["last_runs"][0]["unhandled"], 0)

        mixed = {
            "dry_run": False,
            "notices_seen": 6,
            "done": 0,
            "ignored": 2,
            "actionable_seen": 4,
            "skipped_by_reason": {
                "api_already_filed": 1,
                "existing_note_duplicate": 1,
                "duplicate_in_run": 1,
                "recent_same_notice": 1,
            },
        }
        self.assertEqual(evaluate_stall([mixed for _ in range(4)])["status"], "OK")

    def test_unhandled_actionable_notices_still_fail(self):
        stalled = _live(
            4,
            done=0,
            notices_seen=6,
            ignored=2,
            deduped=0,
            skipped_by_reason={"applicant_unresolved": 4},
        )
        verdict = evaluate_stall([stalled for _ in range(4)])
        self.assertEqual(verdict["status"], "ALERT")
        self.assertIn("done=0", verdict["detail"])
        self.assertEqual(verdict["last_runs"][0]["unhandled"], 4)

        partial = _live(
            4,
            done=0,
            deduped=2,
            skipped_by_reason={"api_already_filed": 2, "applicant_unresolved": 2},
        )
        partial_verdict = evaluate_stall([partial for _ in range(4)])
        self.assertEqual(partial_verdict["status"], "ALERT")
        self.assertEqual(partial_verdict["last_runs"][0]["unhandled"], 2)

    def test_records_without_dedupe_counters_do_not_stay_red(self):
        # Written before deduped and skipped_by_reason. They look like a
        # stall and must not be judged, so they age out of the window.
        historical = {
            "dry_run": False,
            "notices_seen": 6,
            "done": 0,
            "ignored": 2,
            "actionable_seen": 4,
        }
        old = evaluate_stall([historical for _ in range(4)])
        self.assertEqual(old["status"], "OK")
        self.assertEqual(old["runs_found"], 0)
        self.assertEqual(old["unjudged"], 4)
        self.assertIn("need 4", old["detail"])

        # The same shape with the reason map already on disk is judged,
        # and api_already_filed makes it healthy without the new counter.
        recorded = dict(historical)
        recorded["skipped_by_reason"] = {"api_already_filed": 4}
        reinterpreted = evaluate_stall([recorded for _ in range(4)])
        self.assertEqual(reinterpreted["status"], "OK")
        self.assertEqual(reinterpreted["detail"], "ascend driver not stalled")
        self.assertEqual(reinterpreted["unjudged"], 0)

        # One new unhandled run does not alert while the window is short.
        # Four new unhandled runs do, and the old rows stay outside it.
        fresh = _live(4, done=0, deduped=0)
        short = evaluate_stall([historical for _ in range(4)] + [fresh])
        self.assertEqual(short["status"], "OK")
        self.assertEqual(short["runs_found"], 1)
        full = evaluate_stall([historical for _ in range(4)] + [fresh for _ in range(4)])
        self.assertEqual(full["status"], "ALERT")
        self.assertEqual(full["runs_found"], 4)
        self.assertEqual(full["unjudged"], 4)

    def test_compact_record_stores_the_deduped_counter(self):
        record = compact_run_record(
            {
                "dry_run": False,
                "notices_seen": 6,
                "done": 0,
                "ignored": 2,
                "actionable_seen": 4,
                "skipped_by_reason": {
                    "api_already_filed": 3,
                    "existing_note_duplicate: n1": 1,
                    "applicant_unresolved": 0,
                },
            }
        )
        self.assertEqual(record["deduped"], 4)
        self.assertNotIn("subject", record)

    def test_run_log_is_world_readable_under_a_tight_umask(self):
        previous = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "state", "runs.jsonl")
                with patch.dict(os.environ, {"ASCEND_DRIVER_RUN_LOG": path}):
                    written = append_run_record(_live(1, done=1))
                self.assertEqual(written, path)
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
                self.assertEqual(
                    stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode),
                    0o755,
                )
                last = os.path.join(os.path.dirname(path), "last-run.json")
                self.assertEqual(stat.S_IMODE(os.stat(last).st_mode), 0o644)
                record = json.loads(Path(last).read_text(encoding="utf-8"))
                self.assertNotIn("subject", record)
                self.assertNotIn("results", record)
                self.assertEqual(record["done"], 1)
        finally:
            os.umask(previous)

    def test_missing_log_is_quiet_and_unreadable_log_alerts(self):
        missing = evaluate_stall_file("/tmp/robie-ascend-stall-does-not-exist.jsonl")
        self.assertEqual(missing["status"], "OK")
        self.assertIn("not present", missing["detail"])
        with tempfile.TemporaryDirectory() as tmp:
            verdict = evaluate_stall_file(tmp)
        self.assertEqual(verdict["status"], "ALERT")
        self.assertIn("unreadable", verdict["detail"])

    def test_fatal_main_writes_a_record_without_counting_as_live(self):
        import logging
        from contextlib import redirect_stdout
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "ROBIE_ENV",
                    "ASCEND_DRIVER_LIVE",
                    "ASCEND_DRIVER_MAILBOX",
                    "ASCEND_DRIVER_MAILBOXES",
                    "ASCEND_DRIVER_ALLOW_EXTRA_MAILBOXES",
                    "ASCEND_DRIVER_RUN_LOG",
                }
            }
            env["ASCEND_DRIVER_STATE_DIR"] = tmp
            root_logger = logging.getLogger()
            previous_handlers = list(root_logger.handlers)
            previous_level = root_logger.level
            try:
                with patch.dict(os.environ, env, clear=True):
                    with redirect_stdout(StringIO()) as captured:
                        code = driver.main([])
            finally:
                for handler in list(root_logger.handlers):
                    if handler not in previous_handlers:
                        root_logger.removeHandler(handler)
                        handler.close()
                root_logger.setLevel(previous_level)
            self.assertEqual(code, 1)
            payload = json.loads(captured.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertIn("fatal", payload)
            records = [
                json.loads(line)
                for line in Path(tmp, "runs.jsonl").read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(len(records), 1)
        self.assertIn("fatal", records[0])
        self.assertNotIn("subject", records[0])
        self.assertEqual(evaluate_stall(records)["runs_found"], 0)


class WatchScriptTests(unittest.TestCase):
    def test_exit_codes_for_quiet_alert_and_unreadable(self):
        script = ROOT / "scripts" / "ascend_driver_done_watch.py"
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.jsonl"
            quiet = subprocess_run(script, missing)
            self.assertEqual(quiet.returncode, 0, quiet.stdout + quiet.stderr)
            self.assertIn('"status": "OK"', quiet.stdout)
            stalled = Path(tmp) / "runs.jsonl"
            stalled.write_text(
                "".join(json.dumps(_live(2)) + "\n" for _ in range(4)),
                encoding="utf-8",
            )
            alert = subprocess_run(script, stalled)
            self.assertEqual(alert.returncode, 2, alert.stdout + alert.stderr)
            self.assertIn("ALERT", alert.stdout)
            unreadable = subprocess_run(script, Path(tmp))
            self.assertEqual(unreadable.returncode, 1, unreadable.stdout + unreadable.stderr)


def subprocess_run(script: Path, log: Path):
    return subprocess.run(
        ["python3", str(script), "--log", str(log)],
        check=False,
        text=True,
        capture_output=True,
        cwd=str(ROOT),
    )


class HealthProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.health = _health()

    def test_probe_is_production_only_and_does_not_use_the_journal(self):
        names = [name for name, _fn in self.health.checks_for_profile("TEST")]
        self.assertNotIn("ascend_driver_stall", names)
        production = [name for name, _fn in self.health.CHECKS]
        self.assertIn("ascend_driver_stall", production)
        source = Path(self.health.__file__).read_text(encoding="utf-8")
        start = source.index("def check_ascend_driver_stall")
        end = source.index("def check_duplicate_guard")
        body = source[start:end].split('"""', 2)[-1]
        self.assertNotIn("journalctl", body)

    def test_probe_alerts_through_the_health_check_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs.jsonl")
            Path(path).write_text(
                "".join(json.dumps(_live(2)) + "\n" for _ in range(4)),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"ASCEND_DRIVER_RUN_LOG": path}):
                ok, detail, extra = self.health.check_ascend_driver_stall()
        self.assertFalse(ok)
        self.assertIn("done=0", detail)
        self.assertEqual(extra["status"], "ALERT")

    def test_probe_is_quiet_when_the_log_is_missing_or_not_actionable(self):
        with patch.dict(
            os.environ,
            {"ASCEND_DRIVER_RUN_LOG": "/tmp/robie-ascend-stall-missing.jsonl"},
        ):
            ok, detail, _extra = self.health.check_ascend_driver_stall()
        self.assertTrue(ok)
        self.assertIn("not present", detail)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs.jsonl")
            row = _live(0, notices_seen=6, ignored=1, unrecognized=5)
            Path(path).write_text(
                "".join(json.dumps(row) + "\n" for _ in range(4)),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"ASCEND_DRIVER_RUN_LOG": path}):
                ok, detail, _extra = self.health.check_ascend_driver_stall()
        self.assertTrue(ok)
        self.assertIn("not stalled", detail)


class UnitInterpreterTests(unittest.TestCase):
    def test_named_units_use_the_streetsmart_hermes_venv(self):
        units = {
            "deploy/systemd/robie-ascend-notice-driver.service": (
                f"ExecStart={VENV_PYTHON} -m robie_job_engine.ascend_notice_driver --due-days 2"
            ),
            "deploy/systemd/robie-production-preflight.service": (
                f"ExecStart={VENV_PYTHON} -m robie_job_engine.production_preflight"
            ),
            "deploy/systemd/robie-health-check.service": (
                f"ExecStart={VENV_PYTHON} /opt/streetsmart-hermes/releases/current/scripts/robie_health_check.py"
            ),
            "deploy/systemd/robie-health-digest.service": (
                f"ExecStart={VENV_PYTHON} /opt/streetsmart-hermes/releases/current/scripts/robie_health_check.py --daily-digest"
            ),
        }
        for rel, exec_start in units.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("User=streetsmart-hermes\n", text, rel)
            self.assertIn(exec_start, text, rel)
            self.assertNotIn(HERMES_PYTHON, text, rel)
        driver_unit = (ROOT / "deploy/systemd/robie-ascend-notice-driver.service").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("\n[Install]\n", driver_unit)
        self.assertNotIn("--live", driver_unit)
        self.assertIn(f"Environment=ASCEND_DRIVER_STATE_DIR={STATE_DIR}", driver_unit)
        self.assertIn(f"ReadWritePaths={STATE_DIR}", driver_unit)
        timer = (ROOT / "deploy/systemd/robie-ascend-notice-driver.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("OnBootSec=5min", timer)
        self.assertIn("OnActiveSec=15min", timer)
        self.assertIn("OnUnitActiveSec=15min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn(
            "Environment=ASCEND_DRIVER_NOTE_LEDGER="
            "/var/lib/robie-ascend-notice-driver/discussion-note-ledger.json",
            driver_unit,
        )
        example = (
            ROOT
            / "deploy/systemd/robie-ascend-notice-driver.service.d/30-live.conf.example"
        ).read_text(encoding="utf-8")
        self.assertIn("Environment=ASCEND_DRIVER_LIVE=1", example)
        self.assertFalse(
            (
                ROOT / "deploy/systemd/robie-ascend-notice-driver.service.d/30-live.conf"
            ).exists()
        )
        for rel in (
            "deploy/systemd/robie-health-check.service",
            "deploy/systemd/robie-health-digest.service",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(f"Environment=ASCEND_DRIVER_STATE_DIR={STATE_DIR}", text)


if __name__ == "__main__":
    unittest.main()
