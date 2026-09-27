"""Tests for the server-wide watchdog checks in scripts/robie_health_check.py.

Covers the 2026-09-27 incident response: dead Gmail SA key detection,
service-key resolution order, journal error scanning, and sweep freshness.

No live credentials, no network, no box access — everything is mocked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "robie_health_check.py")


def _load():
    spec = importlib.util.spec_from_file_location("robie_health_check", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


h = _load()


class ResolveServiceKeyTest(unittest.TestCase):
    def _write(self, d, name, content):
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return p

    def test_dropin_wins_over_base(self):
        with tempfile.TemporaryDirectory() as d:
            dropin = self._write(d, "override.conf",
                "[Service]\nExecStart=/usr/bin/python app.py --service-key /keys/live.json\n")
            base = self._write(d, "base.service",
                "[Service]\nExecStart=/usr/bin/python app.py --service-key /keys/dead.json\n")
            with patch.object(h, "WATCHDOG_UNIT_PATHS", [dropin, base]):
                self.assertEqual(h._resolve_watchdog_service_key(), "/keys/live.json")

    def test_falls_back_to_default_when_no_units(self):
        with patch.object(h, "WATCHDOG_UNIT_PATHS", []):
            self.assertEqual(h._resolve_watchdog_service_key(), h.WATCHDOG_DEFAULT_KEY)

    def test_skips_unreadable_paths(self):
        with patch.object(h, "WATCHDOG_UNIT_PATHS", ["/nonexistent/x.conf"]):
            self.assertEqual(h._resolve_watchdog_service_key(), h.WATCHDOG_DEFAULT_KEY)


class GmailSaKeyTest(unittest.TestCase):
    def _key_file(self, d, payload):
        p = os.path.join(d, "key.json")
        with open(p, "w") as f:
            json.dump(payload, f)
        return p

    def test_missing_key_file_fails(self):
        with patch.object(h, "_resolve_watchdog_service_key", return_value="/nonexistent/key.json"):
            ok, detail, extra = h.check_gmail_sa_key()
        self.assertFalse(ok)
        self.assertIn("not found", detail)

    def test_invalid_key_json_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._key_file(d, {"not": "a-key"})
            with patch.object(h, "_resolve_watchdog_service_key", return_value=p):
                ok, detail, extra = h.check_gmail_sa_key()
        self.assertFalse(ok)
        self.assertIn("not a valid SA key", detail)

    def test_auth_failure_reports_type_not_secret(self):
        # Simulates the 2026-09-27 invalid_grant: must FAIL loudly, never pass.
        with tempfile.TemporaryDirectory() as d:
            p = self._key_file(d, {"client_email": "x@y.iam.gserviceaccount.com",
                                   "private_key": "fake"})
            with patch.object(h, "_resolve_watchdog_service_key", return_value=p), \
                 patch.dict(sys.modules, {"google.oauth2.service_account": None,
                                          "googleapiclient.discovery": None}):
                # Force the import to fail → caught as auth failure
                ok, detail, extra = h.check_gmail_sa_key()
        self.assertFalse(ok)
        self.assertEqual(extra["client_email"], "x@y.iam.gserviceaccount.com")
        # No private key material in the detail
        self.assertNotIn("fake", detail)


class ServiceErrorScanTest(unittest.TestCase):
    def test_invalid_grant_in_journal_fails(self):
        log = "Sep 27 09:19:29 watchdog: invalid_grant: SignatureException: Invalid signature\n"
        with patch.object(h, "_journal_since", return_value=log):
            ok, detail, extra = h.check_service_errors()
        self.assertFalse(ok)
        self.assertIn("invalid_grant", detail)
        self.assertIn("streetsmart-phone-watchdog.service", detail)

    def test_clean_journal_passes(self):
        log = "Sep 27 11:22:01 watchdog: Sweep complete. Alerted 0 new phone incidents.\n"
        with patch.object(h, "_journal_since", return_value=log):
            ok, detail, extra = h.check_service_errors()
        self.assertTrue(ok)


class SweepFreshnessTest(unittest.TestCase):
    def _show(self, state="active", trigger="Sun 2026-09-27 11:00:00 EDT"):
        m = unittest.mock.Mock()
        m.stdout = f"ActiveState={state}\nLastTriggerUSec={trigger}\n"
        return m

    def test_stale_timer_fails(self):
        old = "Sat 2026-09-20 17:00:00 EDT"  # a week ago
        with patch.object(h.subprocess, "run") as run, \
             patch.object(h, "_journal_since", return_value="Sweep complete"):
            run.side_effect = [
                unittest.mock.Mock(stdout="active\n"),  # is-active
                self._show("active", old),              # eod timer
                self._show("active", old),              # 4359 timer
                self._show("active", old),              # hourly-missed timer
            ]
            ok, detail, extra = h.check_sweep_freshness()
        self.assertFalse(ok)
        self.assertIn("EOD phone report", detail)

    def test_inactive_service_fails(self):
        with patch.object(h.subprocess, "run") as run:
            run.return_value = unittest.mock.Mock(
                stdout="ActiveState=inactive\nLastTriggerUSec=n/a\n"
            )
            ok, detail, extra = h.check_sweep_freshness()
        self.assertFalse(ok)
        self.assertIn("not active", detail)


if __name__ == "__main__":
    unittest.main()
