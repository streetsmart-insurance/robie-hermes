"""All-clients write scope stays closed unless Playground guardrails are on."""

from __future__ import annotations

import importlib
import logging
import os
import unittest
from pathlib import Path
from unittest import mock

from durable_temp import durable_temporary_directory

from robie_job_engine.ezlynx_write_scope import (
    ROBIE_EZLYNX_WRITE_SCOPE_ENV_VAR,
    TEST_EZLYNX_WRITE_APPLICANT_ID,
    applicant_is_write_allowed,
    describe_write_scope,
    log_write_scope_at_startup,
    write_scope_requests_all,
)
from robie_job_engine.playground_config import buster_brown_only_mode, carrier_must_redirect
from robie_job_engine.playground_guardrails import Proposal, classify_playground_request
from robie_job_engine.playground_service import handle_playground_chat
from robie_job_engine.playground_undo import list_writes_since
from robie_job_engine.store import JobStore
from tests.test_playground import SPACE, WHEN, Reader, Writer, _env
from datetime import timedelta


REAL = (
    "Email the carrier at changes@progressive.com to request a policy change "
    "for Real Client applicant 999000111."
)


class AllClientsTests(unittest.TestCase):
    def test_scope_all_is_closed_until_guardrails_are_active(self):
        with mock.patch.dict(os.environ, {"ROBIE_EZLYNX_WRITE_SCOPE": "all"}, clear=False):
            os.environ.pop("ROBIE_PLAYGROUND", None)
            self.assertTrue(write_scope_requests_all())
            self.assertFalse(applicant_is_write_allowed("999000111"))
            self.assertTrue(applicant_is_write_allowed(TEST_EZLYNX_WRITE_APPLICANT_ID))
            self.assertIn("Falling back", describe_write_scope())
            with self.assertLogs("robie_job_engine.ezlynx_write_scope", level=logging.WARNING) as logs:
                self.assertIn("Falling back", log_write_scope_at_startup())
            self.assertTrue(any("Falling back" in line for line in logs.output))

    def test_scope_all_opens_real_clients_only_while_guardrails_hold(self):
        with mock.patch.dict(os.environ, _env(ROBIE_EZLYNX_WRITE_SCOPE="all"), clear=False):
            self.assertTrue(applicant_is_write_allowed("999000111"))
            self.assertIn("all clients", describe_write_scope())
            with mock.patch("robie_job_engine.playground_undo.APPEND_ONLY_TRIGGERS", ()):
                self.assertFalse(applicant_is_write_allowed("999000111"))
                self.assertIn("Falling back", describe_write_scope())
            self.assertTrue(applicant_is_write_allowed("999000111"))

    def test_star_allowlist_is_the_same_request_and_still_falls_back(self):
        import robie_job_engine.ezlynx_write_scope as scope

        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR, None)
                os.environ.pop(ROBIE_EZLYNX_WRITE_SCOPE_ENV_VAR, None)
                os.environ.pop("ROBIE_PLAYGROUND", None)
                os.environ[scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR] = "*"
                mod = importlib.reload(scope)
                self.assertEqual(
                    mod.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS,
                    frozenset({TEST_EZLYNX_WRITE_APPLICANT_ID}),
                )
                self.assertTrue(mod.write_scope_requests_all())
                self.assertFalse(mod.applicant_is_write_allowed("999000111"))
                os.environ[scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR] = "26356199,*"
                mod = importlib.reload(scope)
                self.assertEqual(mod.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS, frozenset({"26356199"}))
                self.assertTrue(mod.applicant_is_write_allowed("26356199"))
                self.assertFalse(mod.applicant_is_write_allowed("999000111"))
        finally:
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR, None)
                os.environ.pop(ROBIE_EZLYNX_WRITE_SCOPE_ENV_VAR, None)
                os.environ.pop("ROBIE_PLAYGROUND", None)
                importlib.reload(scope)

    def test_blocks_still_win_and_a_real_carrier_email_waits_for_go(self):
        reader = Reader()
        writer = Writer(reader)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(ROBIE_EZLYNX_WRITE_SCOPE="all"), clear=False):
                self.assertTrue(classify_playground_request("delete the policy").blocked)
                self.assertTrue(classify_playground_request("bind the policy").blocked)
                self.assertTrue(classify_playground_request("change the premium to 100").blocked)
                self.assertTrue(classify_playground_request("email the client about the change").blocked)
                blocked = handle_playground_chat(
                    db,
                    "delete the policy for Real Client applicant 999000111",
                    conversation_id=SPACE,
                    thread_id="all-block",
                    message_id="all-block",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
                self.assertIn("I can't", blocked[0])
                self.assertFalse(blocked[0].startswith("Practice mode"))
                buster = handle_playground_chat(
                    db,
                    "delete the policy for Buster Brown",
                    conversation_id=SPACE,
                    thread_id="all-buster",
                    message_id="all-buster",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
                self.assertTrue(buster[0].startswith("Practice mode"))
                waiting = handle_playground_chat(
                    db,
                    REAL,
                    conversation_id=SPACE,
                    thread_id="all-mail",
                    message_id="all-mail-1",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
                self.assertEqual(writer.calls, [])
                self.assertIn("changes@progressive.com", waiting[0])
                self.assertNotIn("[PRACTICE - would have gone to", waiting[0])
                self.assertNotIn("carlo@streetsmart.insurance", waiting[0])
                self.assertFalse(waiting[0].startswith("Practice mode"))
                self.assertTrue(buster_brown_only_mode("26356199"))
                self.assertFalse(buster_brown_only_mode("999000111"))
                self.assertTrue(carrier_must_redirect("26356199"))
                self.assertFalse(carrier_must_redirect("999000111"))
                done = handle_playground_chat(
                    db,
                    "go",
                    conversation_id=SPACE,
                    thread_id="all-mail",
                    message_id="all-mail-2",
                    requested_by="Casey",
                    now=WHEN + timedelta(minutes=1),
                    apply=writer,
                    read=reader,
                )
            self.assertEqual(len(writer.calls), 1)
            self.assertEqual(writer.calls[0].new_value, "changes@progressive.com")
            self.assertNotIn("PRACTICE", writer.calls[0].subject)
            self.assertIn("Done.", done[1])
            store = JobStore(db)
            rows = list_writes_since(
                store,
                since=WHEN - timedelta(minutes=1),
                until=WHEN + timedelta(hours=1),
            )
            self.assertTrue(any(row["applicant_id"] == "999000111" for row in rows))

    def test_buster_still_redirects_when_all_clients_are_open(self):
        proposal = Proposal(
            kind="carrier_email",
            client="Buster Brown",
            applicant_id="26356199",
            carrier_address="changes@progressive.com",
            subject="Policy change request",
            field="carrier email",
            old_value="not sent",
            new_value="changes@progressive.com",
        )
        from robie_job_engine.playground_execute import prepare_carrier_delivery

        with mock.patch.dict(os.environ, _env(ROBIE_EZLYNX_WRITE_SCOPE="all"), clear=False):
            prepared = prepare_carrier_delivery(proposal)
        self.assertTrue(prepared.extra["redirected"])
        self.assertEqual(prepared.new_value, "carlo@streetsmart.insurance")
        self.assertIn("[PRACTICE - would have gone to changes@progressive.com]", prepared.subject)
