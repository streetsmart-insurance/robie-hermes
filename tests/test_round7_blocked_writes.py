"""Round 7: a plan that locks, an honest reply, and a recording that stops."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory
from robie_job_engine.answer_only import (
    FIXTURE_POLICY_MARKER,
    FORBIDDEN_READ_RULE,
    LIVE_LOOKUP_FAILED,
    scrub_user_reply,
)
from robie_job_engine.chat_guard import (
    _release_chat_recording,
    guard_chat_response,
    open_chat_job,
)
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import (
    BUSY_SESSION_REPLY,
    busy_session_should_defer,
    incoming_message_action,
)
from robie_job_engine.engine import JobEngine
from robie_job_engine.hitl import structured_blocker_reason
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingManager
from robie_job_engine.store import JobStore
from robie_job_engine.write_verification_loop import (
    PLAN_MAX_OUTPUT_TOKENS,
    PLAN_REQUIRED,
    parse_model_plan,
    plan_is_locked,
    prepare_write_plan,
    refuse_tool_write,
    repair_truncated_json,
)

ROOT = Path(__file__).resolve().parents[1]
TRUNCATED = (
    '{"write":"mailing address","target":"26356199","values":{"mailingAddress":"100 Main"'
)
COMPLETE_PLAN = {
    "write": "mailing address",
    "target": {"applicant_id": "26356199", "policy_number": "HO-100"},
    "values": {"mailingAddress": "100 Test Mailing Rd"},
}


class FakeCapture:
    def start(self, output_path: Path, stop_file: Path) -> int:
        return 4242

    def stop(self, pid: int, stop_file: Path, output_path: Path) -> None:
        stop_file.touch()
        output_path.write_bytes(b"fake-webm")


class FakeUploader:
    def upload(self, path: Path, file_name: str) -> tuple[str, str]:
        return "drive-file", "https://drive.google.com/file/d/drive-file/view"


class _Watch:
    def done(self) -> bool:
        return False


class _Source:
    def __init__(self, user_id: str) -> None:
        self.chat_id = "spaces/clarify"
        self.thread_id = "thread"
        self.user_id = user_id


class _Event:
    def __init__(self, user_id: str) -> None:
        self.source = _Source(user_id)


class _Adapter:
    def __init__(self, job_id: str) -> None:
        self.gateway_runner = None
        self._gateway_turns = {
            ("spaces/clarify", "thread"): {
                "job_id": job_id,
                "task": None,
                "watchdog": _Watch(),
            }
        }


class Round7PlanTests(unittest.TestCase):
    def setUp(self):
        tmp = durable_temporary_directory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "jobs.db")
        self.store = JobStore(self.db)

    def _job(self, text="Change the mailing address"):
        return self.store.create_job("ezlynx.policy_change", {"text": text})

    def test_plan_call_allows_a_full_json_object(self):
        source = (ROOT / "robie_job_engine/write_verification_loop.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(PLAN_MAX_OUTPUT_TOKENS, 4096)
        self.assertIn('"maxOutputTokens": PLAN_MAX_OUTPUT_TOKENS', source)
        self.assertNotIn('"maxOutputTokens": 512', source)
        self.assertIn('"responseMimeType": "application/json"', source)
        self.assertIn('"responseSchema"', source)

    def test_truncated_json_is_repaired_and_retried_once(self):
        repaired = json.loads(repair_truncated_json(TRUNCATED))
        self.assertEqual(repaired["target"], "26356199")
        self.assertEqual(repaired["values"]["mailingAddress"], "100 Main")
        parsed = parse_model_plan(TRUNCATED)
        self.assertEqual(parsed["target"]["applicant_id"], "26356199")
        self.assertNotIn("policy_number", parsed["target"])
        calls = []

        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return TRUNCATED
            return json.dumps(COMPLETE_PLAN)

        locked = prepare_write_plan(self.store, self._job(), model)
        self.assertEqual(len(calls), 2)
        self.assertTrue(locked["locked"])
        self.assertEqual(locked["values"]["mailingAddress"], "100 Test Mailing Rd")

    def test_string_target_locks_and_the_refusal_names_the_field(self):
        job = self._job()
        kwargs = {"job_id": job["id"], "db_path": self.db}
        refused = refuse_tool_write(
            {"plan": {"write": "note", "target": "", "values": {"body": "hello"}}},
            kwargs,
        )
        self.assertIn("The plan field target is wrong", refused)
        self.assertNotEqual(refused, PLAN_REQUIRED)
        self.assertFalse(plan_is_locked(self.store, job["id"]))
        allowed = refuse_tool_write(
            {
                "plan": {
                    "write": "note",
                    "target": "26356199",
                    "values": {"body": "hello"},
                }
            },
            kwargs,
        )
        self.assertIsNone(allowed)
        locked = self.store.get_checkpoint(job["id"], "write_plan")
        self.assertEqual(locked["target"]["applicant_id"], "26356199")
        discussion = self._job(text="File a holder note")
        self.assertIsNone(
            refuse_tool_write(
                {
                    "plan": {
                        "write": "holder note",
                        "target": "Certificate Request",
                        "values": {"holder": "Robie Test Holder"},
                    }
                },
                {"job_id": discussion["id"], "db_path": self.db},
            )
        )
        named = self.store.get_checkpoint(discussion["id"], "write_plan")
        self.assertEqual(named["target"]["discussion"], "Certificate Request")

    def test_the_same_refusal_stops_after_two(self):
        job = self._job(text="File the holder note")
        kwargs = {"job_id": job["id"], "db_path": self.db}
        bad = {"plan": {"write": "note", "values": {"body": "hello"}}}
        first = refuse_tool_write(bad, kwargs)
        second = refuse_tool_write(bad, kwargs)
        third = refuse_tool_write(bad, kwargs)
        self.assertIn("The plan field target is wrong", first)
        self.assertIn("Pass plan with write", first)
        self.assertIn("Stop. Do not call this tool again.", second)
        self.assertIn("Nothing was changed or noted", second)
        self.assertNotIn("Pass plan with write", second)
        self.assertEqual(third, second)
        self.assertFalse(plan_is_locked(self.store, job["id"]))
        self.store.transition(
            job["id"],
            JobStatus.UNVERIFIED,
            expected={JobStatus.PENDING},
            error="plan refused",
        )
        reply = guard_chat_response(
            self.db, job["id"], "The certificate holder note has been prepared."
        )
        self.assertTrue(reply.startswith("Nothing was changed or noted."))
        self.assertNotIn("\n", reply.strip())
        self.assertIn("target", reply)
        self.assertNotIn("has been prepared", reply)
        other = self._job(text="Change a street address")
        self.store.transition(
            other["id"],
            JobStatus.UNVERIFIED,
            expected={JobStatus.PENDING},
            error="plan was not locked",
        )
        pasted = guard_chat_response(self.db, other["id"], json.dumps(COMPLETE_PLAN))
        self.assertTrue(pasted.startswith("Nothing was changed or noted."))
        self.assertNotIn("100 Test Mailing Rd", pasted)


class Round7ReplyTests(unittest.TestCase):
    def test_fixture_policy_is_not_reported_as_an_agency_record(self):
        self.assertIn("tests", FORBIDDEN_READ_RULE)
        self.assertIn("test files", FORBIDDEN_READ_RULE)
        self.assertIn("live EZLynx lookup fails", FORBIDDEN_READ_RULE)
        raw = (
            f"Account 220250093 has {FIXTURE_POLICY_MARKER} with Travelers."
        )
        self.assertEqual(scrub_user_reply(raw), LIVE_LOOKUP_FAILED)
        self.assertNotIn(FIXTURE_POLICY_MARKER, scrub_user_reply(raw))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.plain_english",
                {"text": "Which policies are on this account?", "answer_only": True},
            )
            reply = guard_chat_response(db, job["id"], raw)
        self.assertNotIn(FIXTURE_POLICY_MARKER, reply)
        self.assertNotIn("Travelers", reply)
        self.assertIn("live EZLynx lookup failed", reply)

    def test_internal_markers_are_stripped_after_blocker_detection(self):
        raw = "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: the submit control was not found"
        reason = structured_blocker_reason(raw)
        self.assertIn("PLAYWRIGHT_BLOCKED", reason)
        cleaned = scrub_user_reply(raw)
        self.assertNotIn("ROBIE_BLOCKED", cleaned)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", cleaned)
        self.assertIn("submit control was not found", cleaned)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.plain_english",
                {"text": "What is a deductible?", "answer_only": True},
            )
            store.transition(
                job["id"],
                JobStatus.UNVERIFIED,
                expected={JobStatus.PENDING},
                error=raw,
            )
            reply = guard_chat_response(db, job["id"], raw)
        self.assertNotIn("ROBIE_BLOCKED", reply)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", reply)
        self.assertNotIn("Robie_BLOCKED", reply)


class Round7RecordingTests(unittest.TestCase):
    def _manager(self, db: str, root: Path) -> RecordingManager:
        return RecordingManager(
            db,
            root=root / "recordings",
            capture=FakeCapture(),
            uploader=FakeUploader(),
            enabled=True,
        )

    def test_recording_stops_when_the_verifier_already_set_unverified(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            db = str(root / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.policy_change",
                {"text": "Change the mailing address"},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.checkpoint(job["id"], "action", {"destination": {}})
            manager = self._manager(db, root)
            manager.start(job["id"])
            self.assertEqual(manager.store.active(job["id"])["status"], "RECORDING")

            def _run(engine, job_id):
                current = engine.store.get_job(job_id)
                engine.store.transition(
                    job_id,
                    JobStatus.UNVERIFIED,
                    expected={JobStatus(current["status"])},
                    error="verifier already unverified",
                    release_lease=True,
                )
                return engine.store.get_job(job_id)

            with patch.object(JobEngine, "run", _run):
                reply = guard_chat_response(
                    db,
                    job["id"],
                    "The note has been prepared.",
                    verifiers={"ezlynx.policy_change": lambda *args, **kwargs: None},
                    recordings=manager,
                )
            self.assertIsNone(manager.store.active(job["id"]))
            self.assertNotEqual(manager.store.latest(job["id"])["status"], "RECORDING")
            self.assertTrue(reply.startswith("Nothing was changed or noted."))

    def test_an_in_progress_reply_leaves_the_recording_open(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            db = str(root / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "Please finish the form"},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            manager = self._manager(db, root)
            manager.start(job["id"])
            reply = guard_chat_response(
                db,
                job["id"],
                "still working",
                recordings=manager,
                verifiers={"not.this.action": lambda *args, **kwargs: None},
            )
            self.assertIn("still working", reply.casefold())
            self.assertEqual(manager.store.active(job["id"])["status"], "RECORDING")
            self.assertIn("finally:", (ROOT / "robie_job_engine/chat_guard.py").read_text())
            _release_chat_recording(db, job["id"], manager)
            self.assertEqual(manager.store.active(job["id"])["status"], "RECORDING")


class Round7ClarifyTests(unittest.TestCase):
    def test_a_waiting_job_takes_the_next_message_from_any_sender(self):
        self.assertEqual(
            incoming_message_action(session_busy=True, is_stop=False), "defer"
        )
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-vague",
                "@Robie can you do a book for me",
                conversation_id="spaces/clarify",
            )
            store = JobStore(db)
            self.assertEqual(store.get_job(job_id)["status"], "NEEDS_CLARIFICATION")
            adapter = _Adapter(job_id)
            for sender in ("users/probe-carlo", "users/carlo"):
                self.assertFalse(
                    busy_session_should_defer(adapter, _Event(sender), db_path=db)
                )
            store.transition(
                job_id, JobStatus.RUNNING, expected={JobStatus.NEEDS_CLARIFICATION}
            )
            self.assertTrue(
                busy_session_should_defer(
                    adapter, _Event("users/probe-carlo"), db_path=db
                )
            )
            store.transition(
                job_id,
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.RUNNING},
                error="Which account?",
            )
            thread = "spaces/clarify/threads/book"
            bind_job_chat_thread(store, job_id, thread)
            continued = open_chat_job(
                db,
                "message-account",
                "26356199",
                conversation_id="spaces/clarify",
                inbound_thread_id=thread,
            )
            self.assertEqual(continued, job_id)
            self.assertIn("User reply: 26356199", store.get_job(job_id)["payload"]["text"])
            adapter_source = (ROOT / "integrations/google_chat/adapter.py").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "busy_session_should_defer(self, event, db_path=ROBIE_JOB_DB)",
                adapter_source,
            )
            self.assertEqual(BUSY_SESSION_REPLY, "I'm finishing another job, one moment.")


if __name__ == "__main__":
    unittest.main()
