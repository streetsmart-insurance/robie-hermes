"""Regressions from the 2026-10-02 Prod round that never reached Test.

The agent rewrote the allowlist inside playwright_exec and filed a note.
Prod's DiscussionApi path wrote while TEST held the lease. A landed note
was described as blocked. An ambiguous discussion was picked. A name-only
ask kept fixture applicant 220250093. 'look up john smith' asked for an
applicant id. A cancelled job stayed an active conversation link.
"""

from __future__ import annotations

import json

import pytest
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory


@pytest.fixture(autouse=True)
def _empty_ascend_api_notice_store(tmp_path, monkeypatch):
    from robie_job_engine import ascend_api_notice_source as source

    path = tmp_path / "api-notice" / "events.db"
    source.EventKeyStore(path)
    monkeypatch.setenv(source.DB_ENV, str(path))

from robie_job_engine.chat_guard import _chat_blocker_redirect
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.chat_turn_control import fail_cancelled_chat_job
from robie_job_engine.client_name_lookup import (
    prepare_named_client_lookup,
    prepare_named_write_client,
    set_client_name_searcher,
)
from robie_job_engine.ezlynx_discussions import (
    DiscussionSelectionError,
    file_note_to_existing_discussion,
    select_discussion_for_note,
)
from robie_job_engine.ezlynx_driver_gate import EzlynxDriverGateRefused
from robie_job_engine.write_verification_loop import refuse_tool_write
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingManager
from robie_job_engine.store import JobStore

ROOT = Path(__file__).resolve().parents[1]


def _running(store: JobStore, text: str, **extra: object) -> str:
    payload = {
        "text": text,
        "request_text": text,
        "original_text": text,
        "conversation_id": "spaces/AAQAZbLJO78",
        "requested_by": "Carlo",
    }
    payload.update(extra)
    job = store.create_job("hermes.google_chat_task", payload)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    return job["id"]


class _OneDiscussion:
    def __init__(self) -> None:
        self.posts = 0
        self.posted = False

    def get_discussions(self, applicant_id: str):
        return [{"discussionId": "848144886", "title": "follw up 1", "applicantId": applicant_id}]

    def get_discussion(self, discussion_id: str):
        return {
            "discussionId": discussion_id,
            "title": "follw up 1",
            "noteCount": 32 if self.posted else 31,
            "mostRecentNoteId": "1134306435" if self.posted else "1",
        }

    def append_note(self, discussion_id: str, text: str, note_type: str = "Note"):
        del discussion_id, text, note_type
        self.posts += 1
        self.posted = True
        return {"noteId": "1134306435"}


class SealAndGateTests(unittest.TestCase):
    def test_agent_monkeypatch_of_the_allowlist_still_refuses_the_write(self) -> None:
        script = r"""
import sys
from robie_job_engine.safety_seal import SafetySealError, install_agent_seal

install_agent_seal()
import robie_job_engine.ezlynx_api_only_writes as writes
import robie_job_engine.ezlynx_discussions as discussions
import robie_job_engine.ezlynx_write_scope as scope

scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS = scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS | {"26356199"}
writes.note_id_in_discussion = lambda record, note_id: True
discussions._note_id_of = lambda row: "1134306319"

called = {"n": 0}

def urlopen(*_args, **_kwargs):
    called["n"] += 1
    raise RuntimeError("http")

from robie_job_engine.ezlynx_discussions import DiscussionApiClient, DiscussionApiConfig

client = DiscussionApiClient(
    DiscussionApiConfig(
        discussion_base_url="https://app.ezlynx.com/DiscussionApi/",
        token_endpoint="https://app.ezlynx.com/token",
        client_id="id",
        client_secret="secret",
        username="user",
        integration_group_id="1",
    ),
    urlopen=urlopen,
)
try:
    discussions.file_note_to_existing_discussion(
        client, "26356199", "Round 27 zombie test - this must never be filed."
    )
except SafetySealError as exc:
    if "allowlist" not in str(exc).casefold() and "replaced" not in str(exc).casefold():
        sys.stderr.write(str(exc))
        raise SystemExit(3)
except Exception as exc:
    sys.stderr.write(type(exc).__name__ + ": " + str(exc))
    raise SystemExit(2)
else:
    raise SystemExit(4)
if called["n"] != 0:
    raise SystemExit(5)
try:
    sys.audit("socket.connect", object(), ("app.ezlynx.com", 443))
except SafetySealError:
    pass
else:
    raise SystemExit(6)
sys.audit("socket.connect", object(), ("127.0.0.1", 9222))
raise SystemExit(0)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + str(ROOT / "tests")
        env.pop("ROBIE_EZLYNX_WRITE_APPLICANT_IDS", None)
        env.pop("EZLYNX_WRITE_APPLICANT_IDS", None)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:] + proc.stdout[-500:])

    def test_discussion_api_refuses_when_test_holds_the_lease(self) -> None:
        lease = json.dumps(
            {
                "version": 1,
                "state": "IN",
                "holder": "TEST",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        )
        client = _OneDiscussion()
        with patch.dict(
            os.environ,
            {
                "ROBIE_EZLYNX_DRIVER_GATE_REQUIRED": "1",
                "ROBIE_EZLYNX_DRIVER_HOLDER": "PRODUCTION",
            },
        ), patch(
            "robie_job_engine.ezlynx_driver_gate.read_metadata",
            return_value=lease,
        ):
            with self.assertRaises(EzlynxDriverGateRefused):
                file_note_to_existing_discussion(
                    client, "220250093", "Robie was here"
                )
        self.assertEqual(client.posts, 0)


class HonestNoteTests(unittest.TestCase):
    def test_a_landed_note_is_recorded_and_reported_when_a_later_step_blocks(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            text = "Add a note for Buster Brown applicant 220250093 on follw up 1"
            job_id = _running(store, text)
            client = _OneDiscussion()
            with patch.dict(os.environ, {"ROBIE_JOB_DB": db, "ROBIE_JOB_ID": job_id}):
                result = file_note_to_existing_discussion(
                    client,
                    "220250093",
                    "called back about the round 27 renewal. Robie was here",
                    title_hint="follw up 1",
                    ledger_path=str(Path(tmp) / "ledger.json"),
                )
            self.assertEqual(result["status"], "filed")
            self.assertEqual(result["note_id"], "1134306435")
            saved = store.get_checkpoint(job_id, "discussion_note") or {}
            self.assertEqual(saved.get("note_id"), "1134306435")
            reply = _chat_blocker_redirect(
                store,
                store.get_job(job_id),
                "PLAYWRIGHT_BLOCKED: browser step blocked",
                db_path=db,
                recordings=RecordingManager(db),
            )
            self.assertIn("Added the note", reply or "")
            self.assertNotIn("browser step blocked", (reply or "").casefold())
            self.assertNotIn("did not finish", (reply or "").casefold())


class DiscussionChoiceTests(unittest.TestCase):
    def test_an_invented_title_does_not_pick_a_discussion(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            text = "Add a note to Buster Brown 26356199: Round 27 test"
            job_id = _running(store, text, applicant_id="26356199")
            rows = [
                {"discussionId": "848110016", "title": "Message Received by Robie"},
                {"discussionId": "848144886", "title": "follw up 1"},
            ]
            with patch.dict(os.environ, {"ROBIE_JOB_DB": db, "ROBIE_JOB_ID": job_id}):
                with self.assertRaises(DiscussionSelectionError) as caught:
                    select_discussion_for_note(
                        rows, title_hint="Message Received by Robie"
                    )
            self.assertEqual(caught.exception.code, "AMBIGUOUS_DISCUSSIONS")
            self.assertIn("follw up 1", caught.exception.matches)
            self.assertIn("Message Received by Robie", caught.exception.matches)


class FixtureAccountTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_client_name_searcher(None)

    def test_a_name_only_request_does_not_keep_the_fixture_account(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            text = "Add a note for Buster Brown saying he will call back Fri."
            job_id = _running(store, text, applicant_id="220250093")
            set_client_name_searcher(
                lambda name: {
                    "status": "ok",
                    "matches": [{"applicant_id": "26356199", "name": "Buster Brown"}],
                }
            )
            prepare_named_write_client(store, job_id)
            self.assertEqual(store.get_job(job_id)["payload"]["applicant_id"], "26356199")
            store.update_payload(
                job_id,
                {**store.get_job(job_id)["payload"], "applicant_id": "220250093"},
            )
            store.checkpoint(
                job_id,
                "write_plan",
                {
                    "locked": True,
                    "write": "discussion note",
                    "target": {"applicant_id": "220250093", "discussion": "follw up 1"},
                    "values": {"note_text": "Robie was here"},
                },
            )
            refused = refuse_tool_write(
                {
                    "applicant_id": "220250093",
                    "note_text": "Robie was here",
                    "title_hint": "follw up 1",
                    "plan": {
                        "write": "discussion note",
                        "target": {"applicant_id": "220250093", "discussion": "follw up 1"},
                        "values": {"note_text": "Robie was here"},
                    },
                },
                {"job_id": job_id, "db_path": db},
            )
            self.assertIn("EZLYNX_APPLICANT_UNTRUSTED", refused or "")
            self.assertIn("220250093", refused or "")


class JohnSmithTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_client_name_searcher(None)

    def test_look_up_john_smith_lists_matches(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "look up john smith")
            set_client_name_searcher(
                lambda name: {
                    "status": "ok",
                    "matches": [
                        {"applicant_id": "111", "name": "John Smith"},
                        {"applicant_id": "222", "name": "John Smith"},
                    ],
                }
            )
            line = prepare_named_client_lookup(store, job_id)
            self.assertIn("John Smith", line or "")
            self.assertIn("Which one", line or "")
            self.assertNotIn("applicant id", (line or "").casefold())
            reply = _chat_blocker_redirect(
                store,
                store.get_job(job_id),
                "MISSING_REQUIRED_FIELD: applicant_id",
                db_path=db,
                recordings=RecordingManager(db),
            )
            self.assertNotIn("applicant id", (reply or "").casefold())
            self.assertIsNone(store.get_checkpoint(job_id, "human_input_wait"))


class AscendGateTests(unittest.TestCase):
    def test_notice_driver_does_not_file_when_test_holds_the_lease(self) -> None:
        from test_ascend_notice_driver import make_ctx, make_notice, policy_row

        from robie_job_engine import ascend_notice_driver as driver
        from robie_job_engine.ezlynx_driver_gate import EzlynxDriverGateRefused

        ctx, discussion = make_ctx(
            notices=[make_notice()],
            policy_rows={"HO-998877": [policy_row()]},
            dry_run=False,
        )

        def _refused() -> None:
            raise EzlynxDriverGateRefused(
                "EZLYNX_DRIVER_NOT_IN: driver belongs to TEST"
            )

        with patch("robie_job_engine.safety_seal.driver_gate_for_write", _refused):
            result = driver.process_notice(make_notice(), ctx)
        self.assertIn("driver_gate_refused", result.reason)
        self.assertEqual(getattr(discussion, "posts", 0) or 0, 0)


class CancelLinkTests(unittest.TestCase):
    def test_cancel_clears_the_active_conversation_link(self) -> None:
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _running(store, "look up john smith")
            queue = DurableChatEventQueue(db)
            queue.link_conversation_job(
                conversation_id="spaces/AAQAZbLJO78",
                job_id=job_id,
                message_id="spaces/AAQAZbLJO78/messages/1",
                event_id="spaces/AAQAZbLJO78/messages/1",
            )
            self.assertEqual(queue.active_conversation_job("spaces/AAQAZbLJO78")["job_id"], job_id)
            fail_cancelled_chat_job(store, job_id)
            self.assertIsNone(queue.active_conversation_job("spaces/AAQAZbLJO78"))
            self.assertIn(
                store.get_job(job_id)["status"],
                {JobStatus.CANCELLED.value, JobStatus.FAILED.value},
            )


if __name__ == "__main__":
    unittest.main()
