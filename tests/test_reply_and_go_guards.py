"""Go is bound to the requester and thread. Menus and replies stay honest."""

from __future__ import annotations

import ast
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from durable_temp import durable_temporary_directory

from robie_job_engine.answer_only import mark_answered_question
from robie_job_engine.models import JobStatus
from robie_job_engine.playground_execute import (
    DISCONNECTED_WRITER_KINDS,
    ENABLED_WRITERS,
    STAFF_STILL_HANDLES,
    ApplyResult,
    capability_menu,
    enabled_writer_names,
)
from robie_job_engine.playground_guardrails import Proposal
from robie_job_engine.playground_service import (
    MISSING_THREAD,
    ONLY_REQUESTER,
    handle_playground_chat,
)
from robie_job_engine.playground_voice import line
from robie_job_engine.reporting import health_status_name, is_answered_question_job, prepare_status_digest
from robie_job_engine.store import JobStore
from robie_job_engine.user_reply import format_user_reply

ROOT = Path(__file__).resolve().parents[1]
SPACE = "spaces/PLAYGROUND"
WHEN = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)
THREAD = "spaces/ROBY/threads/change-1"
ADDRESS = (
    "Please change the mailing address from 1 Old St to 100 Test Rd "
    "for Buster Brown applicant 26356199."
)

# Chat or email sends that are not a person's job reply. A new send site
# that is missing from this set and does not call format_user_reply fails
# the test. Operational reports and the health webhook keep their full text.
EXEMPT_SENDS = {
    ("integrations/google_chat/adapter.py", "_create_message"): "transport; send() formats the text",
    ("integrations/google_chat/adapter.py", "_handle_card_event"): "card patch uses the choice receipt",
    ("integrations/google_chat/adapter.py", "_handle_setup_files_command"): "one-time OAuth setup, not a job reply",
    ("integrations/google_chat/adapter.py", "_reply"): "setup-files reply, not a job reply",
    ("integrations/google_chat/adapter.py", "send_card"): "card body, not prose",
    ("integrations/google_chat/adapter.py", "send_typing"): "fixed typing line",
    ("integrations/google_chat/adapter.py", "_create_and_record"): "typing card",
    ("integrations/google_chat/adapter.py", "send_image"): "image caption and url",
    ("integrations/google_chat/adapter.py", "_post_attachment_fallback"): "file-delivery setup notice",
    ("robie_job_engine/accountability_delivery.py", "deliver_report"): "operations report, not a job reply",
    ("robie_job_engine/ascend_sync.py", "send_google_chat_alert"): "operator webhook, not a job reply",
    ("robie_job_engine/staff_jobs_common.py", "send_gmail"): "staff mail helper; callers own the body",
    ("robie_job_engine/confirmation_notify.py", "_real_gmail_sender"): "confirmation mail, not a job reply",
    ("robie_job_engine/confirmation_notify.py", "send"): "confirmation mail sender, not a job reply",
    ("robie_job_engine/lost_customer_retention.py", "send_message"): "retention mail, not a job reply",
    ("robie_job_engine/overdue_policy_change_reports.py", "default_mailer"): "overdue report, not a job reply",
    ("scripts/robie_health_check.py", "send_chat_alert"): "health channel keeps the checklist",
    ("scripts/robie_health_check.py", "send_daily_digest"): "health channel keeps the checklist",
}


def _env() -> dict[str, str]:
    return {
        "ROBIE_PLAYGROUND": "1",
        "ROBIE_PLAYGROUND_SPACE_ID": SPACE,
        "ROBIE_PLAYGROUND_REAL_CLIENTS": "0",
        "ROBIE_PLAYGROUND_LIVE_WRITES": "0",
    }


class _Writer:
    def __init__(self) -> None:
        self.calls: list[Proposal] = []

    def __call__(self, proposal: Proposal) -> ApplyResult:
        self.calls.append(proposal)
        return ApplyResult(applied=True, observed=proposal.new_value)


class _Reader:
    def __call__(self, proposal: Proposal) -> str:
        del proposal
        return "1 Old St"


class GoBindingTests(unittest.TestCase):
    def _ask(self, db: str, *, user: str, thread: str) -> None:
        handle_playground_chat(
            db,
            ADDRESS,
            conversation_id=SPACE,
            thread_id=thread,
            message_id=f"ask-{user}-{thread}",
            requested_by="Casey",
            requester_user_id=user,
            now=WHEN,
            apply=_Writer(),
            read=_Reader(),
        )

    def _waiting(self, db: str) -> dict:
        rows = JobStore(db).list_jobs_by_status({JobStatus.AWAITING_HUMAN_INPUT})
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_second_user_in_the_same_thread_does_not_approve(self):
        writer = _Writer()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), mock.patch(
                "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
                frozenset({"26356199"}),
            ):
                self._ask(db, user="users/requester", thread=THREAD)
                replies = handle_playground_chat(
                    db,
                    "go",
                    conversation_id=SPACE,
                    thread_id=THREAD,
                    message_id="go-other",
                    requested_by="Alex",
                    requester_user_id="users/other",
                    now=WHEN + timedelta(minutes=1),
                    apply=writer,
                    read=_Reader(),
                )
                self.assertEqual(replies, [ONLY_REQUESTER])
                self.assertEqual(writer.calls, [])
                self._waiting(db)

    def test_missing_thread_does_not_approve(self):
        writer = _Writer()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), mock.patch(
                "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
                frozenset({"26356199"}),
            ):
                self._ask(db, user="users/requester", thread=THREAD)
                replies = handle_playground_chat(
                    db,
                    "go",
                    conversation_id=SPACE,
                    thread_id=None,
                    message_id="go-missing",
                    requested_by="Casey",
                    requester_user_id="users/requester",
                    now=WHEN + timedelta(minutes=1),
                    apply=writer,
                    read=_Reader(),
                )
                self.assertEqual(replies, [MISSING_THREAD])
                self.assertEqual(writer.calls, [])
                self._waiting(db)

    def test_empty_thread_does_not_approve(self):
        writer = _Writer()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), mock.patch(
                "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
                frozenset({"26356199"}),
            ):
                self._ask(db, user="users/requester", thread=THREAD)
                replies = handle_playground_chat(
                    db,
                    "go",
                    conversation_id=SPACE,
                    thread_id="",
                    message_id="go-empty",
                    requested_by="Casey",
                    requester_user_id="users/requester",
                    now=WHEN + timedelta(minutes=1),
                    apply=writer,
                    read=_Reader(),
                )
                self.assertEqual(replies, [MISSING_THREAD])
                self.assertEqual(writer.calls, [])
                self._waiting(db)

    def test_requester_in_the_correct_thread_approves(self):
        writer = _Writer()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), mock.patch(
                "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
                frozenset({"26356199"}),
            ):
                self._ask(db, user="users/requester", thread=THREAD)
                replies = handle_playground_chat(
                    db,
                    "go",
                    conversation_id=SPACE,
                    thread_id=THREAD,
                    message_id="go-yes",
                    requested_by="Casey",
                    requester_user_id="users/requester",
                    now=WHEN + timedelta(minutes=1),
                    apply=writer,
                    read=lambda proposal: proposal.new_value,
                )
            self.assertEqual(len(writer.calls), 1)
            self.assertEqual(len(replies), 1)
            self.assertIn("Done", replies[0])
            self.assertNotIn("Ref: job", replies[0])
            finished = JobStore(db).list_jobs_by_status({JobStatus.COMPLETE})
            self.assertEqual(len(finished), 1)
            waiting = JobStore(db).list_jobs_by_status({JobStatus.AWAITING_HUMAN_INPUT})
            self.assertEqual(waiting, [])


class CapabilityMenuTests(unittest.TestCase):
    def test_menu_only_claims_enabled_writers(self):
        menu = capability_menu()
        self.assertEqual(menu.strip(), line("help_menu").strip())
        self.assertIn(STAFF_STILL_HANDLES, menu)
        self.assertNotIn("Look up a client", menu)
        self.assertNotIn("Change an address", menu)
        self.assertNotIn("Add a driver", menu)
        self.assertNotIn("Add a vehicle", menu)
        claims = {
            "note": "Add a note on a discussion",
            "carrier_email": "Email a carrier",
            "cert_draft": "Draft a certificate",
            "simple_edit": "Change an address",
            "add_driver": "Add a driver",
            "add_vehicle": "Add a vehicle",
        }
        enabled = enabled_writer_names()
        self.assertEqual(enabled, ENABLED_WRITERS)
        for name, phrase in claims.items():
            if phrase in menu:
                self.assertIn(name, enabled)
                self.assertNotIn(name, DISCONNECTED_WRITER_KINDS)


class UserReplyTests(unittest.TestCase):
    def test_formatter_drops_internal_words_and_job_ids(self):
        raw = (
            "Done.\n\n"
            "What happened: the note was filed.\n"
            "Status: UNVERIFIED\n"
            "Details\n"
            "Jev: wrong. readback failed. heartbeat missed. checkpoint open.\n"
            "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: Discussion API\n"
            "Ref: job 123e4567-e89b-12d3-a456-426614174000\n"
        )
        cleaned = format_user_reply(raw)
        self.assertEqual(cleaned, "Done.")
        self.assertNotIn("Ref:", cleaned)
        self.assertNotIn("UNVERIFIED", cleaned)
        self.assertNotIn("123e4567", cleaned)

    def test_a_question_stays_one_question(self):
        self.assertEqual(
            format_user_reply("Which client is this for?"),
            "Which client is this for?",
        )

    def test_outbound_send_sites_use_the_formatter(self):
        sites = _outbound_send_sites()
        self.assertGreaterEqual(len(sites), 8)
        missing = []
        for path, name, source in sites:
            if "format_user_reply" in source:
                continue
            if (path, name) in EXEMPT_SENDS:
                continue
            if "post_as_chat_app(" in source and "_create_message(" not in source and ".messages().send(" not in source:
                continue
            missing.append(f"{path}:{name}")
        self.assertEqual(missing, [])


class AnsweredQuestionHealthTests(unittest.TestCase):
    def test_a_question_is_complete_and_not_an_unverified_count(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            question = store.create_job(
                "hermes.plain_english",
                {"text": "Which carriers do we quote for NJ homeowners?", "answer_only": True},
            )
            mark_answered_question(store, question["id"], "We quote Travelers.")
            failed = store.create_job("hermes.plain_english", {"text": "move it"})
            store.transition(
                failed["id"],
                JobStatus.UNVERIFIED,
                expected={JobStatus.PENDING},
                error="no structured destination action checkpoint",
                release_lease=True,
            )
            question_row = store.get_job(question["id"])
            self.assertEqual(question_row["status"], "COMPLETE")
            self.assertTrue(is_answered_question_job(question_row))
            self.assertEqual(health_status_name(question_row), "answered")
            self.assertEqual(health_status_name(store.get_job(failed["id"])), "UNVERIFIED")
            digest = prepare_status_digest(
                db,
                destination="health-test",
                now=datetime.now(timezone.utc),
                artifact_root=str(Path(tmp) / "artifacts"),
            )
        self.assertEqual(digest.summary["status_counts"].get("answered"), 1)
        self.assertEqual(digest.summary["status_counts"].get("UNVERIFIED"), 1)
        self.assertNotIn(question["id"], digest.summary["attention_job_ids"])
        self.assertIn(failed["id"], digest.summary["attention_job_ids"])
        self.assertNotIn("UNVERIFIED", "We quote Travelers.")


def _outbound_send_sites() -> list[tuple[str, str, str]]:
    """Every function that posts Chat text or sends mail."""
    markers = ("_create_message(", "post_as_chat_app(", ".messages().send(", "urlopen(")
    found: list[tuple[str, str, str]] = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("tests/") or "/test_" in relative:
            continue
        if "venv" in relative or relative.startswith("."):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            segment = ast.get_source_segment(source, node) or ""
            if not any(marker in segment for marker in markers):
                continue
            if "urlopen(" in segment and "ROBIE_GOOGLE_CHAT_WEBHOOK_URL" not in segment:
                continue
            found.append((relative, node.name, segment))
    return found
