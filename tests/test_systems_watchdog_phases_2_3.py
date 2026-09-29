"""Tests for the systems watchdog phases 2-3 probes in scripts/robie_health_check.py
and scripts/accountability_vm_health_probe.py.

Covers: both phone Gmail keys probed separately, login secret states,
applicant ingest freshness (30h warn / 36h fail), EOD drive delivery,
task verifier health, 4359 Tuesday proof, chat intake passthrough, and the
daily digest renderer.

No live credentials, no network, no box access — everything is mocked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "robie_health_check.py")
_PROBE_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts",
                             "accountability_vm_health_probe.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


h = _load(_SCRIPT, "robie_health_check")
probe = _load(_PROBE_SCRIPT, "accountability_vm_health_probe")


# ---------------------------------------------------------------------------
# Phone Gmail keys (both probed separately)
# ---------------------------------------------------------------------------

class PhoneGmailKeysTest(unittest.TestCase):
    def _fake_probe(self, results):
        """results: dict key_path -> (ok, detail)."""
        def fake(key_path):
            ok, detail = results.get(key_path, (True, "ok"))
            return ok, detail, {"key_path": key_path}
        return fake

    def test_both_ok(self):
        paths = [p for p, _ in h.PHONE_GMAIL_KEYS]
        with patch.object(h, "_probe_gmail_key",
                          self._fake_probe({p: (True, "ok") for p in paths})):
            ok, detail, extra = h.check_phone_gmail_keys()
        self.assertTrue(ok)
        self.assertEqual(len(extra["keys"]), 2)
        self.assertIn("primary", extra["keys"])
        self.assertIn("backup", extra["keys"])

    def test_primary_dead_backup_ok_still_fails(self):
        # The 2026-09-27 lesson: a dead primary must not be masked by the
        # live backup.
        primary, backup = [p for p, _ in h.PHONE_GMAIL_KEYS]
        with patch.object(h, "_probe_gmail_key", self._fake_probe({
            primary: (False, "gmail auth failed: RefreshError: invalid_grant"),
            backup: (True, "ok"),
        })):
            ok, detail, extra = h.check_phone_gmail_keys()
        self.assertFalse(ok)
        self.assertIn("primary", detail)
        self.assertTrue(extra["keys"]["backup"]["ok"])
        self.assertFalse(extra["keys"]["primary"]["ok"])

    def test_both_dead(self):
        paths = [p for p, _ in h.PHONE_GMAIL_KEYS]
        with patch.object(h, "_probe_gmail_key",
                          self._fake_probe({p: (False, "dead") for p in paths})):
            ok, detail, _ = h.check_phone_gmail_keys()
        self.assertFalse(ok)
        self.assertIn("primary", detail)
        self.assertIn("backup", detail)


# ---------------------------------------------------------------------------
# Login secret states
# ---------------------------------------------------------------------------

class LoginSecretStatesTest(unittest.TestCase):
    def test_ok_when_enabled(self):
        fake_report = {"result": "OK", "reason": "each watched secret has an ENABLED version",
                       "project": "p", "secrets": [
                           {"secret_id": "ezlynx-username", "enabled_versions": ["1"],
                            "newest_state": "ENABLED", "alert": False}]}
        fake_mod = type(sys)("fake_lsh")
        fake_mod.inspect_login_secrets = lambda: fake_report
        with patch.dict(sys.modules, {"robie_job_engine.login_secret_health": fake_mod}):
            # The check imports inside the function; patch the import path
            # by inserting the fake module into robie_job_engine.
            import types
            rje = types.ModuleType("robie_job_engine")
            rje.login_secret_health = fake_mod
            with patch.dict(sys.modules, {"robie_job_engine": rje}):
                ok, detail, extra = h.check_login_secret_states()
        self.assertTrue(ok)
        self.assertEqual(extra["result"], "OK")

    def test_alert_when_no_enabled(self):
        fake_report = {"result": "ALERT", "reason": "ezlynx-password has no ENABLED version",
                       "project": "p", "secrets": [
                           {"secret_id": "ezlynx-password", "enabled_versions": [],
                            "newest_state": "DESTROYED", "alert": True,
                            "missing_enabled": True}]}
        import types
        fake_mod = type(sys)("fake_lsh2")
        fake_mod.inspect_login_secrets = lambda: fake_report
        rje = types.ModuleType("robie_job_engine")
        rje.login_secret_health = fake_mod
        with patch.dict(sys.modules, {"robie_job_engine": rje,
                                      "robie_job_engine.login_secret_health": fake_mod}):
            ok, detail, extra = h.check_login_secret_states()
        self.assertFalse(ok)
        self.assertIn("no ENABLED version", detail)

    def test_unknown_does_not_page(self):
        fake_report = {"result": "UNKNOWN", "reason": "secret manager list failed",
                       "project": "p", "secrets": []}
        import types
        fake_mod = type(sys)("fake_lsh3")
        fake_mod.inspect_login_secrets = lambda: fake_report
        rje = types.ModuleType("robie_job_engine")
        rje.login_secret_health = fake_mod
        with patch.dict(sys.modules, {"robie_job_engine": rje,
                                      "robie_job_engine.login_secret_health": fake_mod}):
            ok, detail, extra = h.check_login_secret_states()
        self.assertTrue(ok)  # UNKNOWN != page
        self.assertIn("UNKNOWN", detail)


# ---------------------------------------------------------------------------
# Applicant ingest freshness
# ---------------------------------------------------------------------------

class ApplicantIngestFreshnessTest(unittest.TestCase):
    def _touch(self, d, age_hours):
        p = os.path.join(d, "applicant_phone_match_export.xls")
        with open(p, "w") as f:
            f.write("x")
        ts = time.time() - age_hours * 3600
        os.utime(p, (ts, ts))
        return p

    def test_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._touch(d, 2)
            with patch.object(h, "APPLICANT_EXPORT_PATH", p):
                ok, detail, extra = h.check_applicant_ingest_freshness()
        self.assertTrue(ok)
        self.assertNotIn("WARNING", detail)
        self.assertAlmostEqual(extra["age_hours"], 2, delta=0.2)

    def test_warn_at_30h(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._touch(d, 32)
            with patch.object(h, "APPLICANT_EXPORT_PATH", p):
                ok, detail, extra = h.check_applicant_ingest_freshness()
        self.assertTrue(ok)  # warn, don't page
        self.assertIn("WARNING", detail)
        self.assertTrue(extra.get("warning"))

    def test_fail_at_36h(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._touch(d, 40)
            with patch.object(h, "APPLICANT_EXPORT_PATH", p):
                ok, detail, extra = h.check_applicant_ingest_freshness()
        self.assertFalse(ok)
        self.assertIn("failing closed", detail)

    def test_missing_file_fails(self):
        with patch.object(h, "APPLICANT_EXPORT_PATH", "/nonexistent/x.xls"):
            ok, detail, _ = h.check_applicant_ingest_freshness()
        self.assertFalse(ok)
        self.assertIn("missing", detail)


# ---------------------------------------------------------------------------
# EOD drive delivery
# ---------------------------------------------------------------------------

class EodDriveDeliveryTest(unittest.TestCase):
    def test_today_present_drive_unverified(self):
        today = datetime.now().strftime("%Y%m%d")
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, f"eod_phone_report_{today}.xlsx"), "w").close()
            open(os.path.join(d, f"eod_phone_leakage_{today}.md"), "w").close()
            with patch.object(h, "EOD_OUTPUT_DIR", d):
                ok, detail, extra = h.check_eod_drive_delivery()
        # Weekday-dependent: only assert the UNVERIFIED flag when expected.
        if extra["expected_today"]:
            self.assertTrue(ok)
            self.assertIn("UNVERIFIED", detail)
            self.assertEqual(extra["drive_delivery"], "UNVERIFIED")
            self.assertTrue(extra["xlsx_present"])

    def test_today_missing_fails_on_weekday(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(h, "EOD_OUTPUT_DIR", d):
                ok, detail, extra = h.check_eod_drive_delivery()
        if extra["expected_today"]:
            self.assertFalse(ok)
            self.assertIn("missing", detail)
        else:
            self.assertTrue(ok)  # weekend: no run expected

    def test_missing_dir_fails_on_weekday(self):
        with patch.object(h, "EOD_OUTPUT_DIR", "/nonexistent/eod"):
            ok, detail, extra = h.check_eod_drive_delivery()
        if extra["expected_today"]:
            self.assertFalse(ok)
        else:
            self.assertTrue(ok)


# ---------------------------------------------------------------------------
# Task verifier health
# ---------------------------------------------------------------------------

class TaskVerifierHealthTest(unittest.TestCase):
    def _db(self, d, rows):
        p = os.path.join(d, "pending.db")
        conn = sqlite3.connect(p)
        conn.execute("""CREATE TABLE pending_tasks (
            id INTEGER PRIMARY KEY, producer TEXT, applicant_id TEXT,
            title TEXT, assignee TEXT, fired_at TEXT, status TEXT,
            detail TEXT, created_at TEXT, resolved_at TEXT)""")
        for status, age_h in rows:
            created = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()
            conn.execute(
                "INSERT INTO pending_tasks (producer, applicant_id, title, assignee,"
                " fired_at, status, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("p", "a", "t", "as", created, status, "", created))
        conn.commit()
        conn.close()
        return p

    def test_healthy_no_stuck(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._db(d, [("PENDING", 0.5), ("PENDING", 1)])
            with patch.dict(os.environ, {"ROBIE_TASK_VERIFY_DB": p},
                             clear=False), \
                 patch.object(h, "_journal_since", return_value=""):
                ok, detail, extra = h.check_task_verifier_health()
        self.assertTrue(ok)
        self.assertEqual(extra["stuck_count"], 0)

    def test_stuck_tasks_fail(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._db(d, [("PENDING", 5), ("UNVERIFIED", 3)])
            with patch.dict(os.environ, {"ROBIE_TASK_VERIFY_DB": p},
                             clear=False), \
                 patch.object(h, "_journal_since", return_value=""):
                ok, detail, extra = h.check_task_verifier_health()
        self.assertFalse(ok)
        self.assertEqual(extra["stuck_count"], 2)
        self.assertIn("stuck", detail)

    def test_journal_traceback_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._db(d, [])
            journal = "Traceback (most recent call last):\n  OperationalError: unable to open database file"
            with patch.dict(os.environ, {"ROBIE_TASK_VERIFY_DB": p},
                             clear=False), \
                 patch.object(h, "_journal_since", return_value=journal):
                ok, detail, _ = h.check_task_verifier_health()
        self.assertFalse(ok)
        self.assertIn("Traceback", detail)

    def test_missing_db_fails(self):
        with patch.dict(os.environ, {"ROBIE_TASK_VERIFY_DB": "/nonexistent/v.db"},
                         clear=False):
            ok, detail, _ = h.check_task_verifier_health()
        self.assertFalse(ok)
        self.assertIn("unreadable", detail)


# ---------------------------------------------------------------------------
# 4359 Tuesday proof
# ---------------------------------------------------------------------------

class Tuesday4359ProofTest(unittest.TestCase):
    def _evidence(self, d, payload):
        p = os.path.join(d, "evidence-latest.json")
        with open(p, "w") as f:
            json.dump(payload, f)
        return p

    def _last_tuesday_0900_et(self):
        # Most recent Tuesday 09:00 ET as UTC.
        now = datetime.now(timezone.utc)
        # ET is UTC-4 (EDT) in this window; use a fixed -4 offset.
        et = now - timedelta(hours=4)
        days_back = (et.weekday() - 1) % 7
        tue_et = (et - timedelta(days=days_back)).replace(hour=9, minute=5,
                                                          second=0, microsecond=0)
        if days_back == 0 and et.hour < 9:
            tue_et -= timedelta(days=7)
        return (tue_et + timedelta(hours=4)).isoformat()

    def test_sent_proof_ok(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._evidence(d, {
                "ran_at": self._last_tuesday_0900_et(),
                "succeeded": True,
                "summary": {"sent": 3},
            })
            with patch.object(h, "EVIDENCE_4359_PATH", p):
                ok, detail, extra = h.check_4359_tuesday_proof()
        self.assertTrue(ok)
        self.assertIn("3 email(s) sent", detail)

    def test_zero_with_reason_ok(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._evidence(d, {
                "ran_at": self._last_tuesday_0900_et(),
                "succeeded": True,
                "summary": {"sent": 0},
                "hold_status": "all rows held: unverified liveness",
            })
            with patch.object(h, "EVIDENCE_4359_PATH", p):
                ok, detail, extra = h.check_4359_tuesday_proof()
        self.assertTrue(ok)
        self.assertIn("0 sent, reason given", detail)

    def test_zero_without_reason_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._evidence(d, {
                "ran_at": self._last_tuesday_0900_et(),
                "succeeded": True,
                "summary": {"sent": 0},
            })
            with patch.object(h, "EVIDENCE_4359_PATH", p):
                ok, detail, _ = h.check_4359_tuesday_proof()
        self.assertFalse(ok)
        self.assertIn("no reason", detail)

    def test_stale_evidence_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._evidence(d, {
                "ran_at": "2026-09-01T13:00:00+00:00",
                "succeeded": True,
                "summary": {"sent": 5},
            })
            with patch.object(h, "EVIDENCE_4359_PATH", p):
                ok, detail, _ = h.check_4359_tuesday_proof()
        self.assertFalse(ok)
        self.assertIn("stale", detail)

    def test_missing_evidence_fails(self):
        with patch.object(h, "EVIDENCE_4359_PATH", "/nonexistent/e.json"):
            ok, detail, _ = h.check_4359_tuesday_proof()
        self.assertFalse(ok)
        self.assertIn("missing", detail)


# ---------------------------------------------------------------------------
# Chat intake (passthrough to preflight)
# ---------------------------------------------------------------------------

class ChatIntakeTest(unittest.TestCase):
    def test_ok_passthrough(self):
        import types
        fake_preflight = types.ModuleType("robie_job_engine.production_preflight")
        fake_preflight.check_chat_intake = lambda: {
            "name": "chat-intake", "ok": True,
            "evidence": "last Chat inbound 2026-09-28T12:00:00+00:00 (60s ago)"}
        rje = types.ModuleType("robie_job_engine")
        rje.production_preflight = fake_preflight
        with patch.dict(sys.modules, {"robie_job_engine": rje,
                                      "robie_job_engine.production_preflight": fake_preflight}):
            ok, detail, _ = h.check_chat_intake()
        self.assertTrue(ok)
        self.assertIn("inbound", detail)

    def test_silent_listener_fails(self):
        import types
        fake_preflight = types.ModuleType("robie_job_engine.production_preflight")
        fake_preflight.check_chat_intake = lambda: {
            "name": "chat-intake", "ok": False,
            "evidence": "hermes-gateway.service active; Chat listener silent "
                        "(last inbound 2026-09-23T12:43:37+00:00)"}
        rje = types.ModuleType("robie_job_engine")
        rje.production_preflight = fake_preflight
        with patch.dict(sys.modules, {"robie_job_engine": rje,
                                      "robie_job_engine.production_preflight": fake_preflight}):
            ok, detail, _ = h.check_chat_intake()
        self.assertFalse(ok)
        self.assertIn("silent", detail)


# ---------------------------------------------------------------------------
# Daily digest rendering
# ---------------------------------------------------------------------------

class DailyDigestTest(unittest.TestCase):
    def _results(self, n_ok=3, failures=(), warnings=()):
        ts = datetime.now(timezone.utc).isoformat()
        rs = [{"name": f"check_{i}", "ok": True, "detail": f"fine {i}",
               "extra": {}, "at": ts} for i in range(n_ok)]
        for name, detail in failures:
            rs.append({"name": name, "ok": False, "detail": detail,
                       "extra": {}, "at": ts})
        for name, detail in warnings:
            rs.append({"name": name, "ok": True, "detail": detail,
                       "extra": {"warning": True}, "at": ts})
        return rs

    def test_all_green_digest_posts(self):
        sent_bodies = []

        def fake_post(req, timeout=15):
            sent_bodies.append(json.loads(req.data.decode()))
            class R:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()

        with patch.dict(os.environ, {"ROBIE_GOOGLE_CHAT_WEBHOOK_URL": "https://x"}), \
             patch.object(h.urllib.request, "urlopen", fake_post):
            sent = h.send_daily_digest(self._results())
        self.assertTrue(sent)
        self.assertIn("all 3 checks green", sent_bodies[0]["text"])

    def test_failure_digest_lists_failures(self):
        sent_bodies = []

        def fake_post(req, timeout=15):
            sent_bodies.append(json.loads(req.data.decode()))
            class R:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()

        with patch.dict(os.environ, {"ROBIE_GOOGLE_CHAT_WEBHOOK_URL": "https://x"}), \
             patch.object(h.urllib.request, "urlopen", fake_post):
            sent = h.send_daily_digest(
                self._results(failures=[("chat_intake", "listener silent")]))
        self.assertTrue(sent)
        self.assertIn("1 issue(s)", sent_bodies[0]["text"])
        self.assertIn("chat_intake", sent_bodies[0]["text"])

    def test_warning_surfaced_in_green_digest(self):
        sent_bodies = []

        def fake_post(req, timeout=15):
            sent_bodies.append(json.loads(req.data.decode()))
            class R:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()

        with patch.dict(os.environ, {"ROBIE_GOOGLE_CHAT_WEBHOOK_URL": "https://x"}), \
             patch.object(h.urllib.request, "urlopen", fake_post):
            sent = h.send_daily_digest(self._results(
                warnings=[("applicant_ingest_freshness", "32h old — WARNING")]))
        self.assertTrue(sent)
        body = sent_bodies[0]["text"]
        self.assertIn("all", body)
        self.assertIn("applicant_ingest_freshness", body)

    def test_no_webhook_no_send(self):
        with patch.dict(os.environ, {}, clear=True):
            # Ensure the webhook var is absent.
            os.environ.pop("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", None)
            sent = h.send_daily_digest(self._results())
        self.assertFalse(sent)


# ---------------------------------------------------------------------------
# Accountability VM probe
# ---------------------------------------------------------------------------

class AccountabilityProbeTest(unittest.TestCase):
    def test_report_delivery_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "report-2026-09-28.pdf")
            with open(p, "w") as f:
                f.write("x")
            with patch.object(probe, "REPORT_SEARCH_DIRS", [d]):
                ok, detail, extra = probe.check_report_delivery()
        self.assertTrue(ok)
        self.assertIn("report delivered", detail)

    def test_report_delivery_stale(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "old.pdf")
            with open(p, "w") as f:
                f.write("x")
            ts = time.time() - 30 * 3600
            os.utime(p, (ts, ts))
            with patch.object(probe, "REPORT_SEARCH_DIRS", [d]):
                ok, detail, extra = probe.check_report_delivery()
        self.assertFalse(ok)
        self.assertIn("no accountability report", detail)

    def test_report_delivery_none_found(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(probe, "REPORT_SEARCH_DIRS", [d]):
                ok, detail, _ = probe.check_report_delivery()
        self.assertFalse(ok)

    def test_disk_ok(self):
        ok, detail, extra = probe.check_disk()
        self.assertTrue(ok)
        self.assertIn("disk OK", detail)

    def test_main_json_shape(self):
        # main() prints JSON to stdout; verify the shape via the CHECKS list.
        self.assertEqual([n for n, _ in probe.CHECKS],
                         ["service_states", "timer_states",
                          "report_delivery", "disk"])


if __name__ == "__main__":
    unittest.main()
