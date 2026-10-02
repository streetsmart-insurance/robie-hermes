"""Regressions from the 2026-10-01 Test round on PR #725.

A cancelled turn wrote an EZLynx note after /stop, under a different job.
These tests pin the six fixes from that NO-GO.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import _render_chat_terminal, open_chat_job
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import (
    ALREADY_FINISHED_REPLY,
    register_chat_adapter,
    request_agent_stop,
    stop_session_keys,
)
from robie_job_engine.client_name_lookup import (
    prepare_named_write_client,
    set_client_name_searcher,
)
from robie_job_engine.ezlynx_discussions import file_note_to_existing_discussion
from robie_job_engine.live_turn_guard import (
    CLARIFY_TIMEOUT_STOP,
    accept_clarify_thread_reply,
    acting_job_id,
    bind_turn_owner,
    clarify_job_for_reply,
    contains_clarify_timeout,
    end_job_after_clarify_timeout,
    format_complete_answer,
    neutralize_clarify_timeout,
    note_clarify_pending,
    refuse_account_file_search,
    refuse_live_write,
    reset_turn_job,
    set_turn_job,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingManager
from robie_job_engine.store import JobStore
from robie_job_engine.write_verification_loop import refuse_tool_write
from test_round10_reply_lifecycle import (
    SPACE,
    THREAD,
    _Event,
    _adapter_module,
    _chat,
    _outbound_text,
)


def _running(store: JobStore, text: str, *, thread: str | None = THREAD) -> str:
    job = store.create_job(
        "hermes.google_chat_task",
        {
            "text": text,
            "request_text": text,
            "original_text": text,
            "conversation_id": SPACE,
            "requested_by": "Carlo",
        },
    )
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    if thread:
        bind_job_chat_thread(store, job["id"], thread)
    return job["id"]


class _NoteClient:
    def __init__(self) -> None:
        self.appended = False

    def get_discussions(self, applicant_id: str):
        del applicant_id
        return [{"discussionId": "843110603", "title": "follw up 1"}]

    def get_discussion(self, discussion_id: str):
        return {
            "discussionId": discussion_id,
            "title": "follw up 1",
            "noteCount": 4,
            "mostRecentNoteId": "1",
        }

    def append_note(self, discussion_id: str, text: str, note_type: str = "Note"):
        del discussion_id, text, note_type
        self.appended = True
        return {"noteId": "1134303997"}


class WriteAfterStopTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_turn_job("")

    def test_cancelled_turn_cannot_write_and_is_not_credited_to_the_new_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            owner = _running(store, "Add a note for Buster Brown on follw up 1")
            store.transition(
                owner,
                JobStatus.CANCELLED,
                expected={JobStatus.RUNNING},
                error="Cancelled.",
                release_lease=True,
            )
            other = _running(store, "A later request about the renewal")
            agent = type("Agent", (), {})()
            bind_turn_owner(agent, owner)
            token = set_turn_job(owner)
            self.addCleanup(lambda: reset_turn_job(token))
            env = {"ROBIE_JOB_ID": other, "ROBIE_CURRENT_JOB_ID": other, "ROBIE_JOB_DB": db}
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(acting_job_id({"job_id": other}, agent), owner)
                refused = refuse_live_write({"db_path": db, "job_id": other})
                self.assertIn(owner, refused or "")
                self.assertIn("CANCELLED", refused or "")
                self.assertNotIn(other, refused or "")
                client = _NoteClient()
                with self.assertRaises(RuntimeError) as caught:
                    file_note_to_existing_discussion(
                        client,
                        "220250093",
                        "Client called about renewal. Robie was here.",
                        title_hint="follw up 1",
                    )
            self.assertIn("EZLYNX_WRITE_REFUSED", str(caught.exception))
            self.assertFalse(client.appended)
            self.assertIsNone(store.get_checkpoint(other, "discussion_note"))

    def test_clarify_timeout_ends_the_job_and_does_not_choose(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "Add a note for Buster Brown")
            raw = "[user did not respond within 60m]"
            self.assertTrue(contains_clarify_timeout(raw))
            self.assertEqual(end_job_after_clarify_timeout(store, job_id), CLARIFY_TIMEOUT_STOP)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.FAILED.value)
            self.assertFalse(store.get_checkpoint(job_id, "clarify_timeout")["chose"])
            shown = neutralize_clarify_timeout(
                {"content": raw, "nested": ["pick Renewal Manual Policy"]}
            )
            self.assertNotIn("60m", str(shown))
            self.assertIn("Do not choose a discussion", str(shown))
            self.assertIn("EZLYNX_WRITE_REFUSED", refuse_live_write({"db_path": db, "job_id": job_id}) or "")

    def test_terminal_cancel_releases_the_thread_lease_and_fails_clarify(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "Add a note for Buster Brown")
            chat = _chat(db)
            agent_key = f"agent:main:google_chat:dm:{SPACE}/threads/job"
            released: list[str] = []

            class _Lease:
                def release(self) -> None:
                    released.append(agent_key)

            chat.gateway_runner._running_agents[agent_key] = object()
            chat.gateway_runner._active_session_leases = {agent_key: _Lease()}
            cleared: list[str] = []
            chat.clear_pending_clarify = cleared.append
            register_chat_adapter(chat)
            chat._gateway_turns[(SPACE, THREAD)] = {"job_id": job_id, "task": None}
            store.transition(
                job_id,
                JobStatus.CANCELLED,
                expected={JobStatus.RUNNING},
                error="Cancelled.",
                release_lease=True,
            )
            self.assertIn(agent_key, released)
            self.assertIn(agent_key, cleared)
            from robie_job_engine.chat_turn_control import agent_stop_requested

            self.assertTrue(agent_stop_requested(job_id))


class StopKeyTests(unittest.TestCase):
    def test_top_level_stop_uses_the_thread_agent_key(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "Add a note for Buster Brown")
            chat = _chat(db)
            agent_key = f"agent:main:google_chat:dm:{SPACE}/threads/job"
            chat.gateway_runner._running_agents[agent_key] = object()
            event = _Event(None, "/stop")
            resolved, derived, keys = stop_session_keys(chat, event, job_id, store)
            self.assertEqual(resolved, agent_key)
            self.assertIn(agent_key, keys)
            self.assertNotEqual(resolved, derived)
            self.assertTrue(str(derived).startswith("chat:"))

    def test_in_thread_stop_kills_a_live_turn_on_a_cancelled_job(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "Add a note for Buster Brown")
            store.transition(
                job_id,
                JobStatus.CANCELLED,
                expected={JobStatus.RUNNING},
                error="Cancelled.",
                release_lease=True,
            )
            request_agent_stop(job_id)
            chat = _chat(db)
            agent_key = f"agent:main:google_chat:dm:{SPACE}/threads/job"
            chat.gateway_runner._running_agents[agent_key] = object()
            seen: list[str] = []

            async def _terminate(event, stopped_job, *, reason):
                del event
                seen.append(f"{stopped_job}:{reason}")

            chat._terminate_running_agent = _terminate
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(chat._apply_chat_stop(_Event(THREAD, "/stop")))
            self.assertEqual(seen, [f"{job_id}:/stop"])
            posted = _outbound_text(chat)
            self.assertTrue(posted)
            self.assertNotIn(ALREADY_FINISHED_REPLY, posted)


class ClarifyReplyTests(unittest.TestCase):
    def test_thread_reply_resumes_the_clarify_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "Add a note for Buster Brown. Which discussion?")
            note_clarify_pending(store, job_id, "Which discussion?")
            reply_id = "spaces/ROBY/messages/follw"
            resumed = open_chat_job(
                db,
                reply_id,
                "use follw up 1",
                requested_by="Carlo",
                conversation_id=SPACE,
                inbound_thread_id=THREAD,
            )
            self.assertEqual(resumed, job_id)
            self.assertEqual(len(store.list_jobs_by_status(set(JobStatus))), 1)
            saved = store.get_checkpoint(job_id, "clarification_reply") or {}
            self.assertEqual(saved.get("text"), "use follw up 1")
            injected = store.get_checkpoint(job_id, "clarify_reply_injected") or {}
            self.assertEqual(injected.get("message_id"), reply_id)
            self.assertEqual(
                clarify_job_for_reply(store, "use follw up 1", THREAD)["id"]
                if clarify_job_for_reply(store, "use follw up 1", THREAD)
                else job_id,
                job_id,
            )


class AccountSourceTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_client_name_searcher(None)

    def test_search_files_cannot_supply_an_applicant(self):
        self.assertIn(
            "FILE_SEARCH_BLOCKED",
            refuse_account_file_search("search_files", {"query": "Buster Brown"}) or "",
        )
        self.assertIn(
            "FILE_SEARCH_BLOCKED",
            refuse_account_file_search("read_file", {"path": "/opt/releases/old/fixtures/buster.json"})
            or "",
        )
        self.assertIsNone(refuse_account_file_search("ezlynx_discussion_note", {}))

    def test_a_name_is_searched_instead_of_asking_for_an_id(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            text = "Add a note for Buster Brown saying he will call back Fri."
            job_id = _running(store, text, thread=None)
            set_client_name_searcher(
                lambda name: {
                    "status": "ok",
                    "matches": [{"applicant_id": "26356199", "name": "Buster Brown"}],
                }
            )
            line = prepare_named_write_client(store, job_id)
            self.assertIsNone(line)
            self.assertEqual(store.get_job(job_id)["payload"]["applicant_id"], "26356199")
            refused = refuse_tool_write(
                {
                    "applicant_id": "buster brown",
                    "note_text": "He will call back Fri. Robie was here.",
                    "title_hint": "follw up 1",
                    "plan": {
                        "write": "discussion note",
                        "target": {"applicant_id": "buster brown", "discussion": "follw up 1"},
                        "values": {"note_text": "He will call back Fri. Robie was here."},
                    },
                },
                {"job_id": job_id, "db_path": db},
            )
            self.assertTrue(refused is None or "applicant id" not in (refused or "").casefold() or "do not ask" in (refused or "").casefold())
            if refused:
                self.assertNotIn("Could you please provide the EZLynx Applicant ID", refused)
                self.assertIn("Do not ask the user for an applicant id", refused)

    def test_file_applicant_is_refused_when_the_request_names_a_person(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "Add a note for Buster Brown on follw up 1", thread=None)
            store.checkpoint(
                job_id,
                "write_plan",
                {
                    "locked": True,
                    "write": "discussion note",
                    "target": {"discussion": "follw up 1", "applicant_id": "26356199"},
                    "values": {"note_text": "Robie was here"},
                },
            )
            refused = refuse_tool_write(
                {
                    "applicant_id": "220250093",
                    "note_text": "Robie was here",
                    "title_hint": "follw up 1",
                },
                {"job_id": job_id, "db_path": db},
            )
            self.assertIn("EZLYNX_APPLICANT_UNTRUSTED", refused or "")
            self.assertIn("220250093", refused or "")


class AnswerTextTests(unittest.TestCase):
    def test_chat_close_keeps_the_policy_and_drops_the_answered_prefix(self):
        answer = (
            "The General Liability (GL) policies on record for Buster Brown "
            "(26356199) in EZLynx are both Inactive. "
            "Policy 768578657 is with Rocklake MGA / Great American."
        )
        shown = format_complete_answer(f"Answered. {answer}")
        self.assertFalse(shown.lower().startswith("answered"))
        self.assertIn("768578657", shown)
        self.assertIn("Rocklake", shown)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "What is the GL policy number and carrier for Buster Brown?",
                    "request_text": "What is the GL policy number and carrier for Buster Brown?",
                    "answer_only": True,
                },
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            rendered = _render_chat_terminal(
                store,
                store.get_job(job["id"]),
                answer,
                RecordingManager(db),
            )
            self.assertNotIn("Answered.", rendered)
            self.assertIn("768578657", rendered)
            self.assertIn("Great American", rendered)


class ClarifyAcceptTests(unittest.TestCase):
    def test_accept_does_not_require_a_new_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "Which discussion?")
            note_clarify_pending(store, job_id, "Which discussion?")
            job = store.get_job(job_id)
            self.assertTrue(
                accept_clarify_thread_reply(store, job, "use follw up 1", "m1")
            )
            self.assertEqual(
                (store.get_checkpoint(job_id, "clarification_reply") or {}).get("text"),
                "use follw up 1",
            )


if __name__ == "__main__":
    unittest.main()
