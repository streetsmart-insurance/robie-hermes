"""Robie Playground guardrails, confirmation, stop, redirect, readback, digest."""

from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_turn_control import is_stop_command
from robie_job_engine.hitl_ladder import unanswered_hitl_kill_reason
from robie_job_engine.playground_config import (
    buster_brown_only_mode,
    carrier_must_redirect,
    message_in_playground,
    sop_folders,
)
from robie_job_engine.playground_execute import (
    ApplyResult,
    compare_readback,
    default_apply,
    prepare_carrier_delivery,
)
from robie_job_engine.playground_guardrails import (
    CARRIER_EMAIL,
    Proposal,
    classify_playground_request,
)
from robie_job_engine.playground_service import (
    handle_playground_chat,
    handle_playground_email,
)
from robie_job_engine.playground_sop import (
    collect_documents,
    dedupe_documents,
    is_excluded_drive_file,
    retrieve_sop,
)
from robie_job_engine.playground_undo import (
    daily_window,
    list_writes_since,
    record_write,
    render_daily_change_list,
)
from robie_job_engine.runtime_env import playground_enabled
from robie_job_engine.store import JobStore

ROOT = Path(__file__).resolve().parents[1]
SPACE = "spaces/PLAYGROUND"
WHEN = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
ADDRESS = (
    "Please change the mailing address from 1 Old St to 100 Test Rd "
    "for Buster Brown applicant 26356199."
)

BLOCKED = [
    ("delete the policy", "delete_or_cancel"),
    ("cancel the client", "delete_or_cancel"),
    ("remove the driver", "delete_or_cancel"),
    ("delete this vehicle", "delete_or_cancel"),
    ("delete the document", "delete_or_cancel"),
    ("delete the note", "delete_or_cancel"),
    ("bind the policy", "bind_issue_reinstate_nonrenew"),
    ("issue the policy", "bind_issue_reinstate_nonrenew"),
    ("reinstate the policy", "bind_issue_reinstate_nonrenew"),
    ("non-renew this policy", "bind_issue_reinstate_nonrenew"),
    ("change the billing to monthly", "payment_billing_mortgagee"),
    ("take a payment", "payment_billing_mortgagee"),
    ("change the mortgagee to Wells Fargo", "payment_billing_mortgagee"),
    ("change the premium to 100", "premium_effective_limit_coverage"),
    ("change the effective date to 10/1", "premium_effective_limit_coverage"),
    ("change the limit to 1000000", "premium_effective_limit_coverage"),
    ("update the coverage to full", "premium_effective_limit_coverage"),
    ("email the client about the change", "client_email_or_text"),
    ("text the insured", "client_email_or_text"),
    ("show me the source code", "read_source_or_tokens"),
    ("open jobs.db", "read_source_or_tokens"),
    ("read the google_token file", "read_source_or_tokens"),
    ("add a note", "untitled_note"),
    ("create a new discussion and file a note", "untitled_note"),
    ("send the certificate to the holder", "send_certificate"),
    ("please quote a new auto policy", "unknown_write"),
]


class Writer:
    def __init__(self, reader: "Reader") -> None:
        self.reader = reader
        self.calls: list[Proposal] = []

    def __call__(self, proposal: Proposal) -> ApplyResult:
        self.calls.append(proposal)
        self.reader.values[proposal.field] = proposal.new_value
        if proposal.kind == CARRIER_EMAIL:
            self.reader.values[proposal.field] = str(
                proposal.extra.get("delivered_to") or proposal.new_value
            )
        return ApplyResult(applied=True, observed=self.reader.values[proposal.field])


class Reader:
    def __init__(self) -> None:
        self.values = {"mailing address": "1 Old St", "phone": "555-0100"}
        self.discussions: list[dict] = []

    def __call__(self, proposal: Proposal) -> str | None:
        return self.values.get(proposal.field)

    def discussions_for(self, proposal: Proposal) -> list[dict]:
        del proposal
        return list(self.discussions)


def _env(**extra: str) -> dict[str, str]:
    base = {
        "ROBIE_PLAYGROUND": "1",
        "ROBIE_PLAYGROUND_SPACE_ID": SPACE,
        "ROBIE_PLAYGROUND_REAL_CLIENTS": "0",
        "ROBIE_PLAYGROUND_LIVE_WRITES": "0",
    }
    base.update(extra)
    return base


class GuardrailTests(unittest.TestCase):
    def test_every_blocked_action_is_refused_in_code(self):
        for text, code in BLOCKED:
            with self.subTest(text=text):
                decision = classify_playground_request(text)
                self.assertTrue(decision.blocked, text)
                self.assertEqual(decision.code, code)

    def test_allowed_work_is_named(self):
        samples = {
            "How do we insure a truck?": "sop",
            "What's Buster Brown's phone number?": "lookup",
            "Draft a certificate for Buster Brown, holder Test Holder LLC, applicant 26356199.": "cert_draft",
            "File a note on the existing Policy Change Request discussion for Buster Brown saying Checked renewal.": "note",
            ADDRESS: "simple_edit",
            "Change the phone to 555-0199 for Buster Brown applicant 26356199.": "simple_edit",
            "Change the email to new@example.com for Buster Brown applicant 26356199.": "simple_edit",
            "Add driver Jane Doe for Buster Brown applicant 26356199.": "add_driver",
            "Add vehicle 1HGBH41JXMN109186 for Buster Brown applicant 26356199.": "add_vehicle",
            "Email the carrier at changes@progressive.com to request a policy change for Buster Brown applicant 26356199.": "carrier_email",
        }
        for text, intent in samples.items():
            with self.subTest(text=text):
                decision = classify_playground_request(text)
                self.assertFalse(decision.blocked, decision.reason)
                self.assertEqual(decision.intent, intent)

    def test_vague_ask_is_one_question(self):
        decision = classify_playground_request("@Robie can you do a book for me")
        self.assertEqual(decision.intent, "vague")
        self.assertIn("?", decision.question)
        self.assertEqual(decision.question.count("?"), 1)

    def test_bare_stop_is_playground_stop_and_not_the_global_command(self):
        self.assertFalse(is_stop_command("stop"))
        self.assertEqual(classify_playground_request("stop").intent, "stop")
        self.assertEqual(classify_playground_request("/stop").intent, "stop")
        self.assertNotEqual(
            classify_playground_request("please stop the policy change").intent,
            "stop",
        )

    def test_playground_is_off_unless_the_flag_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROBIE_PLAYGROUND", None)
            os.environ["ROBIE_PLAYGROUND_SPACE_ID"] = SPACE
            self.assertFalse(playground_enabled())
            self.assertFalse(message_in_playground(SPACE))

    def test_blocked_chat_never_calls_the_writer(self):
        reader = Reader()
        writer = Writer(reader)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False):
                for index, (text, _code) in enumerate(BLOCKED):
                    replies = handle_playground_chat(
                        db,
                        text,
                        conversation_id=SPACE,
                        thread_id=f"thread-{index}",
                        message_id=f"msg-{index}",
                        requested_by="Casey",
                        now=WHEN,
                        apply=writer,
                        read=reader,
                    )
                    self.assertIsNotNone(replies)
                    if _code == "untitled_note":
                        self.assertIn("already has a title", replies[0])
                    else:
                        self.assertIn("I can't", replies[0])
                    self.assertIn("Practice mode", replies[0])
                    self.assertNotIn("Ref: job", replies[0])
                    self.assertNotRegex(replies[0], r"[0-9a-f]{8}-[0-9a-f]{4}-")
        self.assertEqual(writer.calls, [])


class ConfirmationTests(unittest.TestCase):
    def _allow(self):
        return mock.patch(
            "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
            frozenset({"26356199"}),
        )

    def test_write_waits_for_go_and_then_confirms_the_readback(self):
        reader = Reader()
        reader.discussions = [{"title": "Policy Change Request Checkup"}]
        writer = Writer(reader)
        notes: list[str] = []

        def file_note(proposal, title, body):
            del proposal
            notes.append(f"{title}: {body}")
            return "note-42"

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), self._allow():
                waiting = handle_playground_chat(
                    db,
                    ADDRESS,
                    conversation_id=SPACE,
                    thread_id="t1",
                    message_id="m1",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                    discussions=reader.discussions_for,
                    file_note=file_note,
                )
                self.assertEqual(writer.calls, [])
                self.assertIn("Nothing is changed yet", waiting[0])
                self.assertIn("Buster Brown", waiting[0])
                self.assertIn("mailing address", waiting[0])
                self.assertIn("1 Old St", waiting[0])
                self.assertIn("100 Test Rd", waiting[0])
                self.assertNotIn("Ref: job", waiting[0])
                done = handle_playground_chat(
                    db,
                    "go",
                    conversation_id=SPACE,
                    thread_id="t1",
                    message_id="m2",
                    requested_by="Casey",
                    now=WHEN + timedelta(minutes=1),
                    apply=writer,
                    read=reader,
                    discussions=reader.discussions_for,
                    file_note=file_note,
                )
            self.assertEqual(len(writer.calls), 1)
            self.assertEqual(len(done), 1)
            self.assertIn("Done", done[0])
            self.assertNotIn("Readback", done[0])
            self.assertIn("I added a short note", done[0])
            self.assertIn("Policy Change Request", notes[0])
            store = JobStore(db)
            finished = store.list_jobs_by_status({"COMPLETE"})
            self.assertEqual(len(finished), 1)
            self.assertEqual(finished[0]["status"], "COMPLETE")
            rows = list_writes_since(store, since=WHEN - timedelta(minutes=1), until=WHEN + timedelta(hours=1))
            self.assertGreaterEqual(len(rows), 2)
            change = next(row for row in rows if row["field_name"] == "mailing address")
            self.assertEqual(change["requested_by"], "Casey")
            self.assertEqual(change["before_value"], "1 Old St")
            self.assertEqual(change["after_value"], "100 Test Rd")
            self.assertEqual(change["readback_result"], "matched")
            self.assertTrue(change["job_id"])
            self.assertTrue(change["requested_at"])

    def test_carrier_email_is_read_back_to_the_sink_before_it_sends(self):
        reader = Reader()
        writer = Writer(reader)
        ask = (
            "Email the carrier at changes@progressive.com to request a policy change "
            "for Buster Brown applicant 26356199."
        )
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), self._allow():
                waiting = handle_playground_chat(
                    db,
                    ask,
                    conversation_id=SPACE,
                    thread_id="t-carrier",
                    message_id="m-carrier-1",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
                self.assertEqual(writer.calls, [])
                self.assertIn("[PRACTICE - would have gone to changes@progressive.com]", waiting[0])
                self.assertIn("carlo@streetsmart.insurance", waiting[0])
                handle_playground_chat(
                    db,
                    "yes",
                    conversation_id=SPACE,
                    thread_id="t-carrier",
                    message_id="m-carrier-2",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
        self.assertEqual(len(writer.calls), 1)
        sent = writer.calls[0]
        self.assertEqual(sent.new_value, "carlo@streetsmart.insurance")
        self.assertNotEqual(sent.new_value, "changes@progressive.com")
        self.assertIn("[PRACTICE - would have gone to changes@progressive.com]", sent.subject)

    def test_go_is_refused_when_the_applicant_is_not_allowlisted(self):
        reader = Reader()
        writer = Writer(reader)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), mock.patch(
                "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
                frozenset({"220250093"}),
            ):
                handle_playground_chat(
                    db,
                    ADDRESS,
                    conversation_id=SPACE,
                    thread_id="t-block",
                    message_id="m-block",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
        self.assertEqual(writer.calls, [])

    def test_no_go_within_30_minutes_cancels(self):
        reader = Reader()
        writer = Writer(reader)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), self._allow():
                handle_playground_chat(
                    db,
                    ADDRESS,
                    conversation_id=SPACE,
                    thread_id="t-wait",
                    message_id="m-wait",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
                late = handle_playground_chat(
                    db,
                    "yes",
                    conversation_id=SPACE,
                    thread_id="t-wait",
                    message_id="m-late",
                    requested_by="Casey",
                    now=WHEN + timedelta(minutes=31),
                    apply=writer,
                    read=reader,
                )
        self.assertEqual(writer.calls, [])
        self.assertIn("30 minutes", late[0])
        self.assertIn("didn't change anything", late[0].casefold())

    def test_stop_and_slash_stop_end_the_job(self):
        reader = Reader()
        writer = Writer(reader)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), self._allow():
                handle_playground_chat(
                    db,
                    ADDRESS,
                    conversation_id=SPACE,
                    thread_id="t-stop",
                    message_id="m-stop-1",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
                stopped = handle_playground_chat(
                    db,
                    "stop",
                    conversation_id=SPACE,
                    thread_id="t-stop",
                    message_id="m-stop-2",
                    requested_by="Alex",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
                handle_playground_chat(
                    db,
                    ADDRESS,
                    conversation_id=SPACE,
                    thread_id="t-cancel",
                    message_id="m-cancel-1",
                    requested_by="Casey",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
                cancelled = handle_playground_chat(
                    db,
                    "/stop",
                    conversation_id=SPACE,
                    thread_id="t-cancel",
                    message_id="m-cancel-2",
                    requested_by="Alex",
                    now=WHEN,
                    apply=writer,
                    read=reader,
                )
            store = JobStore(db)
            failed = store.list_jobs_by_status({"FAILED"})
            self.assertGreaterEqual(len(failed), 2)
        self.assertIn("cancelled", stopped[0].casefold())
        self.assertIn("cancelled", cancelled[0].casefold())
        self.assertEqual(writer.calls, [])

    def test_readback_mismatch_does_not_claim_success(self):
        reader = Reader()
        writer = Writer(reader)

        def stale(proposal: Proposal) -> ApplyResult:
            writer.calls.append(proposal)
            return ApplyResult(applied=True, observed="1 Old St")

        def still_old(proposal: Proposal) -> str:
            del proposal
            return "1 Old St"

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False), self._allow():
                handle_playground_chat(
                    db,
                    ADDRESS,
                    conversation_id=SPACE,
                    thread_id="t-bad",
                    message_id="m-bad-1",
                    requested_by="Casey",
                    now=WHEN,
                    apply=stale,
                    read=reader,
                )
                done = handle_playground_chat(
                    db,
                    "go",
                    conversation_id=SPACE,
                    thread_id="t-bad",
                    message_id="m-bad-2",
                    requested_by="Casey",
                    now=WHEN,
                    apply=stale,
                    read=still_old,
                )
        self.assertEqual(len(done), 1)
        self.assertNotIn("Done", done[0])
        self.assertIn("could not confirm", done[0].casefold())
        self.assertIn("not calling this a success", done[0].casefold())
        self.assertIn("person needs", done[0].casefold())

    def test_hitl_sweeper_does_not_eat_a_playground_confirmation(self):
        reason = unanswered_hitl_kill_reason(
            {
                "action_type": "playground.task",
                "status": "AWAITING_HUMAN_INPUT",
                "updated_at": "2020-01-01T00:00:00+00:00",
                "payload": {},
            }
        )
        self.assertIsNone(reason)


class CarrierAndPracticeTests(unittest.TestCase):
    def test_practice_carrier_mail_is_redirected(self):
        proposal = Proposal(
            kind=CARRIER_EMAIL,
            client="Buster Brown",
            applicant_id="26356199",
            carrier_address="changes@progressive.com",
            subject="Policy change request",
            field="carrier email",
            old_value="not sent",
            new_value="changes@progressive.com",
        )
        with mock.patch.dict(os.environ, _env(), clear=False):
            prepared = prepare_carrier_delivery(proposal)
        self.assertTrue(prepared.extra["redirected"])
        self.assertEqual(prepared.new_value, "carlo@streetsmart.insurance")
        self.assertIn(
            "[PRACTICE - would have gone to changes@progressive.com]",
            prepared.subject,
        )
        self.assertNotIn("changes@progressive.com", prepared.new_value)

    def test_real_allowlisted_client_is_not_redirected(self):
        proposal = Proposal(
            kind=CARRIER_EMAIL,
            client="Real Client",
            applicant_id="999000111",
            carrier_address="changes@progressive.com",
            subject="Policy change request",
            new_value="changes@progressive.com",
        )
        with mock.patch.dict(os.environ, _env(ROBIE_EZLYNX_WRITE_SCOPE="all"), clear=False):
            self.assertFalse(carrier_must_redirect("999000111"))
            prepared = prepare_carrier_delivery(proposal)
        self.assertFalse(prepared.extra.get("redirected"))
        self.assertEqual(prepared.new_value, "changes@progressive.com")
        with mock.patch.dict(os.environ, _env(), clear=False), mock.patch(
            "robie_job_engine.playground_config.write_allowed",
            return_value=True,
        ):
            self.assertTrue(carrier_must_redirect("999000111"))

    def test_non_allowlisted_client_still_redirects_when_the_switch_is_on(self):
        with mock.patch.dict(os.environ, _env(ROBIE_PLAYGROUND_REAL_CLIENTS="1"), clear=False), mock.patch(
            "robie_job_engine.playground_config.write_allowed",
            return_value=False,
        ):
            self.assertTrue(carrier_must_redirect("888000111"))

    def test_practice_tag_is_on_for_buster_and_off_when_real_clients_are_open(self):
        reader = Reader()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, _env(), clear=False):
                self.assertTrue(buster_brown_only_mode())
                tagged = handle_playground_chat(
                    db,
                    "what can you do",
                    conversation_id=SPACE,
                    thread_id="t-help",
                    message_id="m-help",
                    requested_by="Casey",
                    now=WHEN,
                    read=reader,
                )
            self.assertTrue(tagged[0].startswith("Practice mode"))
            self.assertIn("Here's what I can", tagged[0])
            with mock.patch.dict(
                os.environ, _env(ROBIE_EZLYNX_WRITE_SCOPE="all"), clear=False
            ), mock.patch(
                "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
                frozenset({"999000111"}),
            ):
                self.assertFalse(buster_brown_only_mode())
                self.assertTrue(buster_brown_only_mode("26356199"))
                plain = handle_playground_chat(
                    db,
                    "what can you do",
                    conversation_id=SPACE,
                    thread_id="t-help-2",
                    message_id="m-help-2",
                    requested_by="Casey",
                    now=WHEN,
                    read=reader,
                )
        self.assertFalse(plain[0].startswith("Practice mode"))

    def test_readback_match_and_mismatch(self):
        matched = compare_readback(expected="100 Test Rd", observed="100 test rd")
        self.assertTrue(matched.matched)
        self.assertFalse(matched.human_required)
        missed = compare_readback(expected="100 Test Rd", observed="1 Old St")
        self.assertFalse(missed.matched)
        self.assertTrue(missed.human_required)
        empty = compare_readback(expected="100 Test Rd", observed=None)
        self.assertTrue(empty.human_required)

    def test_live_writes_stay_off_and_the_note_path_takes_the_driver_lock(self):
        proposal = Proposal(
            kind="note",
            applicant_id="26356199",
            discussion_title="Policy Change Request",
            new_value="checked",
            client="Buster Brown",
            field="note",
        )
        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND_LIVE_WRITES": "0"}, clear=False):
            refused = default_apply(proposal)
        self.assertFalse(refused.applied)
        entered: list[int] = []

        class _Lock:
            def __enter__(self):
                entered.append(1)
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND_LIVE_WRITES": "1"}, clear=False), mock.patch(
            "robie_job_engine.playground_execute.with_ezlynx_lock",
            return_value=_Lock(),
        ), mock.patch(
            "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
            return_value={"status": "filed", "note_id": "n1"},
        ):
            filed = default_apply(proposal)
        self.assertTrue(filed.applied)
        self.assertEqual(entered, [1])
        self.assertEqual(filed.note_id, "n1")


class DailyListAndSopTests(unittest.TestCase):
    def test_daily_list_groups_the_last_day_by_client(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            record_write(
                store,
                job_id="job-buster",
                requested_by="Casey",
                requested_at=WHEN.isoformat(),
                client_name="Buster Brown",
                applicant_id="26356199",
                field_name="mailing address",
                before_value="1 Old St",
                after_value="100 Test Rd",
                write_kind="simple_edit",
                readback_result="matched",
                readback_detail="matches",
                created_at=WHEN.isoformat(),
            )
            record_write(
                store,
                job_id="job-other",
                requested_by="Alex",
                requested_at=(WHEN - timedelta(hours=2)).isoformat(),
                client_name="Other Client",
                applicant_id="111",
                field_name="phone",
                before_value="555-0100",
                after_value="555-0199",
                write_kind="simple_edit",
                readback_result="matched",
                readback_detail="matches",
                created_at=(WHEN - timedelta(hours=2)).isoformat(),
            )
            record_write(
                store,
                job_id="job-old",
                requested_by="Casey",
                requested_at=(WHEN - timedelta(days=3)).isoformat(),
                client_name="Buster Brown",
                applicant_id="26356199",
                field_name="email",
                before_value="old@example.com",
                after_value="new@example.com",
                write_kind="simple_edit",
                readback_result="matched",
                readback_detail="matches",
                created_at=(WHEN - timedelta(days=3)).isoformat(),
            )
            since, until = daily_window(now=WHEN + timedelta(minutes=5))
            rows = list_writes_since(store, since=since, until=until)
            text = render_daily_change_list(rows, now=until)
            with self.assertRaises(sqlite3.IntegrityError):
                with store.connect() as conn:
                    conn.execute("DELETE FROM playground_undo_log")
        self.assertIn("Buster Brown", text)
        self.assertIn("Other Client", text)
        self.assertIn("100 Test Rd", text)
        self.assertNotIn("old@example.com", text)
        self.assertLess(text.index("Buster Brown"), text.index("Other Client"))

    def test_sop_excludes_and_dedupes_and_cites_the_source(self):
        files = [
            {"id": "1", "name": "How to insure a truck", "md5": "aaa", "folder_label": "core", "text": "Trucking uses the trucking guide."},
            {"id": "2", "name": "How to insure a truck (1)", "md5": "aaa", "folder_label": "trucking"},
            {"id": "3", "name": "EZLynx API keys", "folder_label": "core"},
            {"id": "4", "name": "Carrier login websites", "folder_label": "core"},
            {"id": "5", "name": "Payroll calendar", "folder_label": "employee_hub"},
            {"id": "6", "name": "Handbook", "driveId": "0ANbwd0py5G63Uk9PVA", "folder_label": "core"},
            {"id": "7", "name": "Finance pack", "driveId": "0AKIUMpLpYux4Uk9PVA", "folder_label": "core"},
            {"id": "8", "name": "Adobe scan packet", "folder_label": "core"},
            {"id": "9", "name": "Mail client scans", "folder_label": "core"},
            {"id": "10", "name": "Book of business", "folder_label": "core"},
            {"id": "11", "name": "Lawsuit folder notes", "folder_label": "core"},
        ]
        for meta in files[2:]:
            self.assertTrue(is_excluded_drive_file(meta, folder_label=meta["folder_label"]), meta["name"])
        kept = dedupe_documents(files[:2])
        self.assertEqual([item["id"] for item in kept], ["1"])

        class Drive:
            def list_folder(self, folder_id: str):
                label = {
                    "1nwtIlz07UNNloBR8zqDOSOKEU64m6YdE": "core",
                    "1orog25WUu6KzW4DFJ88vK_kId2ZDmYvC": "trucking",
                }.get(folder_id, "other")
                return [item for item in files if item["folder_label"] == label]

            def export_text(self, file_id: str, mime: str) -> str:
                del mime
                return next(item["text"] for item in files if item["id"] == file_id)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROBIE_PLAYGROUND_SOP_FOLDERS", None)
            self.assertEqual(sop_folders()[0][0], "core")
            collected = collect_documents(Drive())
        ids = {item["id"] for item in collected}
        self.assertIn("1", ids)
        self.assertNotIn("2", ids)
        self.assertNotIn("3", ids)
        self.assertNotIn("5", ids)
        hits = retrieve_sop("How do we insure a truck?", [{"doc_id": "1", "title": "How to insure a truck", "folder": "trucking", "text": "Trucking uses the trucking guide."}])
        self.assertEqual(hits[0].title, "How to insure a truck")
        self.assertIn("trucking", hits[0].citation)

    def test_email_uses_the_same_block_and_stays_off_without_the_flag(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ROBIE_PLAYGROUND", None)
                self.assertIsNone(
                    handle_playground_email(db, "delete the policy", message_id="e0")
                )
            with mock.patch.dict(os.environ, {"ROBIE_PLAYGROUND": "1"}, clear=False):
                reply = handle_playground_email(
                    db,
                    "delete the policy",
                    sender="casey@streetsmart.insurance",
                    thread_id="thread-1",
                    message_id="e1",
                )
        self.assertIn("I can't", reply)
        self.assertNotIn("Ref: job", reply)

    def test_hooks_are_in_front_of_the_agent(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        email = (ROOT / "robie_job_engine/email_guard.py").read_text(encoding="utf-8")
        stop_at = adapter.index("if is_stop_command(text)")
        open_at = adapter.index(
            "job_id = await asyncio.to_thread(\n                open_chat_job"
        )
        self.assertLess(adapter.index("handle_playground_chat"), stop_at)
        self.assertLess(stop_at, open_at)
        self.assertLess(
            email.index("handle_playground_email"),
            email.index("final = engine.run"),
        )
        service = (ROOT / "deploy/systemd/robie-playground-daily.service").read_text(encoding="utf-8")
        timer = (ROOT / "deploy/systemd/robie-playground-daily.timer").read_text(encoding="utf-8")
        self.assertIn("hermes-test-01", service)
        self.assertIn("ConditionHost", service)
        self.assertIn("America/New_York", timer)
        self.assertIn("robie_job_engine.playground_daily", service)
