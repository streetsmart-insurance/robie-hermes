"""Regressions from the 2026-10-02 Test round on head 4230ae8.

Chat kept the first paragraph of a GL answer, asked the user to sign in
before trying the stored credentials, and a top-level /stop left the
running job going. Tool text still showed applicant 220250093, an
unlisted sender got a CSR line, and DiscussionApi 404s were followed by
guessed paths.
"""

from __future__ import annotations

import asyncio
import io
import json
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib import error

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import _sign_in_after_recovery
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import (
    SENDER_REFUSED,
    clear_agent_stop,
    sender_is_allowed,
)
from robie_job_engine.ezlynx_account_nav import install_account_nav_guard
from robie_job_engine.ezlynx_discussions import (
    DiscussionApiClient,
    DiscussionApiConfig,
    DiscussionApiError,
    arm_discussion_api_call,
    reset_discussion_api_misses,
)
from robie_job_engine.ezlynx_driver_gate import EzlynxDriverGateRefused
from robie_job_engine.live_turn_guard import (
    format_complete_answer,
    refuse_tab_applicant,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.playwright_observability import record_playwright_exec
from robie_job_engine.session_recovery import prepare_chat_sign_in
from robie_job_engine.store import JobStore
from robie_job_engine.user_reply import SIGN_IN_QUESTION, format_outbound_reply, format_user_reply
from robie_job_engine.worker_contract import UNVERIFIED_STUCK_TEXT, sanitize_worker_response
from robie_job_engine.write_verification_loop import refuse_tool_write
from test_round10_reply_lifecycle import (
    FRESH,
    SPACE,
    THREAD,
    _Event,
    _adapter_module,
    _chat,
    _outbound_text,
)

API_BASE = "https://app.uatezlynx.com/DiscussionApi/"
TOKEN_URL = "https://identity.example.com/connect/token"
GL_ANSWER = (
    "I identified general liability as coverage for bodily injury and "
    "property damage claims that a third party brings against the insured.\n\n"
    "The second paragraph keeps form CG 00 01, the each-occurrence limit, "
    "and the general aggregate. "
    + ("Premises, operations, products, and completed operations stay in the answer. " * 30)
)


def _question_job() -> dict:
    return {
        "action_type": "hermes.google_chat_task",
        "payload": {
            "answered": True,
            "answer_only": True,
            "request_text": "What does general liability cover?",
            "text": "What does general liability cover?",
        },
    }


class _Agent:
    def __init__(self) -> None:
        self.alive = True
        self.reasons: list[str] = []

    def interrupt(self, reason: str = "") -> None:
        self.reasons.append(reason)
        self.alive = False


class _Turn:
    def __init__(self) -> None:
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True


class FullAnswerTests(unittest.TestCase):
    def test_question_keeps_every_paragraph_and_skips_the_destination_rewrite(self):
        self.assertGreater(len(GL_ANSWER), 1555)
        # format_user_reply defaults to collapse=True and keeps statements[0].
        # That line, capped at 400, is what Chat posted instead of the answer.
        collapsed = format_user_reply(GL_ANSWER)
        self.assertNotIn("second paragraph", collapsed)
        self.assertLess(len(collapsed), 400)

        shown = format_complete_answer(GL_ANSWER)
        self.assertIn("second paragraph", shown)
        self.assertIn("CG 00 01", shown)
        self.assertGreater(len(shown), 900)

        delivered = format_outbound_reply(GL_ANSWER, _question_job())
        self.assertIn("second paragraph", delivered)
        self.assertIn("CG 00 01", delivered)
        self.assertGreater(len(delivered), 900)

        status = (
            "Saved the mailing address on the account.\n\n"
            "The prior street was 1 Main Street."
        )
        write_job = {
            "action_type": "ezlynx.policy_change",
            "payload": {"request_text": "Change the mailing address"},
        }
        self.assertNotIn("1 Main Street", format_outbound_reply(status, write_job))

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            question = store.create_job(
                "hermes.google_chat_task",
                {
                    "request_text": "What does general liability cover?",
                    "text": "What does general liability cover?",
                },
            )
            kept = sanitize_worker_response(store, question["id"], GL_ANSWER)
            self.assertFalse(kept["rewritten"])
            self.assertIn("second paragraph", kept["response_text"])
            self.assertNotIn("no verified destination progress", kept["response_text"])

            write = store.create_job(
                "ezlynx.policy_change",
                {
                    "request_text": "Change the mailing address",
                    "text": "Change the mailing address",
                },
            )
            rewritten = sanitize_worker_response(store, write["id"], GL_ANSWER)
            self.assertTrue(rewritten["rewritten"])
            self.assertEqual(rewritten["response_text"], UNVERIFIED_STUCK_TEXT)

    def test_adapter_chunks_a_long_answer_instead_of_cutting_it(self):
        adapter = _adapter_module()
        chat = _chat(":memory:")
        body = "General liability. " * 400
        self.assertGreater(len(body), adapter._MAX_TEXT_LENGTH)
        chunks = chat._chunk_text(body)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual("".join(chunks), body)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), adapter._MAX_TEXT_LENGTH)


class SignInRecoveryTests(unittest.TestCase):
    def test_gate_refusal_does_not_sign_in_or_ask(self):
        calls: list[str] = []

        def gate() -> None:
            raise EzlynxDriverGateRefused("lease held elsewhere")

        def recover() -> dict:
            calls.append("recover")
            return {"recovered": True}

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {"text": "file a note", "request_text": "file a note"},
            )
            decision = prepare_chat_sign_in(
                store, job["id"], gate=gate, recover=recover
            )
            self.assertEqual(decision, "refused")
            self.assertEqual(calls, [])
            note = store.get_checkpoint(job["id"], "session_recovery") or {}
            self.assertTrue(note.get("refused"))
            self.assertFalse(note.get("recovered"))

    def test_recovered_session_asks_for_a_rerun(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {"text": "file a note", "request_text": "file a note"},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            decision = prepare_chat_sign_in(
                store,
                job["id"],
                gate=lambda: None,
                recover=lambda: {"recovered": True, "state": "app"},
            )
            self.assertEqual(decision, "rerun")
            rerun = store.get_checkpoint(job["id"], "session_recovery_rerun") or {}
            self.assertTrue(rerun.get("rerun"))
            with patch(
                "robie_job_engine.session_recovery.prepare_chat_sign_in",
                return_value="rerun",
            ):
                reply = _sign_in_after_recovery(
                    store, job["id"], db_path=db, recordings=None
                )
            self.assertEqual(reply, "")
            self.assertNotIn(SIGN_IN_QUESTION, reply)
            self.assertEqual(
                store.get_job(job["id"])["status"], JobStatus.RUNNING.value
            )

    def test_failed_recovery_is_the_only_time_we_ask(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {"text": "file a note", "request_text": "file a note"},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            decision = prepare_chat_sign_in(
                store,
                job["id"],
                gate=lambda: None,
                recover=lambda: {"recovered": False, "reason": "still logged out"},
            )
            self.assertEqual(decision, "ask")
            # The suite does not launch a browser when neither callable is passed.
            self.assertEqual(prepare_chat_sign_in(store, job["id"]), "ask")
            with patch(
                "robie_job_engine.session_recovery.prepare_chat_sign_in",
                return_value="refused",
            ):
                reply = _sign_in_after_recovery(
                    store, job["id"], db_path=db, recordings=None
                )
            self.assertIn("does not hold the EZLynx driver", reply)
            self.assertNotIn(SIGN_IN_QUESTION, reply)
            self.assertNotEqual(
                store.get_job(job["id"])["status"],
                JobStatus.NEEDS_CLARIFICATION.value,
            )


class TopLevelStopTests(unittest.TestCase):
    def test_top_level_stop_cancels_the_running_job_and_aborts_its_agent(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            running = store.create_job(
                "ezlynx.discussion_note",
                {
                    "text": "file a note on the checkup discussion",
                    "request_text": "file a note on the checkup discussion",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(
                running["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            bind_job_chat_thread(store, running["id"], THREAD)
            parked = store.create_job(
                "browser.read",
                {
                    "text": "read the dec page",
                    "request_text": "read the dec page",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(
                parked["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            store.transition(
                parked["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.RUNNING},
                error="browser read requires a destination locator",
                release_lease=True,
            )
            bind_job_chat_thread(store, parked["id"], "spaces/ROBY/threads/parked")
            other = store.create_job(
                "ezlynx.discussion_note",
                {
                    "text": "file a note somewhere else",
                    "request_text": "file a note somewhere else",
                    "conversation_id": "spaces/OTHER",
                    "requested_by": "Carlo",
                },
            )
            store.transition(
                other["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            chat = _chat(db)
            agent = _Agent()
            other_agent = _Agent()
            running_key = "agent:main:google_chat:dm:spaces/ROBY:job"
            other_key = "agent:main:google_chat:dm:spaces/OTHER:job"
            chat._active_sessions = {running_key: object(), other_key: object()}
            chat.gateway_runner._running_agents = {
                running_key: agent,
                other_key: other_agent,
            }
            turn = _Turn()
            chat._gateway_turns[(SPACE, THREAD)] = {
                "job_id": running["id"],
                "task": turn,
            }
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    asyncio.run(chat._apply_chat_stop(_Event(FRESH, "/stop")))
            finally:
                clear_agent_stop(running["id"])
                clear_agent_stop(parked["id"])
            self.assertEqual(
                store.get_job(running["id"])["status"], JobStatus.CANCELLED.value
            )
            self.assertEqual(
                store.get_job(parked["id"])["status"], JobStatus.CANCELLED.value
            )
            self.assertEqual(
                store.get_job(other["id"])["status"], JobStatus.RUNNING.value
            )
            lines = _outbound_text(chat)
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith("Stopped. That job is cancelled."))
            self.assertFalse(agent.alive)
            self.assertTrue(other_agent.alive)
            self.assertNotIn(running_key, chat._active_sessions)
            self.assertIn(other_key, chat._active_sessions)
            self.assertTrue(turn.cancelled)

    def test_in_thread_stop_leaves_another_threads_running_job(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            parked = store.create_job(
                "browser.read",
                {
                    "text": "read the dec page",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(
                parked["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            store.transition(
                parked["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.RUNNING},
                error="browser read requires a destination locator",
                release_lease=True,
            )
            bind_job_chat_thread(store, parked["id"], THREAD)
            running = store.create_job(
                "ezlynx.discussion_note",
                {
                    "text": "file a note on the checkup discussion",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(
                running["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            bind_job_chat_thread(store, running["id"], "spaces/ROBY/threads/other-job")
            chat = _chat(db)
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    asyncio.run(chat._apply_chat_stop(_Event(THREAD, "/stop")))
            finally:
                clear_agent_stop(parked["id"])
                clear_agent_stop(running["id"])
            self.assertEqual(
                store.get_job(parked["id"])["status"], JobStatus.CANCELLED.value
            )
            self.assertEqual(
                store.get_job(running["id"])["status"], JobStatus.RUNNING.value
            )
            self.assertEqual(len(_outbound_text(chat)), 1)


class TabApplicantTests(unittest.TestCase):
    def test_tool_text_has_no_example_id_and_a_tab_id_is_refused(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("ezlynx_note_tool.py", "ezlynx_document_tool.py"):
            text = (root / "deploy" / "hermes" / "tools" / name).read_text()
            self.assertNotIn("220250093", text)
            self.assertIn("open browser tab", text)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {
                    "text": "Add a note for Buster Brown",
                    "request_text": "Add a note for Buster Brown",
                },
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            record_playwright_exec(
                "page.goto('https://app.ezlynx.com/web/account/220250093/policies')",
                {"url": "https://app.ezlynx.com/web/account/220250093/policies"},
                job_id=job["id"],
                db_path=db,
            )
            loaded = store.get_job(job["id"])
            tab = refuse_tab_applicant(store, loaded, "220250093")
            self.assertIsNotNone(tab)
            self.assertIn("EZLYNX_APPLICANT_UNTRUSTED", tab or "")
            self.assertIn("browser tab", tab or "")
            refused = refuse_tool_write(
                {"applicant_id": "220250093", "note_text": "Robie was here"},
                {"job_id": job["id"], "db_path": db},
            )
            self.assertIn("EZLYNX_APPLICANT_UNTRUSTED", refused or "")
            self.assertIn("browser tab", refused or "")

            typed = store.create_job(
                "ezlynx.discussion_note",
                {
                    "text": "Add a note for Buster Brown applicant 220250093",
                    "request_text": "Add a note for Buster Brown applicant 220250093",
                },
            )
            store.transition(
                typed["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            record_playwright_exec(
                "page.goto('https://app.ezlynx.com/web/account/220250093/policies')",
                {"url": "https://app.ezlynx.com/web/account/220250093/policies"},
                job_id=typed["id"],
                db_path=db,
            )
            self.assertIsNone(
                refuse_tab_applicant(store, store.get_job(typed["id"]), "220250093")
            )


class SenderAllowlistTests(unittest.TestCase):
    def test_allowlist_is_checked_before_a_job_opens(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_CHAT_ALLOWED_USERS": "", "GOOGLE_CHAT_ALLOW_ALL_USERS": ""},
            clear=False,
        ):
            self.assertTrue(sender_is_allowed("users/stranger", "Stranger"))
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CHAT_ALLOWED_USERS": "other@example.com",
                "GOOGLE_CHAT_ALLOW_ALL_USERS": "1",
            },
            clear=False,
        ):
            self.assertTrue(sender_is_allowed("users/stranger", "Stranger"))
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CHAT_ALLOWED_USERS": "carlo@example.com",
                "GOOGLE_CHAT_ALLOW_ALL_USERS": "",
            },
            clear=False,
        ):
            self.assertTrue(sender_is_allowed("users/carlo", "carlo@example.com"))
            self.assertFalse(sender_is_allowed("users/stranger", "Stranger"))

        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            chat = _chat(str(Path(tmp) / "jobs.db"))
            with patch.dict(
                os.environ,
                {
                    "GOOGLE_CHAT_ALLOWED_USERS": "other@example.com",
                    "GOOGLE_CHAT_ALLOW_ALL_USERS": "",
                },
                clear=False,
            ):
                with patch.object(adapter, "open_chat_job") as opened:
                    asyncio.run(
                        chat._open_and_run_chat_job(
                            _Event(THREAD, "please file a note"),
                            "please file a note",
                        )
                    )
            opened.assert_not_called()
            lines = _outbound_text(chat)
            self.assertEqual(lines, [SENDER_REFUSED])
            self.assertNotIn("csr", lines[0].casefold())


class _Body:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw


class DiscussionMissTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_discussion_api_misses()

    def tearDown(self) -> None:
        reset_discussion_api_misses()

    def _client(self, urlopen):
        config = DiscussionApiConfig(
            discussion_base_url=API_BASE,
            token_endpoint=TOKEN_URL,
            client_id="id",
            client_secret="secret",
            username="user",
            integration_group_id="159",
        )
        return DiscussionApiClient(
            config, urlopen=urlopen, session_headers=lambda url: {}
        )

    def test_two_misses_stop_the_next_call_before_http(self):
        calls: list[str] = []

        def urlopen(url, data=None, headers=None, timeout=None):
            del data, headers, timeout
            calls.append(str(url))
            if "connect/token" in url:
                return _Body({"access_token": "tok", "expires_in": 3600})
            raise error.HTTPError(
                url, 404, "missing", Message(), io.BytesIO(b"missing")
            )

        client = self._client(urlopen)
        with self.assertRaises(DiscussionApiError):
            client.get_discussion_ids("26356199")
        with self.assertRaises(DiscussionApiError):
            client.get_discussions("26356199")
        discussion_calls = [url for url in calls if "v8/discussions" in url]
        self.assertEqual(len(discussion_calls), 2)
        before = len(calls)
        with self.assertRaises(DiscussionApiError) as caught:
            client.get_discussion("1134306319")
        self.assertIn("Do not guess another path", str(caught.exception))
        self.assertEqual(len(calls), before)

    def test_a_different_path_after_one_miss_does_not_go_out(self):
        calls: list[str] = []

        def urlopen(url, data=None, headers=None, timeout=None):
            del data, headers, timeout
            calls.append(str(url))
            if "connect/token" in url:
                return _Body({"access_token": "tok", "expires_in": 3600})
            raise error.HTTPError(
                url, 405, "method", Message(), io.BytesIO(b"no")
            )

        client = self._client(urlopen)
        with self.assertRaises(DiscussionApiError):
            client._get("v8/discussions/ids-by-applicant", {"applicantId": "1"})
        before = len(calls)
        with self.assertRaises(DiscussionApiError) as caught:
            client._get("v8/discussions/search-by-name", {"name": "Buster"})
        self.assertIn("Do not guess another path", str(caught.exception))
        self.assertIn("Report this error", str(caught.exception))
        self.assertEqual(len(calls), before)

    def test_one_known_miss_still_allows_the_next_known_path(self):
        calls: list[str] = []

        def urlopen(url, data=None, headers=None, timeout=None):
            del data, headers, timeout
            calls.append(str(url))
            if "connect/token" in url:
                return _Body({"access_token": "tok", "expires_in": 3600})
            if "by-applicant" in url and "ids-by-applicant" not in url:
                return _Body([])
            raise error.HTTPError(
                url, 404, "missing", Message(), io.BytesIO(b"missing")
            )

        client = self._client(urlopen)
        with self.assertRaises(DiscussionApiError):
            client.get_discussion_ids("26356199")
        rows = client.get_discussions("26356199")
        self.assertEqual(rows, [])
        # The successful read cleared the miss, so a later known call still goes out.
        before = len([url for url in calls if "v8/discussions" in url])
        client.get_discussions("26356199")
        after = len([url for url in calls if "v8/discussions" in url])
        self.assertEqual(after, before + 1)

    def test_page_goto_stops_guessing_discussion_paths(self):
        seen: list[str] = []

        class Page:
            def goto(self, url, *args, **kwargs):
                del args, kwargs
                seen.append(str(url))
                return type("Response", (), {"status": 404})()

        scope = {"Page": Page, "_robie_task_text": ""}
        install_account_nav_guard(scope)
        page = Page()
        first = f"{API_BASE}v8/discussions/ids-by-applicant?applicantId=1"
        second = f"{API_BASE}v8/discussions/by-applicant?applicantId=1"
        guessed = f"{API_BASE}v8/discussions/search-by-name"
        page.goto(first)
        page.goto(second)
        with self.assertRaises(DiscussionApiError) as caught:
            page.goto(guessed)
        self.assertIn("Do not guess another path", str(caught.exception))
        self.assertEqual(seen, [first, second])
        # arm itself, with the counter already at the limit, raises too.
        with self.assertRaises(DiscussionApiError):
            arm_discussion_api_call(first)


if __name__ == "__main__":
    unittest.main()
