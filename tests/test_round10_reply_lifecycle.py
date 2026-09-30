"""Round 10: close the chat when the reply is out, and keep worker words off it."""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_app_post import post_hitl_to_originating_thread
from robie_job_engine.chat_guard import (
    guard_chat_response,
    notify_terminal_chat_job,
    open_chat_job,
)
from robie_job_engine.chat_job_controls import (
    LEFT_AS_IS,
    close_declined_note_repost,
    consume_note_repost_allowance,
    hard_block_reply,
    note_repost_confirmed_by_reply,
    settle_job_when_reply_sent,
)
from robie_job_engine.chat_thread import bind_job_chat_thread
from robie_job_engine.chat_turn_control import (
    NOTHING_RUNNING_REPLY,
    STOPPED_AFTER_TEN_MINUTES,
    busy_session_should_defer,
    session_is_busy,
)
from robie_job_engine.discussion_note_ledger import already_added_question
from robie_job_engine.ezlynx_session import (
    APP_WEB_URL,
    LOGIN_URL,
    PlaywrightEzlynxSession,
    SessionState,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingManager
from robie_job_engine.store import JobStore
from robie_job_engine.user_reply import format_user_reply

ROOT = Path(__file__).resolve().parents[1]
SPACE = "spaces/ROBY"
THREAD = "spaces/ROBY/threads/job"
OTHER = "spaces/ROBY/threads/other"
FRESH = "spaces/ROBY/threads/brand-new"


def _install_gateway_stub() -> None:
    if "gateway.platforms.base" in sys.modules:
        return
    gateway = types.ModuleType("gateway")
    config = types.ModuleType("gateway.config")
    config.Platform = lambda value: value
    config.PlatformConfig = type("PlatformConfig", (), {})
    platforms = types.ModuleType("gateway.platforms")
    helpers = types.ModuleType("gateway.platforms.helpers")

    class MessageDeduplicator:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

    helpers.MessageDeduplicator = MessageDeduplicator
    base = types.ModuleType("gateway.platforms.base")

    class BasePlatformAdapter:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

    class SendResult:
        def __init__(
            self,
            success: bool = False,
            error: str | None = None,
            retryable: bool = False,
            raw_response: dict | None = None,
            message_id: str | None = None,
        ) -> None:
            self.success = success
            self.error = error
            self.retryable = retryable
            self.raw_response = raw_response
            self.message_id = message_id

    def _unused(*args, **kwargs):
        del args, kwargs
        return None

    base.BasePlatformAdapter = BasePlatformAdapter
    base.MessageEvent = type("MessageEvent", (), {})
    base.MessageType = type("MessageType", (), {"TEXT": "text"})
    base.ProcessingOutcome = type("ProcessingOutcome", (), {})
    base.SendResult = SendResult
    base.cache_audio_from_bytes = _unused
    base.cache_document_from_bytes = _unused
    base.cache_image_from_bytes = _unused
    base.cache_video_from_bytes = _unused
    sys.modules["gateway"] = gateway
    sys.modules["gateway.config"] = config
    sys.modules["gateway.platforms"] = platforms
    sys.modules["gateway.platforms.helpers"] = helpers
    sys.modules["gateway.platforms.base"] = base


def _adapter_module():
    _install_gateway_stub()
    import integrations.google_chat.adapter as adapter

    return adapter


class _Live:
    def done(self) -> bool:
        return False


class _Source:
    def __init__(self, thread_id: str | None, user_name: str = "Carlo") -> None:
        self.chat_id = SPACE
        self.thread_id = thread_id
        self.user_name = user_name
        self.user_id = "users/carlo"


class _Event:
    def __init__(self, thread_id: str | None, text: str = "hello") -> None:
        self.source = _Source(thread_id)
        self.message_id = "spaces/ROBY/messages/in"
        self.text = text
        self.raw_message = {}


class _Counts:
    def incr(self, *args, **kwargs) -> None:
        del args, kwargs


class _Exec:
    def __init__(self, body: dict) -> None:
        self.body = body

    def execute(self, http=None):
        del http
        thread = (self.body.get("thread") or {}).get("name") or THREAD
        return {"name": "spaces/ROBY/messages/out", "thread": {"name": thread}}


class _Messages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Exec(kwargs.get("body") or {})


class _Spaces:
    def __init__(self, messages: _Messages) -> None:
        self._messages = messages

    def messages(self) -> _Messages:
        return self._messages


class _Api:
    def __init__(self) -> None:
        self.messages = _Messages()
        self._spaces = _Spaces(self.messages)

    def spaces(self) -> _Spaces:
        return self._spaces


class _Runner:
    def __init__(self) -> None:
        self._running_agents: dict = {}
        self._sessions: dict = {}


def _chat(db: str):
    adapter = _adapter_module()
    chat = adapter.GoogleChatAdapter.__new__(adapter.GoogleChatAdapter)
    chat._typing_messages = {}
    chat._active_chat_job = {}
    chat._gateway_turns = {}
    chat._rate_limit_hits = {}
    chat._thread_count_store = _Counts()
    chat._chat_api = _Api()
    chat._last_inbound_thread = {}
    chat.gateway_runner = _Runner()
    chat.pause_typing_for_chat = lambda chat_id: None
    chat.resume_typing_for_chat = lambda chat_id: None
    chat._new_authed_http = lambda: object()
    chat._db = db
    return chat


def _outbound_text(chat) -> list[str]:
    return [
        str((call.get("body") or {}).get("text") or "")
        for call in chat._chat_api.messages.calls
    ]


def _assert_clean(test: unittest.TestCase, text: str, *job_ids: str) -> None:
    folded = text.casefold()
    test.assertNotIn("robie_blocked", folded)
    test.assertNotIn("playwright_blocked", folded)
    test.assertNotIn("ref: job", folded)
    test.assertNotIn("missing_required_field", folded)
    for job_id in job_ids:
        if job_id:
            test.assertNotIn(job_id, text)


class _Page:
    def __init__(self, url: str) -> None:
        self.url = url
        self.body = ""
        self.login_controls = 0
        self.web_links = 0
        self.form_visible = False
        self.form_after_reload = False
        self.signed_in_after_app = False
        self.stay_on_login = False
        self.gotos: list[str] = []
        self.reloads = 0
        self.filled: list[tuple[str, str]] = []
        self.clicked: list[str] = []

    def goto(self, url: str, wait_until: str | None = None) -> None:
        del wait_until
        self.gotos.append(url)
        self.url = url
        if self.signed_in_after_app and "ezlynx.com/web" in url:
            self.url = "https://app.ezlynx.com/web/home"
            self.web_links = 4
            self.login_controls = 0
            self.body = "home"
        elif self.stay_on_login and "ezlynx.com/web" in url:
            self.url = "https://app.ezlynx.com/auth/account/login"
            self.body = ""

    def reload(self, wait_until: str | None = None) -> None:
        del wait_until
        self.reloads += 1
        if self.form_after_reload:
            self.form_visible = True

    def locator(self, selector: str):
        return _Locator(self, selector)

    def wait_for_load_state(self, *args, **kwargs) -> None:
        del args, kwargs

    def wait_for_timeout(self, ms: int) -> None:
        del ms


class _Locator:
    def __init__(self, page: _Page, selector: str) -> None:
        self.page = page
        self.selector = selector

    def inner_text(self, timeout: int | None = None) -> str:
        del timeout
        return self.page.body

    def count(self) -> int:
        if "txtUserName" in self.selector:
            return self.page.login_controls
        if "/web/" in self.selector:
            return self.page.web_links
        return 0

    def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        del state, timeout
        if self.page.form_visible:
            return
        raise TimeoutError("blank login page")

    def fill(self, value: str) -> None:
        self.page.filled.append((self.selector, value))

    def click(self) -> None:
        self.page.clicked.append(self.selector)


class ReplyCloseTests(unittest.TestCase):
    def test_final_reply_marks_the_job_terminal_and_drops_the_lock(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "add a note", "conversation_id": SPACE},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            bind_job_chat_thread(store, job["id"], THREAD)
            chat = _chat(db)
            key = ("agent:main:google_chat:dm:" + SPACE)
            chat.gateway_runner._running_agents[key] = object()
            chat._gateway_turns[(SPACE, THREAD)] = {
                "job_id": job["id"],
                "task": _Live(),
                "watchdog": _Live(),
            }
            chat._active_chat_job[SPACE] = job["id"]
            with patch.object(adapter, "ROBIE_JOB_DB", db), patch.object(
                adapter, "guard_chat_response", return_value="Added the note.\n"
            ):
                result = asyncio.run(
                    chat.send(
                        SPACE,
                        "Execution Summary\nnote is verified",
                        reply_to="spaces/ROBY/messages/in",
                        metadata={"robie_job_id": job["id"], "thread_id": FRESH},
                    )
                )
            self.assertTrue(result.success)
            finished = store.get_job(job["id"])
            self.assertEqual(finished["status"], JobStatus.UNVERIFIED.value)
            self.assertFalse(finished.get("lease_owner"))
            self.assertEqual(chat._gateway_turns, {})
            self.assertNotIn(SPACE, chat._active_chat_job)
            self.assertNotIn(key, chat.gateway_runner._running_agents)
            event = _Event(THREAD)
            self.assertFalse(session_is_busy(chat, event))

    def test_in_progress_line_keeps_the_lock(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("hermes.google_chat_task", {"text": "working"})
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            self.assertFalse(
                settle_job_when_reply_sent(db, job["id"], "Still working on it.")
            )
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.RUNNING.value)

    def test_waiting_thread_is_not_the_busy_reply(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            running = store.create_job(
                "hermes.google_chat_task",
                {"text": "add a note on Buster", "conversation_id": SPACE},
            )
            store.transition(
                running["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            waiting = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "which client?",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(
                waiting["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.PENDING},
                release_lease=True,
            )
            bind_job_chat_thread(store, waiting["id"], THREAD)
            chat = _chat(db)
            chat.gateway_runner._running_agents[
                "agent:main:google_chat:dm:" + SPACE
            ] = object()
            chat._gateway_turns[(SPACE, OTHER)] = {
                "job_id": running["id"],
                "task": _Live(),
                "watchdog": _Live(),
            }
            self.assertTrue(session_is_busy(chat, _Event(THREAD)))
            self.assertFalse(
                busy_session_should_defer(chat, _Event(THREAD), db_path=db)
            )
            self.assertTrue(
                busy_session_should_defer(chat, _Event(FRESH), db_path=db)
            )


class PrefixAndThreadTests(unittest.TestCase):
    def test_blocked_waiting_stop_and_sweeper_text_stays_clean(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            blocked = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "add the holder",
                    "conversation_id": SPACE,
                    "thread_id": THREAD,
                },
            )
            store.transition(
                blocked["id"], JobStatus.RUNNING, expected={JobStatus.PENDING}
            )
            store.transition(
                blocked["id"],
                JobStatus.AWAITING_HUMAN_INPUT,
                expected={JobStatus.RUNNING},
                error="ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: the submit control was not found",
                release_lease=True,
            )
            bind_job_chat_thread(store, blocked["id"], THREAD)
            chat = _chat(db)
            raw = (
                "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: the submit control was not found\n"
                f"Ref: job {blocked['id']}"
            )
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(
                    chat.send(
                        SPACE,
                        raw,
                        metadata={
                            "robie_job_id": blocked["id"],
                            "thread_id": FRESH,
                        },
                    )
                )
                asyncio.run(
                    chat.send(
                        SPACE,
                        "ROBIE_BLOCKED: MISSING_REQUIRED_FIELD: client name",
                        metadata={
                            "robie_job_id": blocked["id"],
                            "robie_delivery_kind": "notice",
                        },
                    )
                )
                asyncio.run(
                    chat.send(
                        SPACE,
                        STOPPED_AFTER_TEN_MINUTES,
                        metadata={
                            "robie_job_id": blocked["id"],
                            "robie_stop_notice": True,
                            "robie_delivery_kind": "ceiling",
                            "thread_id": FRESH,
                        },
                    )
                )
                asyncio.run(
                    chat.send(
                        SPACE,
                        "Stopped. That job is cancelled. Ref: job "
                        + blocked["id"],
                        metadata={
                            "robie_job_id": blocked["id"],
                            "robie_stop_notice": True,
                            "robie_delivery_kind": "stop",
                        },
                    )
                )
            for text in _outbound_text(chat):
                _assert_clean(self, text, blocked["id"])
            self.assertTrue(any("got stuck" in text.casefold() for text in _outbound_text(chat)))
            self.assertTrue(
                any(text == STOPPED_AFTER_TEN_MINUTES for text in _outbound_text(chat))
            )
            ceiling = chat._chat_api.messages.calls[2]
            self.assertEqual(ceiling["messageReplyOption"], "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD")
            self.assertEqual(ceiling["body"]["thread"]["name"], THREAD)

            posted: list[str] = []

            def poster(space, message, thread_name=None, thread_key=None):
                del space, thread_name, thread_key
                posted.append(message)
                return {"name": "spaces/ROBY/messages/hitl"}

            post_hitl_to_originating_thread(
                "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: resume from checkpoint\n"
                f"Ref: job {blocked['id']}",
                job_id=blocked["id"],
                store=store,
                db_path=db,
                poster=poster,
            )
            failed = store.create_job(
                "hermes.google_chat_task",
                {"text": "sweep me", "conversation_id": SPACE, "thread_id": THREAD},
            )
            store.transition(failed["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                failed["id"],
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error=f"ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: sweeper {failed['id']}",
                release_lease=True,
            )
            bind_job_chat_thread(store, failed["id"], THREAD)
            notify_terminal_chat_job(db, failed["id"], poster=poster)
            for text in posted:
                _assert_clean(self, text, blocked["id"], failed["id"])

    def test_final_write_uses_the_stored_thread_name(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {
                    "text": "add a note",
                    "account_name": "Buster Brown",
                    "conversation_id": SPACE,
                },
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                job["id"],
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="reply sent",
                release_lease=True,
            )
            store.checkpoint(
                job["id"],
                "discussion_note",
                {
                    "status": "filed",
                    "discussion_id": "d-1",
                    "discussion_title": "follw up 1",
                    "note_text": "follw up 1",
                    "read_back": True,
                    "note_id": "new-note",
                },
            )
            store.checkpoint(
                job["id"],
                "discussion_note_readback",
                {"matched": True, "note_id": "new-note"},
            )
            bind_job_chat_thread(store, job["id"], THREAD)
            chat = _chat(db)
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(
                    chat.send(
                        SPACE,
                        "Execution Summary\nWhat happened: the note is verified",
                        metadata={"robie_job_id": job["id"], "thread_id": FRESH},
                    )
                )
            call = chat._chat_api.messages.calls[0]
            self.assertEqual(call["messageReplyOption"], "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD")
            self.assertEqual(call["body"]["thread"], {"name": THREAD})
            text = call["body"]["text"]
            self.assertEqual(text, 'Added the note to Buster Brown on "follw up 1".')
            self.assertNotIn("Execution Summary", text)
            _assert_clean(self, text, job["id"])


class LedgerStopAndBlockTests(unittest.TestCase):
    def test_repeat_note_question_is_visible_and_yes_posts_once(self):
        question = already_added_question("2026-09-30T19:15:00+00:00")
        self.assertIn("I already added that note at ", question)
        self.assertTrue(question.endswith("Want me to add it again?"))
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {
                    "text": "add a note",
                    "account_name": "Buster Brown",
                    "conversation_id": SPACE,
                    "requested_by": "Carlo",
                },
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.checkpoint(
                job["id"],
                "discussion_note",
                {
                    "status": "already_posted",
                    "discussion_id": "d-1",
                    "discussion_title": "follw up 1",
                    "reason": question,
                },
            )
            bind_job_chat_thread(store, job["id"], THREAD)
            reply = guard_chat_response(db, job["id"], "Execution Summary\nnote is verified")
            self.assertIn(question.strip(), reply)
            self.assertTrue(settle_job_when_reply_sent(db, job["id"], reply))
            parked = store.get_job(job["id"])
            self.assertEqual(parked["status"], JobStatus.NEEDS_CLARIFICATION.value)
            self.assertFalse(parked.get("lease_owner"))
            chat = _chat(db)
            with patch.object(adapter, "ROBIE_JOB_DB", db), patch.object(
                adapter, "guard_chat_response", return_value=reply
            ):
                asyncio.run(
                    chat.send(
                        SPACE,
                        reply,
                        metadata={"robie_job_id": job["id"], "thread_id": FRESH},
                    )
                )
            sent = _outbound_text(chat)[0]
            self.assertIn("I already added that note at ", sent)
            self.assertIn("Want me to add it again?", sent)
            self.assertEqual(
                chat._chat_api.messages.calls[0]["body"]["thread"]["name"], THREAD
            )
            self.assertTrue(note_repost_confirmed_by_reply(store, job["id"], "yes"))
            self.assertTrue(consume_note_repost_allowance(store, job["id"]))
            self.assertFalse(consume_note_repost_allowance(store, job["id"]))
            self.assertEqual(close_declined_note_repost(store, job["id"], "no"), LEFT_AS_IS)
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.CANCELLED.value)
            self.assertEqual(
                store.get_checkpoint(job["id"], "note_left_as_is")["reply"], LEFT_AS_IS
            )

    def test_top_level_stop_cancels_waiting_jobs_and_idle_stop_is_visible(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)

            def waiting(text: str, thread: str) -> str:
                job = store.create_job(
                    "hermes.google_chat_task",
                    {
                        "text": text,
                        "conversation_id": SPACE,
                        "requested_by": "Carlo",
                    },
                )
                store.transition(
                    job["id"],
                    JobStatus.NEEDS_CLARIFICATION,
                    expected={JobStatus.PENDING},
                    release_lease=True,
                )
                bind_job_chat_thread(store, job["id"], thread)
                return job["id"]

            first = waiting("which client?", THREAD)
            second = waiting("which policy?", OTHER)
            stranger = store.create_job(
                "hermes.google_chat_task",
                {
                    "text": "someone else",
                    "conversation_id": SPACE,
                    "requested_by": "Other Person",
                },
            )
            store.transition(
                stranger["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.PENDING},
                release_lease=True,
            )
            chat = _chat(db)

            async def _noop(*args, **kwargs):
                del args, kwargs

            chat._terminate_running_agent = _noop
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(chat._apply_chat_stop(_Event(FRESH, "/stop")))
            self.assertEqual(store.get_job(first)["status"], JobStatus.CANCELLED.value)
            self.assertEqual(store.get_job(second)["status"], JobStatus.CANCELLED.value)
            self.assertEqual(
                store.get_job(stranger["id"])["status"],
                JobStatus.NEEDS_CLARIFICATION.value,
            )
            text = _outbound_text(chat)[0]
            self.assertIn("Stopped.", text)
            self.assertNotEqual(text, NOTHING_RUNNING_REPLY)
            _assert_clean(self, text, first, second)
            idle = _chat(db)
            idle._terminate_running_agent = _noop
            with patch.object(adapter, "ROBIE_JOB_DB", db):
                asyncio.run(idle._apply_chat_stop(_Event(None, "/stop")))
            self.assertEqual(_outbound_text(idle), [NOTHING_RUNNING_REPLY])

    def test_mixed_deductible_ask_is_one_plain_refusal(self):
        reply = hard_block_reply(
            "add holder X and change auto deductible to $1000"
        )
        self.assertEqual(
            reply,
            "I can't change the deductible. I can't add the certificate "
            "holder yet either, so staff still has to do that.",
        )
        self.assertNotIn("\n", reply or "")
        cleaned = format_user_reply(
            "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: " + (reply or "")
        )
        self.assertNotIn("ROBIE_BLOCKED", cleaned)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", cleaned)
        self.assertIn("deductible", cleaned)

    def test_question_only_does_not_record_or_hold_the_chat(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with patch.object(
                RecordingManager, "safe_start", side_effect=AssertionError("recording")
            ):
                job_id = open_chat_job(
                    db,
                    "spaces/ROBY/messages/q",
                    "which carriers do we quote for auto?",
                    conversation_id=SPACE,
                )
            store = JobStore(db)
            reason = store.get_checkpoint(job_id, "recording_exemption")["reason"]
            self.assertIn("question only", reason)
            chat = _chat(db)
            chat.gateway_runner._running_agents[
                "agent:main:google_chat:dm:" + SPACE
            ] = object()
            chat._gateway_turns[(SPACE, THREAD)] = {
                "job_id": job_id,
                "task": _Live(),
                "watchdog": _Live(),
            }
            self.assertTrue(session_is_busy(chat, _Event(FRESH)))
            self.assertFalse(
                busy_session_should_defer(chat, _Event(FRESH), db_path=db)
            )
            from robie_job_engine.chat_guard import stop_generic_chat_job_heartbeat

            stop_generic_chat_job_heartbeat(db, job_id)


class EzlynxBlankLoginTests(unittest.TestCase):
    def test_state_opens_the_app_page_before_calling_it_logged_out(self):
        session = PlaywrightEzlynxSession.__new__(PlaywrightEzlynxSession)
        page = _Page("https://app.ezlynx.com/auth/account/login")
        page.signed_in_after_app = True
        session._page = page
        self.assertEqual(session.state(), SessionState.SIGNED_IN)
        self.assertEqual(page.gotos, [APP_WEB_URL])

        logged_out = PlaywrightEzlynxSession.__new__(PlaywrightEzlynxSession)
        blank = _Page("about:blank")
        blank.stay_on_login = True
        logged_out._page = blank
        self.assertEqual(logged_out.state(), SessionState.LOGIN_REQUIRED)
        self.assertIn(APP_WEB_URL, blank.gotos)

    def test_login_reloads_a_blank_login_page(self):
        session = PlaywrightEzlynxSession.__new__(PlaywrightEzlynxSession)
        page = _Page("https://app.ezlynx.com/auth/account/login")
        page.form_after_reload = True
        page.stay_on_login = True
        session._page = page
        with patch(
            "robie_job_engine.ezlynx_session.wait_for_post_login_state",
            return_value=SessionState.SIGNED_IN,
        ):
            state = session.login("agent", "secret")
        self.assertEqual(state, SessionState.SIGNED_IN)
        self.assertGreaterEqual(page.reloads, 1)
        self.assertIn(LOGIN_URL, page.gotos)
        self.assertTrue(any(item[0] == "#txtUserName" for item in page.filled))

    def test_bootstrap_checks_the_app_page_and_reloads_a_blank_form(self):
        from ezlynx_login_bootstrap import (
            SUBMISSION_URL,
            ensure_login_form,
            session_is_logged_in_on_app_page,
        )

        page = _Page("https://app.ezlynx.com/auth/account/login")
        page.signed_in_after_app = True
        self.assertTrue(session_is_logged_in_on_app_page(page))
        self.assertEqual(page.gotos[0], SUBMISSION_URL)

        blank = _Page("https://app.ezlynx.com/auth/account/login")
        blank.form_after_reload = True
        ensure_login_form(blank)
        self.assertEqual(blank.reloads, 1)
        self.assertTrue(blank.form_visible)


if __name__ == "__main__":
    unittest.main()
