"""Regressions from the 2026-10-02 rerun on head 0b7764d.

A john smith search was parked as an account URL guess. A discussion
card click checked a stop on the previous job. Login bootstrap reported
AUTHENTICATED from the tab URL and did not handle the two-session prompt.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

import ezlynx_login_bootstrap as bootstrap
from robie_job_engine.chat_turn_control import (
    STOPPED_OUTPUT,
    clear_agent_stop,
    refuse_current_tool_call,
    request_agent_stop,
)
from robie_job_engine.ezlynx_account_nav import (
    install_account_nav_guard,
    is_account_url_guess,
    is_applicant_search_url,
    refuse_guessed_account_url,
)
from robie_job_engine.ezlynx_driver_gate import EzlynxDriverGateRefused
from robie_job_engine.live_turn_guard import (
    _BOUND_RESUME,
    bind_card_click_resume,
    bind_turn_owner,
    bound_resume_job_id,
    set_turn_job,
    stamp_agent_once,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from test_round10_reply_lifecycle import SPACE, _adapter_module, _chat

SEARCH_WEB = "https://app.ezlynx.com/web/applicant/search?searchPhrase=john+smith"
SEARCH_PORTAL = (
    "https://app.ezlynx.com/applicantportal/Search/Index?searchPhrase=john+smith"
)
SUMMARY = "https://app.ezlynx.com/web/account/220250093/summary"
UNKNOWN_SUMMARY = "https://app.ezlynx.com/web/applicant/summary"
UNKNOWN_DETAILS = "https://app.ezlynx.com/applicantportal/Applicant/Details"
UNKNOWN_INDEX = "https://app.ezlynx.com/web/applicant/index"
SESSION_TEXT = (
    "Your account is limited to 2 active sessions. "
    "Continue will log out the session that is currently active. "
    "Chrome on Windows, last active 2:40 PM."
)


def _page_class():
    class FakePage:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def goto(self, url, **_kwargs):
            self.urls.append(str(url))
            return type("Response", (), {"status": 200})()

    return FakePage


class _LimitPage:
    def __init__(self, body: str, *, links: int = 2) -> None:
        self.url = "https://app.ezlynx.com/web/home"
        self.body = body
        self.web_links = links
        self.login_controls = 0
        self.clicked: list[str] = []
        self.reloads = 0

    def locator(self, selector: str):
        return _LimitLocator(self, selector)

    def reload(self, wait_until: str | None = None) -> None:
        del wait_until
        self.reloads += 1

    def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    def wait_for_timeout(self, _ms: int) -> None:
        return None


class _LimitLocator:
    def __init__(self, page: _LimitPage, selector: str) -> None:
        self.page = page
        self.selector = selector
        self.first = self

    def inner_text(self, timeout: int | None = None) -> str:
        del timeout
        return self.page.body

    def count(self) -> int:
        if "txtUserName" in self.selector:
            return self.page.login_controls
        if "Continue" in self.selector:
            return 0 if "limited to 2 active sessions" not in self.page.body.casefold() else 1
        if "/web/" in self.selector or "quickSearchInput" in self.selector:
            return self.page.web_links
        return 0

    def click(self) -> None:
        self.page.clicked.append(self.selector)
        self.page.body = "Policies"


class SearchRouteTests(unittest.TestCase):
    def test_name_search_routes_are_not_account_url_guesses(self):
        for url in (SEARCH_WEB, SEARCH_PORTAL):
            self.assertTrue(is_applicant_search_url(url))
            self.assertFalse(is_account_url_guess(url))
            refuse_guessed_account_url(url)
        for unknown in (UNKNOWN_SUMMARY, UNKNOWN_DETAILS, UNKNOWN_INDEX):
            self.assertTrue(is_account_url_guess(unknown))
            with self.assertRaisesRegex(RuntimeError, "no account id is known"):
                refuse_guessed_account_url(unknown)
        self.assertTrue(is_account_url_guess(SUMMARY))
        with self.assertRaisesRegex(RuntimeError, "Do not enumerate"):
            refuse_guessed_account_url(SUMMARY)
        page_cls = _page_class()
        install_account_nav_guard({"Page": page_cls, "_robie_task_text": "look up john smith"})
        page = page_cls()
        page.goto(SEARCH_WEB)
        page.goto(SEARCH_PORTAL)
        self.assertEqual(page.urls, [SEARCH_WEB, SEARCH_PORTAL])
        with self.assertRaisesRegex(RuntimeError, "no account id is known"):
            page.goto(UNKNOWN_SUMMARY)
        with self.assertRaisesRegex(RuntimeError, "Do not enumerate"):
            page.goto(SUMMARY)
        self.assertEqual(page.urls, [SEARCH_WEB, SEARCH_PORTAL])


class CardClickStopTests(unittest.TestCase):
    def setUp(self) -> None:
        _BOUND_RESUME.clear()
        self._token = set_turn_job("")

    def tearDown(self) -> None:
        _BOUND_RESUME.clear()
        set_turn_job("")

    def test_card_click_checks_the_resumed_job_not_the_env_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            stopped = store.create_job(
                "ezlynx.discussion_note",
                {"text": "the earlier note", "request_text": "the earlier note"},
            )
            store.transition(stopped["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                stopped["id"],
                JobStatus.CANCELLED,
                expected={JobStatus.RUNNING},
                error="Cancelled.",
                release_lease=True,
            )
            request_agent_stop(stopped["id"])
            live = store.create_job(
                "ezlynx.discussion_note",
                {
                    "text": "Add a note for Buster Brown",
                    "request_text": "Add a note for Buster Brown",
                },
            )
            store.transition(live["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            agent = type("Agent", (), {})()
            bind_turn_owner(agent, stopped["id"])
            session_key = "agent:main:google_chat:dm:spaces/ROBY:job"
            adapter = type("Adapter", (), {})()
            adapter.gateway_runner = type("Runner", (), {"_running_agents": {session_key: agent}})()
            try:
                with patch.dict(os.environ, {"ROBIE_JOB_ID": stopped["id"], "JOB_ID": stopped["id"]}):
                    # No resume yet. The env job was stopped. That id is not this turn.
                    set_turn_job("")
                    self.assertIsNone(refuse_current_tool_call({}))
                    bind_card_click_resume(
                        adapter,
                        store,
                        live["id"],
                        session_key,
                        "Which discussion?",
                    )
                    # The tool thread does not inherit the click handler's context.
                    set_turn_job("")
                    self.assertIsNone(refuse_current_tool_call({}))
                    self.assertEqual(stamp_agent_once(agent), live["id"])
                    pending = store.get_checkpoint(live["id"], "clarify_pending") or {}
                    self.assertEqual(pending.get("session_key"), session_key)
                    self.assertNotEqual(pending.get("session_key"), "")
                    request_agent_stop(live["id"])
                    set_turn_job("")
                    self.assertEqual(refuse_current_tool_call({}), STOPPED_OUTPUT)
            finally:
                clear_agent_stop(stopped["id"])
                clear_agent_stop(live["id"])

    def test_unstamped_agent_does_not_capture_a_stopped_env_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            stopped = store.create_job(
                "ezlynx.discussion_note",
                {"text": "earlier", "request_text": "earlier"},
            )
            store.transition(stopped["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                stopped["id"],
                JobStatus.CANCELLED,
                expected={JobStatus.RUNNING},
                error="Cancelled.",
                release_lease=True,
            )
            request_agent_stop(stopped["id"])
            live = store.create_job(
                "ezlynx.discussion_note",
                {"text": "the note", "request_text": "the note"},
            )
            store.transition(live["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            session_key = "agent:main:google_chat:dm:spaces/ROBY:fresh"
            agent = type("Agent", (), {})()
            adapter = type("Adapter", (), {})()
            adapter.gateway_runner = type(
                "Runner", (), {"_running_agents": {session_key: agent}}
            )()
            try:
                with patch.dict(
                    os.environ,
                    {
                        "ROBIE_JOB_ID": stopped["id"],
                        "JOB_ID": stopped["id"],
                        "ROBIE_JOB_DB": db,
                    },
                ):
                    set_turn_job("")
                    bind_card_click_resume(adapter, store, live["id"], session_key)
                    # The tool wrapper stamps, then binds the context to that id.
                    stamped = stamp_agent_once(agent)
                    set_turn_job(stamped)
                    self.assertEqual(stamped, live["id"])
                    self.assertIsNone(refuse_current_tool_call({}))
                    self.assertEqual(bound_resume_job_id(), live["id"])
            finally:
                clear_agent_stop(stopped["id"])
                set_turn_job("")

    def test_blank_session_key_is_filled_from_the_live_session(self):
        adapter = _adapter_module()
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            live = store.create_job(
                "ezlynx.discussion_note",
                {"text": "Add a note", "request_text": "Add a note"},
            )
            chat = _chat(db)
            clarify_id = "clarify-discussion"
            key = "agent:main:google_chat:dm:spaces/ROBY:live"
            agent = type("Agent", (), {})()
            chat._clarify_state = {clarify_id: ""}
            chat._clarify_resume = {
                clarify_id: {"job_id": live["id"], "session_key": "", "question": "Which discussion?"}
            }
            chat._active_chat_job = {SPACE: live["id"]}
            chat.gateway_runner._running_agents[key] = agent
            chat._active_sessions = {key: object()}
            try:
                with patch.object(adapter, "ROBIE_JOB_DB", db):
                    chat._resume_clarify_click(clarify_id)
            finally:
                _BOUND_RESUME.clear()
                set_turn_job("")
            pending = store.get_checkpoint(live["id"], "clarify_pending") or {}
            self.assertEqual(pending.get("session_key"), key)
            self.assertEqual(getattr(agent, "_robie_turn_job_id", ""), live["id"])


class _CredentialPage:
    """Two-session prompt with empty username and password fields."""

    def __init__(self, body: str, *, after_continue: str, become_app: bool) -> None:
        self.body = body
        self.url = "https://app.ezlynx.com/auth/account/login"
        self.events: list[tuple[str, str]] = []
        self.values = {"#txtUserName": "", "#txtPassword": ""}
        self.web_links = 0
        self.login_controls = 1
        self.fields_present = True
        self.reloads = 0
        self.after_continue = after_continue
        self.become_app = become_app
        self.continue_clicks = 0

    def locator(self, selector: str):
        return _CredentialLocator(self, selector)

    def goto(self, url: str, wait_until: str | None = None) -> None:
        del wait_until
        self.url = url
        self.events.append(("goto", url))

    def reload(self, wait_until: str | None = None) -> None:
        del wait_until
        self.reloads += 1

    def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    def wait_for_timeout(self, _ms: int) -> None:
        return None


class _CredentialLocator:
    def __init__(self, page: _CredentialPage, selector: str) -> None:
        self.page = page
        self.selector = selector
        self.first = self

    def inner_text(self, timeout: int | None = None) -> str:
        del timeout
        return self.page.body

    def count(self) -> int:
        if self.selector in {"#txtUserName", "#txtPassword"}:
            return 1 if self.page.fields_present else 0
        if "txtUserName" in self.selector:
            return self.page.login_controls
        if "Continue" in self.selector:
            folded = self.page.body.casefold()
            return 1 if "limited to 2 active sessions" in folded else 0
        if "/web/" in self.selector or "quickSearchInput" in self.selector:
            return self.page.web_links
        return 0

    def fill(self, value: str) -> None:
        self.page.events.append(("fill", self.selector))
        self.page.values[self.selector] = value

    def click(self) -> None:
        self.page.events.append(("click", self.selector))
        if "Continue" not in self.selector:
            return
        self.page.continue_clicks += 1
        self.page.body = self.page.after_continue
        if self.page.become_app:
            self.page.url = "https://app.ezlynx.com/web/home"
            self.page.web_links = 4
            self.page.login_controls = 0
            self.page.fields_present = False

    def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        del state, timeout
        return None


class TwoSessionLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        bootstrap.begin_login_run()

    def test_url_alone_is_not_authenticated(self):
        page = _LimitPage("Policies", links=0)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = bootstrap.resolve_login_barrier(page, gate=lambda: None)
        self.assertIsNone(result)
        self.assertNotIn("AUTHENTICATED", stdout.getvalue())
        self.assertGreaterEqual(page.reloads, 1)
        self.assertFalse(bootstrap.authenticated(page))

    def test_two_session_prompt_continues_only_when_this_env_holds_the_lease(self):
        refused = _LimitPage(SESSION_TEXT)
        stdout = io.StringIO()

        def deny() -> None:
            raise EzlynxDriverGateRefused("driver belongs to PRODUCTION")

        with redirect_stdout(stdout):
            code = bootstrap.handle_two_session_prompt(refused, gate=deny)
        self.assertEqual(code, bootstrap.SESSION_LIMIT_REFUSED)
        self.assertNotEqual(code, 24)
        self.assertEqual(refused.clicked, [])
        self.assertIn("did not press Continue", stdout.getvalue())
        self.assertIn("Chrome on Windows", stdout.getvalue())

        allowed = _LimitPage(SESSION_TEXT)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = bootstrap.resolve_login_barrier(allowed, gate=lambda: None)
        self.assertEqual(code, 0)
        self.assertNotEqual(code, 24)
        self.assertTrue(allowed.clicked)
        self.assertIn("ended the other active session", stdout.getvalue())
        self.assertIn("Chrome on Windows, last active 2:40 PM", stdout.getvalue())
        self.assertIn("AUTHENTICATED", stdout.getvalue())
        self.assertGreaterEqual(allowed.reloads, 1)

    def test_credentials_are_refilled_before_continue(self):
        page = _CredentialPage(SESSION_TEXT, after_continue="Policies", become_app=True)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = bootstrap.handle_two_session_prompt(
                page,
                gate=lambda: None,
                username="robie-user",
                password="secret-pass",
            )
        self.assertEqual(code, 0)
        fills = [event for event in page.events if event[0] == "fill"]
        clicks = [event for event in page.events if event[0] == "click"]
        self.assertEqual(
            fills, [("fill", "#txtUserName"), ("fill", "#txtPassword")]
        )
        self.assertEqual(page.values["#txtUserName"], "robie-user")
        self.assertEqual(page.values["#txtPassword"], "secret-pass")
        self.assertEqual(page.continue_clicks, 1)
        self.assertLess(page.events.index(fills[0]), page.events.index(clicks[0]))
        self.assertLess(page.events.index(fills[1]), page.events.index(clicks[0]))
        text = stdout.getvalue()
        self.assertIn("ended the other active session", text)
        self.assertNotIn("secret-pass", text)

    def test_failed_continue_does_not_log_that_a_session_ended(self):
        page = _CredentialPage(
            SESSION_TEXT, after_continue="Username is required", become_app=False
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = bootstrap.handle_two_session_prompt(
                page,
                gate=lambda: None,
                username="robie-user",
                password="secret-pass",
            )
        self.assertEqual(code, bootstrap.SESSION_LIMIT_LOGIN_FAILED)
        self.assertNotEqual(code, 24)
        self.assertNotEqual(code, bootstrap.SESSION_LIMIT_NOT_CLEARED)
        text = stdout.getvalue()
        self.assertNotIn("ended the other", text)
        self.assertIn("Username is required", text)
        self.assertIn("still active", text)
        self.assertNotIn("secret-pass", text)
        self.assertEqual(page.continue_clicks, 1)
        self.assertEqual(page.values["#txtUserName"], "robie-user")

    def test_continue_is_pressed_only_once_per_run(self):
        page = _CredentialPage(
            SESSION_TEXT, after_continue=SESSION_TEXT, become_app=False
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            first = bootstrap.handle_two_session_prompt(
                page, gate=lambda: None, username="robie-user", password="secret-pass"
            )
            second = bootstrap.handle_two_session_prompt(
                page, gate=lambda: None, username="robie-user", password="secret-pass"
            )
        self.assertEqual(page.continue_clicks, 1)
        self.assertEqual(first, bootstrap.SESSION_LIMIT_LOGIN_FAILED)
        self.assertEqual(second, bootstrap.SESSION_LIMIT_LOGIN_FAILED)
        self.assertNotIn("ended the other", stdout.getvalue())
        self.assertIn("was not pressed again", stdout.getvalue())

    def test_self_login_uses_the_same_continue_path(self):
        from robie_job_engine.ezlynx_session import (
            PlaywrightEzlynxSession,
            SessionState,
        )
        from robie_job_engine.secret_manager import EzlynxCredentials
        from robie_job_engine.session_recovery import attempt_session_recovery

        page = _CredentialPage(SESSION_TEXT, after_continue="Policies", become_app=True)
        session = PlaywrightEzlynxSession.__new__(PlaywrightEzlynxSession)
        session._page = page
        self.assertEqual(session.state(), SessionState.LOGIN_REQUIRED)

        class Browser:
            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False

            def state(self):
                return SessionState.LOGIN_REQUIRED

            def login(self, username: str, password: str):
                return session.login(username, password)

            def close(self) -> None:
                self.closed = True

        with durable_temporary_directory() as tmp:
            lock = str(Path(tmp) / "ezlynx-session.lock")
            stdout = io.StringIO()
            with patch.dict(os.environ, {"ROBIE_EZLYNX_SESSION_LOCK": lock}), patch(
                "robie_job_engine.ezlynx_driver_gate.require_driver_in",
                lambda **_kwargs: None,
            ), patch(
                "robie_job_engine.ezlynx_session.load_ezlynx_credentials",
                return_value=EzlynxCredentials("robie-user", "secret-pass"),
            ), redirect_stdout(stdout):
                result = attempt_session_recovery(browser_factory=Browser)
        self.assertTrue(result["recovered"], result)
        self.assertEqual(result["state"], SessionState.SIGNED_IN.value)
        fills = [event for event in page.events if event[0] == "fill"]
        self.assertEqual(fills, [("fill", "#txtUserName"), ("fill", "#txtPassword")])
        self.assertEqual(page.continue_clicks, 1)
        self.assertIn("ended the other active session", stdout.getvalue())
        self.assertNotIn("secret-pass", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
