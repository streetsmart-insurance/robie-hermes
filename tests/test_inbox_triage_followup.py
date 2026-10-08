#!/usr/bin/env python3
"""Tests for inbox_triage_followup.py (inbox triage follow-up enrichment).

Fixtures are real 2026-09-22 Sonant call-analysis emails from
carlo@streetsmart.insurance (message ids 1a0c920af059657c,
1a0c924c267e52c7).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "robie_job_engine"))
import inbox_triage_followup as itfu

# Parsed plain text of Sonant "Unscheduled Callback" email
# (gmail 1a0c920af059657c, 2026-09-22 12:39:17 UTC).
SONANT_CALLBACK = """Call Analysis
Date, Time
Sep 22, 2026, 8:38 AM EDT
Duration
0:00
Direction
Inbound
Client Phone
+18484487679
AMS Account #
35246235
Account Name
Kevin Hill Plumbing & Heating
Address
13 KEATS DR, BAYVILLE, NJ, 08721
Producer
Taylor Cimei
CSR
Jackie Arriola
Ams Source
ezlynx
Analysis
Summary
Caller asked to speak with Hannah Williams, but the office was closed. The agent collected the caller's name, Kevin Hill, and informed him that a licensed agent would call back during business hours.
Class Of Call
others
Next Step Actions
return call during business hours; confirm preferred callback window; route message to appropriate agent/team for Hannah Williams request
Type Of Call
Speak to Specific Person
Call Outcome
Unscheduled Callback
Personal Or Commercial
Not disclosed"""

# Parsed plain text of Sonant "Other" email where the caller hung up at the
# greeting with no request (gmail 1a0c924c267e52c7, 2026-09-22 12:43:44 UTC).
SONANT_NO_CONTACT = """Call Analysis
Date, Time
Sep 22, 2026, 8:43 AM EDT
Duration
0:00
Direction
Inbound
Client Phone
+13322755709
Analysis
Summary
The call reached the StreetSmart Insurance greeting message at 8:43 AM ET, which is before office opening hours. No caller response or request was made, so no service need, policy details, or follow-up action was captured.
Class Of Call
others
Type Of Call
Other
Call Outcome
Other"""


class SonantParseTest(unittest.TestCase):
    def test_callback_parses_all_fields(self):
        p = itfu.parse_sonant_text(SONANT_CALLBACK)
        self.assertEqual(p["client_phone"], "+18484487679")
        self.assertEqual(p["client_phone_normalized"], "8484487679")
        self.assertEqual(p["ams_account"], "35246235")
        self.assertEqual(p["account_name"], "Kevin Hill Plumbing & Heating")
        self.assertEqual(p["producer"], "Taylor Cimei")
        self.assertEqual(p["csr"], "Jackie Arriola")
        self.assertEqual(p["call_outcome"], "Unscheduled Callback")
        self.assertTrue(p["actionable"])
        self.assertTrue(p["summary"].startswith("Caller asked to speak with Hannah Williams"))

    def test_no_request_not_actionable(self):
        p = itfu.parse_sonant_text(SONANT_NO_CONTACT)
        self.assertEqual(p["client_phone_normalized"], "3322755709")
        self.assertFalse(p["actionable"])
        self.assertIn("no request", p["reason"])

    def test_missing_phone_not_actionable(self):
        p = itfu.parse_sonant_text("Call Analysis\nDate, Time\nSep 22, 2026, 8:43 AM EDT")
        self.assertFalse(p["actionable"])
        self.assertIn("no caller phone", p["reason"])

    def test_sonant_task_body_plain_english(self):
        p = itfu.parse_sonant_text(SONANT_CALLBACK)
        body = itfu.sonant_task_body(p)
        self.assertIn("Kevin Hill Plumbing & Heating", body)
        self.assertIn("8484487679", body)
        self.assertIn("Hannah Williams", body)
        ok, reason = itfu.validate_summary(body)
        self.assertTrue(ok, reason)


class PhoneMatchTest(unittest.TestCase):
    DIRECTORY = ("applicant_id,account_name,phone_cell,phone_home,phone_work\n"
                 "96917513,Lei Mechanical LLC,7326855762,,\n"
                 "150698361,LK Home Improvement LLC,,2018934168,\n")

    def _dir(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        f.write(self.DIRECTORY)
        f.close()
        return f.name

    def test_hit_on_home_phone(self):
        row = itfu.match_phone_to_applicant("(201) 893-4168", self._dir())
        self.assertIsNotNone(row)
        self.assertEqual(row["applicant_id"], "150698361")

    def test_miss_returns_none(self):
        self.assertIsNone(itfu.match_phone_to_applicant("8484487679", self._dir()))

    def test_normalize(self):
        self.assertEqual(itfu.normalize_phone("+18484487679"), "8484487679")
        self.assertEqual(itfu.normalize_phone("917-596-7809"), "9175967809")
        self.assertEqual(itfu.normalize_phone("not a number"), "")


class PayloadTest(unittest.TestCase):
    def _good(self):
        return dict(applicant_id="222730378",
                    task_title="Call back Lou about personal-residence quote",
                    task_body=("Lou wrote asking for a personal-residence quote callback.\n"
                               "Callback: 9175967809.\n"
                               "His multifamily at 38 Barkalow is insured through FMI."),
                    assignee="Carlo1",
                    email_subject="Personal Residence insurance",
                    due_date="2026-09-23")

    def test_valid_payload_assembles(self):
        payload, err = itfu.assemble_task_payload(**self._good())
        self.assertEqual(err, "")
        self.assertEqual(payload["applicant_id"], "222730378")
        self.assertEqual(payload["due_date"], "2026-09-23")
        self.assertIn("task_notes", payload)

    def test_note_text_compat_key_matches_task_notes(self):
        # Regression: the live Zap maps the webhook's note_text field to the
        # EZLynx description (shared with the phone watchdog). The payload
        # must carry the same summary under both keys so no Zap edit is needed.
        payload, err = itfu.assemble_task_payload(**self._good())
        self.assertEqual(err, "")
        self.assertIn("note_text", payload)
        self.assertEqual(payload["note_text"], payload["task_notes"])
        self.assertTrue(payload["note_text"].strip())

    def test_due_date_required(self):
        kw = self._good()
        kw["due_date"] = ""
        payload, err = itfu.assemble_task_payload(**kw)
        self.assertIsNone(payload)
        self.assertIn("due_date", err)

    def test_placeholder_applicant_refused(self):
        kw = self._good()
        kw["applicant_id"] = "199000001"
        payload, err = itfu.assemble_task_payload(**kw)
        self.assertIsNone(payload)
        self.assertIn("placeholder", err)

    def test_jargon_body_refused(self):
        kw = self._good()
        kw["task_body"] = "Fire the webhook payload via the API.\nSecond line here."
        payload, err = itfu.assemble_task_payload(**kw)
        self.assertIsNone(payload)
        self.assertIn("jargon", err)

    def test_empty_body_refused(self):
        kw = self._good()
        kw["task_body"] = "   "
        payload, err = itfu.assemble_task_payload(**kw)
        self.assertIsNone(payload)


class GateTest(unittest.TestCase):
    def _pending(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump([{
            "task_id": "itfu-20260922-001",
            "gmail_id": "1a0bf2aaedadb988",
            "source": "inbox-triage",
            "applicant_id": "222730378",
            "task_title": "Call back Lou about personal-residence quote",
            "task_notes": "Lou wrote asking for a personal-residence quote callback.\nCallback: 9175967809.",
            "assignee": "Carlo1",
            "email_subject": "Personal Residence insurance",
            "due_date": "2026-09-23",
            "status": "pending",
        }], f)
        f.close()
        return f.name

    def test_gate_refuses_without_approval(self):
        r = itfu.preview_fire(self._pending(), "itfu-20260922-001", "")
        self.assertFalse(r["ok"])
        self.assertIn("approval required", r["error"])

    def test_gate_refuses_unknown_task(self):
        r = itfu.preview_fire(self._pending(), "nope", "Carlo")
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])

    def test_gate_approved_runs_dry_run_only(self):
        r = itfu.preview_fire(self._pending(), "itfu-20260922-001", "Carlo Ferrara")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["approved_by"], "Carlo Ferrara")
        self.assertTrue(r["dry_run"]["dry_run"])
        self.assertIn("--applicant-verified", r["live_command"])
        self.assertIn("LIVE FIRE NOT PERFORMED", r["note"])


if __name__ == "__main__":
    unittest.main()
