"""Round 8: discussion-note readback, recording vs audit, short Chat line."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_ezlynx_destination_verifier import (
    HermesChatEzlynxDestinationVerifier,
)
from robie_job_engine.chat_guard import (
    _merge_discussion_note_destination,
    guard_chat_response,
    open_chat_job,
    stop_generic_chat_job_heartbeat,
)
from robie_job_engine.complete_guard import intended_destination_identity
from robie_job_engine.engine import JobEngine
from robie_job_engine.models import JobStatus, VerificationEvidence
from robie_job_engine.post_job_audit import (
    api_readback_confirms_write,
    audit_terminal_job,
    format_audit_chat_message,
    run_post_job_audit,
)
from robie_job_engine.recording import RecordingStore
from robie_job_engine.store import JobStore
from robie_job_engine.write_verification_loop import (
    default_ezlynx_observed,
    lock_stated_plan,
    write_reply_if_planned,
)

APPLICANT = "26356199"
DISCUSSION = "848144886"
TITLE = "follw up 1"
NOTE = "Called about the follow up."


class _DiscussionPort:
    def __init__(self, record=None, boom=False):
        self.record = record if record is not None else {}
        self.boom = boom
        self.calls = []

    def get_discussion(self, discussion_id):
        self.calls.append(discussion_id)
        if self.boom:
            raise ConnectionError("DiscussionApi down")
        return self.record


def _note_record(body=NOTE, applicant=APPLICANT):
    record = {
        "applicantId": applicant,
        "notes": [{"body": body, "noteId": "note-9"}],
    }
    return record


def _job(applicant=APPLICANT, policy=""):
    payload = {"applicant_id": applicant, "account_name": "Buster Brown"}
    if policy:
        payload["policy_number"] = policy
    return {
        "id": "cc04c7f8-4788-45b9-974b-60a49da51e1a",
        "action_type": "hermes.google_chat_task",
        "created_at": "2026-09-30T12:00:00+00:00",
        "payload": payload,
    }


def _action(**extra):
    destination = {
        "applicant_id": APPLICANT,
        "discussion_id": DISCUSSION,
        "discussion_title": TITLE,
        "note_text": NOTE,
    }
    destination.update(extra)
    return {"destination": destination}


class DiscussionNoteReadbackTests(unittest.TestCase):
    def test_note_readback_matches_text_without_a_policy_number(self):
        port = _DiscussionPort(_note_record())
        result = HermesChatEzlynxDestinationVerifier(port).verify(_job(), _action())
        self.assertTrue(result.verified)
        self.assertTrue(result.evidence.authoritative)
        self.assertEqual(result.evidence.locator, DISCUSSION)
        self.assertEqual(result.evidence.method, "DiscussionApi")
        self.assertTrue(result.evidence.observed["note_text_matched"])
        self.assertNotIn("note_is_receipt_not_evidence", result.evidence.observed)
        self.assertEqual(port.calls, [DISCUSSION])
        self.assertIsNone(result.error)

    def test_whitespace_and_zero_width_still_match(self):
        port = _DiscussionPort(_note_record(body="Called   about the follow up."))
        claimed = _action(note_text="Called about\u200b the follow up.")
        result = HermesChatEzlynxDestinationVerifier(port).verify(_job(), claimed)
        self.assertTrue(result.verified)
        self.assertTrue(result.evidence.observed["note_text_matched"])

    def test_missing_note_text_is_authoritative_and_not_a_policy_lookup(self):
        port = _DiscussionPort(_note_record(body="some other note"))
        result = HermesChatEzlynxDestinationVerifier(port).verify(_job(), _action())
        self.assertFalse(result.verified)
        self.assertTrue(result.evidence.authoritative)
        self.assertFalse(result.retryable)
        self.assertIn("note text is not on discussion", result.error)
        self.assertNotIn("nothing to re-read", result.error)
        self.assertFalse(result.evidence.observed["note_text_matched"])

    def test_discussion_api_failure_is_retryable(self):
        port = _DiscussionPort(boom=True)
        result = HermesChatEzlynxDestinationVerifier(port).verify(_job(), _action())
        self.assertFalse(result.verified)
        self.assertTrue(result.retryable)
        self.assertFalse(result.evidence.authoritative)
        self.assertNotIn("nothing to re-read", result.error)

    def test_discussion_for_a_different_applicant_fails(self):
        port = _DiscussionPort(_note_record(applicant="111"))
        result = HermesChatEzlynxDestinationVerifier(port).verify(_job(), _action())
        self.assertFalse(result.verified)
        self.assertTrue(result.evidence.authoritative)
        self.assertIn("belongs to applicant", result.error)

    def test_no_discussion_id_still_has_nothing_to_reread(self):
        port = _DiscussionPort()
        result = HermesChatEzlynxDestinationVerifier(port).verify(
            _job(),
            _action(discussion_id="", note_text=""),
        )
        self.assertFalse(result.verified)
        self.assertIn("nothing to re-read", result.error)
        self.assertEqual(port.calls, [])

    def test_policy_write_still_uses_the_policy_number(self):
        class PolicyPort(_DiscussionPort):
            def policy_by_number(self, policy_number):
                return {
                    "status": "success",
                    "data": {
                        "Policies": [
                            {"PolicyNumber": "HO-100", "ApplicantId": APPLICANT}
                        ]
                    },
                }

            def documents_for_applicant(self, applicant_id, policy_id=0):
                return []

            def download_document(self, document_id):
                return b""

            def discussions_for_applicant(self, applicant_id):
                return [{"title": TITLE}]

            def get_discussion(self, discussion_id):
                raise AssertionError("a policy write must not key off the discussion")

        result = HermesChatEzlynxDestinationVerifier(PolicyPort()).verify(
            _job(policy="HO-100"),
            _action(policy_number="HO-100"),
        )
        self.assertTrue(result.verified)
        self.assertTrue(result.evidence.observed.get("note_is_receipt_not_evidence"))

    def test_discussion_id_is_the_identity_only_when_no_policy_number(self):
        self.assertEqual(
            intended_destination_identity(
                action={"destination": {"policy_number": "HO-100", "discussion_id": DISCUSSION}},
                payload={},
            ),
            "HO-100",
        )
        self.assertEqual(
            intended_destination_identity(
                action={"destination": {"discussion_id": DISCUSSION, "applicant_id": APPLICANT}},
                payload={"applicant_id": APPLICANT},
            ),
            DISCUSSION,
        )


class _Scorer:
    def evaluate(self, state, questions):
        return {
            "answers": {
                "satisfied": {"type": "noul", "noul": 0.9},
                "outcome": {"type": "choice", "choice": "completed", "confidence": 0.9},
            }
        }


class DiscussionObservedTests(unittest.TestCase):
    def test_live_readback_uses_discussion_id_not_a_policy_number(self):
        class Port:
            def get_discussion(self, discussion_id):
                self.discussion_id = discussion_id
                return {"notes": [{"body": NOTE, "noteId": "9"}]}

        port = Port()
        with patch.dict(os.environ, {"ROBIE_EZLYNX_API_READBACK": "1"}):
            with patch(
                "robie_job_engine.ezlynx_api_read_port.EzlynxApiClientReadPort",
                return_value=port,
            ):
                observed = default_ezlynx_observed(
                    {
                        "target": {"discussion_id": DISCUSSION, "applicant_id": APPLICANT},
                        "values": {"note_text": NOTE},
                    }
                )
        self.assertEqual(observed["note_text"], NOTE)
        self.assertNotIn("_error", observed)
        self.assertEqual(port.discussion_id, DISCUSSION)

    def test_live_readback_still_needs_a_policy_number_for_a_policy_write(self):
        with patch.dict(os.environ, {"ROBIE_EZLYNX_API_READBACK": "1"}):
            observed = default_ezlynx_observed(
                {"target": {"applicant_id": APPLICANT}, "values": {"mailingAddress": "1 Main"}}
            )
        self.assertIn("policy number", observed["_error"])

    def test_plan_fetch_copies_discussion_id_from_the_note_checkpoint(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.policy_change",
                {"text": "Add a note", "applicant_id": APPLICANT},
            )
            lock_stated_plan(
                store,
                job,
                {
                    "write": "note",
                    "target": {"applicant_id": APPLICANT, "discussion": TITLE},
                    "values": {"note_text": NOTE},
                },
            )
            store.checkpoint(
                job["id"],
                "discussion_note",
                {"discussion_id": DISCUSSION, "note_text": NOTE, "status": "filed"},
            )
            seen = {}

            def fetch(plan):
                seen["discussion_id"] = (plan.get("target") or {}).get("discussion_id")
                return {"note_text": NOTE}

            write_reply_if_planned(
                store,
                store.get_job(job["id"]),
                "",
                fetch_fn=fetch,
                client=_Scorer(),
            )
            self.assertEqual(seen["discussion_id"], DISCUSSION)


def _confirming_evidence(store, job_id):
    store.add_evidence(
        job_id,
        True,
        VerificationEvidence(
            method="DiscussionApi",
            source="ezlynx-discussionapi",
            expected={
                "applicant_id": APPLICANT,
                "discussion_id": DISCUSSION,
                "note_text": NOTE,
            },
            observed={
                "applicant_id": APPLICANT,
                "discussion_id": DISCUSSION,
                "note_text": NOTE,
                "note_text_matched": True,
                "read_method": "DiscussionApi",
            },
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=DISCUSSION,
        ),
    )


def _recording(db, job_id, status):
    store = RecordingStore(db)
    recording = store.create(job_id, Path(db).with_suffix(".webm"), Path(db).with_suffix(".stop"))
    return store.update(recording["id"], status=status)


class RecordingAuditTests(unittest.TestCase):
    def _job(self, db, *, confirm):
        store = JobStore(db)
        job = store.create_job(
            "hermes.google_chat_task",
            {
                "text": "add a note",
                "applicant_id": APPLICANT,
                "account_name": "Buster Brown",
            },
        )
        job_id = job["id"]
        store.checkpoint(
            job_id,
            "gateway_progress",
            {
                "first_at": "2026-09-30T12:00:00+00:00",
                "last_at": "2026-09-30T12:01:00+00:00",
            },
        )
        store.checkpoint(
            job_id,
            "action",
            {"destination": {"applicant_id": APPLICANT, "discussion_id": DISCUSSION}},
        )
        store.checkpoint(
            job_id,
            "worker_response",
            {"response_text": "playwright_exec page.goto('https://app.ezlynx.com')"},
        )
        if confirm:
            _confirming_evidence(store, job_id)
        _recording(db, job_id, "RECORDING")
        return job_id

    def test_confirmed_write_does_not_fail_the_verdict_on_a_recording(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = self._job(db, confirm=True)
            audit = run_post_job_audit(db, job_id, session_root=Path(tmp) / "sessions")
            self.assertTrue(api_readback_confirms_write(JobStore(db), job_id))
            self.assertEqual(audit["recording_motion"]["result"], "FAIL")
            self.assertIn("RECORDING", audit["recording_motion"]["reason"])
            self.assertTrue(audit["recording_motion"]["supporting_only"])
            self.assertEqual(audit["tool_vs_recording"]["result"], "MISMATCH")
            self.assertTrue(audit["tool_vs_recording"]["supporting_only"])
            self.assertEqual(audit["verdict"], "PASS")
            self.assertFalse(audit["authorizes_complete"])

    def test_unconfirmed_write_still_fails_on_the_recording(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = self._job(db, confirm=False)
            store = JobStore(db)
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": DISCUSSION,
                    "note_text": NOTE,
                    "read_back": True,
                },
            )
            self.assertFalse(api_readback_confirms_write(store, job_id))
            audit = run_post_job_audit(db, job_id, session_root=Path(tmp) / "sessions")
            self.assertEqual(audit["recording_motion"]["result"], "FAIL")
            self.assertNotIn("supporting_only", audit["recording_motion"])
            self.assertEqual(audit["verdict"], "FAIL")
            self.assertFalse(audit["authorizes_complete"])

    def test_engine_stops_the_recording_before_the_audit_reads_it(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"applicant_id": APPLICANT, "account_name": "Buster Brown", "text": "add a note"},
            )
            job_id = job["id"]
            store.checkpoint(
                job_id,
                "gateway_progress",
                {
                    "first_at": "2026-09-30T12:00:00+00:00",
                    "last_at": "2026-09-30T12:01:00+00:00",
                },
            )
            store.checkpoint(
                job_id,
                "action",
                {
                    "destination": {
                        "applicant_id": APPLICANT,
                        "discussion_id": DISCUSSION,
                        "discussion_title": TITLE,
                        "note_text": NOTE,
                    }
                },
            )
            recording = _recording(db, job_id, "RECORDING")
            events = []

            class Spy:
                enabled = False

                def safe_stop(self, current_job_id, status):
                    events.append("safe_stop")
                    RecordingStore(db).update(
                        recording["id"],
                        status="READY",
                        drive_url="https://drive.google.com/file/d/round8/view",
                        drive_file_id="round8",
                    )

                def stop_and_upload(self, current_job_id, status):
                    events.append("stop_and_upload")
                    return {"status": "READY", "drive_url": "https://drive.google.com/file/d/round8/view"}

                def release_local_after_audit(self, current_job_id):
                    events.append("release")

                def completion_error(self, current_job_id):
                    return None

            engine = JobEngine(
                store,
                {},
                {
                    "hermes.google_chat_task": HermesChatEzlynxDestinationVerifier(
                        _DiscussionPort(_note_record())
                    )
                },
                recordings=Spy(),
            )
            with patch(
                "robie_job_engine.playwright_observability.maybe_snapshot_and_bind",
                return_value=None,
            ), patch(
                "robie_job_engine.playwright_observability.persist_cdp_snapshot",
                return_value={},
            ), patch(
                "robie_job_engine.tab_cleanup.maybe_cleanup_terminal_job_tabs",
                return_value={"ok": True},
            ):
                final = engine.run(job_id)
            self.assertEqual(final["status"], JobStatus.COMPLETE.value, final.get("last_error"))
            self.assertIn("safe_stop", events)
            self.assertLess(events.index("safe_stop"), events.index("release"))
            audit = store.get_checkpoint(job_id, "post_job_audit")
            self.assertEqual(audit["recording_motion"]["status"], "READY")
            self.assertNotIn("RECORDING", str(audit["recording_motion"].get("reason") or ""))
            self.assertFalse(audit["authorizes_complete"])


class ChatNoteStatusTests(unittest.TestCase):
    def _finish(self, db, job_id):
        store = JobStore(db)
        current = store.get_job(job_id)
        if current["status"] == JobStatus.RUNNING.value:
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
        elif current["status"] == JobStatus.PENDING.value:
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.PENDING},
                error="fixture",
                release_lease=True,
            )

    def test_confirmed_note_is_one_plain_sentence_plus_the_ref(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-note-ok",
                "Add a note on Buster Brown",
                conversation_id="spaces/note-ok",
            )
            store = JobStore(db)
            payload = dict(store.get_job(job_id)["payload"])
            payload["account_name"] = "Buster Brown"
            store.update_payload(job_id, payload)
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": DISCUSSION,
                    "discussion_title": "follw\u200b up 1",
                    "applicant_id": APPLICANT,
                    "note_text": NOTE,
                    "read_back": True,
                },
            )
            _confirming_evidence(store, job_id)
            self._finish(db, job_id)
            with patch(
                "robie_job_engine.playwright_observability.persist_cdp_snapshot",
                return_value={},
            ), patch(
                "robie_job_engine.tab_cleanup.maybe_cleanup_terminal_job_tabs",
                return_value={"ok": True},
            ):
                response = guard_chat_response(db, job_id, "I added the note")
            stop_generic_chat_job_heartbeat(db, job_id)
            lines = [line for line in response.strip().splitlines() if line.strip()]
            self.assertEqual(
                lines,
                [
                    'Done. Added a note to Buster Brown on "follw up 1": '
                    f'"{NOTE}". I re-checked EZLynx and it\'s there.',
                    f"Ref: job {job_id}",
                ],
            )
            self.assertNotIn("\u200b", response)
            for banned in (
                "Details",
                "What happened",
                "Technical detail",
                "Applicant ID",
                "Discussion API",
                "Heartbeat",
                "Recording motion",
                "Playwright",
                "ROBIE post-job audit",
            ):
                self.assertNotIn(banned, response)
            stored = store.get_checkpoint(job_id, "post_job_audit")
            self.assertIsNotNone(stored)
            self.assertIn("ROBIE post-job audit", format_audit_chat_message(stored))
            posted = []
            audit_terminal_job(
                db,
                job_id,
                chat_poster=lambda audit, text: posted.append(text),
            )
            self.assertEqual(len(posted), 1)
            self.assertIn("Heartbeat gateway_progress", posted[0])
            self.assertIn("Recording motion", posted[0])

    def test_tool_readback_alone_is_not_confirmation(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-note-miss",
                "Add a note on Buster Brown",
                conversation_id="spaces/note-miss",
            )
            store = JobStore(db)
            payload = dict(store.get_job(job_id)["payload"])
            payload["account_name"] = "Buster Brown"
            store.update_payload(job_id, payload)
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": DISCUSSION,
                    "discussion_title": TITLE,
                    "note_text": NOTE,
                    "read_back": True,
                },
            )
            self._finish(db, job_id)
            with patch(
                "robie_job_engine.playwright_observability.persist_cdp_snapshot",
                return_value={},
            ), patch(
                "robie_job_engine.tab_cleanup.maybe_cleanup_terminal_job_tabs",
                return_value={"ok": True},
            ):
                response = guard_chat_response(db, job_id, "I added the note")
            stop_generic_chat_job_heartbeat(db, job_id)
            lines = [line for line in response.strip().splitlines() if line.strip()]
            self.assertEqual(
                lines,
                [
                    'I tried to add the note to Buster Brown on "follw up 1" '
                    "but couldn't confirm it landed. Please check before counting it done.",
                    f"Ref: job {job_id}",
                ],
            )
            self.assertNotIn("I re-checked EZLynx", response)
            self.assertNotIn("Details", response)
            self.assertNotIn("ROBIE post-job audit", response)

    def test_address_note_keeps_the_csr_sentence(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-address",
                "Change the mailing address for Buster Brown",
                conversation_id="spaces/address",
            )
            store = JobStore(db)
            store.checkpoint(
                job_id,
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": DISCUSSION,
                    "discussion_title": TITLE,
                    "note_text": NOTE,
                    "read_back": True,
                },
            )
            self._finish(db, job_id)
            with patch(
                "robie_job_engine.playwright_observability.persist_cdp_snapshot",
                return_value={},
            ), patch(
                "robie_job_engine.tab_cleanup.maybe_cleanup_terminal_job_tabs",
                return_value={"ok": True},
            ):
                response = guard_chat_response(db, job_id, "done")
            stop_generic_chat_job_heartbeat(db, job_id)
            self.assertIn("I noted the request on follw up 1", response)
            self.assertNotIn("Done. Added a note", response)

    def test_merge_puts_note_keys_on_the_action_and_leaves_note_id_off(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "add a note", "applicant_id": APPLICANT},
            )
            store.checkpoint(
                job["id"],
                "action",
                {"destination": {"applicant_id": APPLICANT, "policy_number": ""}},
            )
            store.checkpoint(
                job["id"],
                "discussion_note",
                {
                    "discussion_id": DISCUSSION,
                    "discussion_title": TITLE,
                    "note_text": NOTE,
                    "note_id": "should-not-become-the-identity",
                    "applicant_id": APPLICANT,
                    "status": "filed",
                },
            )
            _merge_discussion_note_destination(store, job["id"])
            destination = store.get_checkpoint(job["id"], "action")["destination"]
            self.assertEqual(destination["discussion_id"], DISCUSSION)
            self.assertEqual(destination["note_text"], NOTE)
            self.assertNotIn("note_id", destination)
            self.assertEqual(
                intended_destination_identity(
                    action={"destination": destination},
                    payload={"applicant_id": APPLICANT},
                ),
                DISCUSSION,
            )


if __name__ == "__main__":
    unittest.main()
