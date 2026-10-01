"""Progress traces do not COMPLETE, and a stuck tab is not a sign-out.

Job 598820fc (Test, 7:00 PM ET, "whats the GL policy number and carrier
for buster brown") stored seven playwright_exec lines, rendered them as
"Answered.", and went COMPLETE while the agent was still calling tools.
The close lined up with PLAYWRIGHT_BLOCKED: do not guess an EZLynx search
URL. Jobs e369a3c9 and 598820fc also treated a blank account shell plus a
root redirect to login as a sign-out. A new page in the same context was
still authenticated.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import _render_chat_terminal, guard_chat_response
from robie_job_engine.chat_turn_control import (
    agent_stop_requested,
    clear_agent_stop,
    is_refused_tool_text,
    is_tool_progress_text,
)
from robie_job_engine.client_name_lookup import (
    assess_session_after_stuck_tab,
    install_stuck_tab_recovery,
    open_bound_account_in_fresh_page,
    set_session_probe,
    sign_out_posture,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.playwright_observability import record_playwright_exec
from robie_job_engine.recording import RecordingManager
from robie_job_engine.store import JobStore
from robie_job_engine.turn_finalization import COULD_NOT_FINISH, visible_fallback_line
from robie_job_engine.user_reply import SIGN_IN_QUESTION

ASK = "whats the GL policy number and carrier for buster brown"
FOUND = "26356199"
ACCOUNT = f"https://app.ezlynx.com/web/account/{FOUND}/policies"
ACTIVITY = f"https://app.ezlynx.com/web/account/{FOUND}/activity"
LOGIN = "https://app.ezlynx.com/auth/account/login"
ANSWER = "Buster Brown's GL policy number is GL-100 with Harbor."
SEARCH_REFUSAL = (
    "PLAYWRIGHT_BLOCKED: do not guess an EZLynx search URL. "
    "Type the name into input#applicantSearch on the open page."
)
SIGN_OUT_TEXT = (
    "PLAYWRIGHT_BLOCKED: the session is signed out. "
    "Please sign in at https://app.ezlynx.com/auth/account/login"
)


def _progress(count: int = 7) -> str:
    return "\n".join(
        f'🎭 playwright_exec: "step {index}"' for index in range(count)
    )


def _job(store: JobStore) -> str:
    created = store.create_job(
        "hermes.google_chat_task",
        {"text": ASK, "conversation_id": "spaces/fix", "requested_by": "Carlo"},
    )
    store.transition(created["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    store.checkpoint(
        created["id"],
        "client_name_search",
        {
            "resolved": True,
            "source": "search",
            "applicant_ids": [FOUND],
            "user_line": "",
            "name": "buster brown",
        },
    )
    payload = dict(store.get_job(created["id"]).get("payload") or {})
    payload["applicant_id"] = FOUND
    store.update_payload(created["id"], payload)
    return created["id"]


class _Body:
    def __init__(self, page: "_FakePage") -> None:
        self.page = page

    def inner_text(self, timeout: int = 0) -> str:
        del timeout
        return self.page.body_text


class _FakePage:
    def __init__(self, url: str, *, html: str = "", body: str = "") -> None:
        self.url = url
        self.html = html
        self.body_text = body
        self.gotos: list[str] = []
        self.closed = False
        self.context: "_FakeContext | None" = None

    def content(self) -> str:
        return self.html

    def locator(self, selector: str) -> _Body:
        del selector
        return _Body(self)

    def goto(self, url: str, *args, **kwargs) -> str:
        del args, kwargs
        self.gotos.append(str(url))
        self.url = str(url)
        return str(url)

    def close(self) -> None:
        self.closed = True
        if self.context is not None:
            self.context.pages = [item for item in self.context.pages if item is not self]


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.authenticated = True

    def new_page(self) -> _FakePage:
        page = _FakePage("about:blank")
        page.context = self
        original = page.goto

        def goto(url: str, *args, **kwargs) -> str:
            original(url, *args, **kwargs)
            if self.authenticated and "/auth/account/login" not in url:
                page.url = ACTIVITY
                page.html = "<app-root><div>General Liability</div></app-root>"
                page.body_text = "General Liability GL-100 Harbor"
            else:
                page.url = LOGIN
                page.html = "<app-root></app-root>"
                page.body_text = ""
            return page.url

        page.goto = goto
        self.pages.append(page)
        return page


class ProgressDoesNotCompleteTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_session_probe(None)

    def test_progress_only_content_gives_no_complete_and_one_honest_line(self):
        progress = _progress(7)
        self.assertTrue(is_tool_progress_text("\n".join(progress.splitlines()[:7])))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            reply = guard_chat_response(db, job_id, progress)
            job = store.get_job(job_id)
            self.assertNotEqual(job["status"], JobStatus.COMPLETE.value)
            self.assertEqual(job["status"], JobStatus.RUNNING.value)
            self.assertEqual(reply, "")
            self.assertIsNone(store.get_checkpoint(job_id, "worker_response"))
            rendered = _render_chat_terminal(
                store, job, progress, RecordingManager(db)
            )
            self.assertEqual(rendered.strip(), COULD_NOT_FINISH)
            self.assertEqual(len(rendered.strip().splitlines()), 1)
            self.assertNotIn("Answered.", rendered)
            self.assertEqual(visible_fallback_line(db, job_id), COULD_NOT_FINISH)
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)

    def test_refused_search_url_then_more_tools_does_not_finalize(self):
        self.assertTrue(is_refused_tool_text(SEARCH_REFUSAL))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            self.assertEqual(guard_chat_response(db, job_id, SEARCH_REFUSAL), "")
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
            more = "\n".join(
                f'🎭 playwright_exec: "after refusal {index}"' for index in range(3)
            )
            self.assertEqual(guard_chat_response(db, job_id, more), "")
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
            self.assertFalse(agent_stop_requested(job_id))
            heartbeat = "\n".join(["Working…"] * 4)
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.RUNNING.value
            )
            guard_chat_response(db, job_id, heartbeat)
            self.assertNotEqual(
                store.get_job(job_id)["status"], JobStatus.COMPLETE.value
            )

    def test_terminal_job_interrupts_the_agent_and_releases_the_lease(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            record_playwright_exec(
                f"page.goto('{ACCOUNT}')",
                {"url": ACCOUNT},
                job_id=job_id,
                db_path=db,
                status="ok",
            )
            self.assertIsNotNone(store.claim(job_id, "turn-lease"))
            self.assertTrue(store.get_job(job_id)["lease_owner"])
            try:
                reply = guard_chat_response(db, job_id, ANSWER)
            finally:
                clear_agent_stop(job_id)
            self.assertIn("GL-100", reply)
            self.assertIn("Harbor", reply)
            finished = store.get_job(job_id)
            self.assertEqual(finished["status"], JobStatus.COMPLETE.value)
            self.assertFalse(finished.get("lease_owner"))
            # The stop was requested at the terminal transition, before cleanup.
            # clear_agent_stop above is the next turn. Re-read the flag by
            # completing a second time is unnecessary: claim the behavior
            # from a fresh terminal transition.
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            record_playwright_exec(
                f"page.goto('{ACCOUNT}')",
                {"url": ACCOUNT},
                job_id=job_id,
                db_path=db,
                status="ok",
            )
            store.claim(job_id, "turn-lease")
            guard_chat_response(db, job_id, ANSWER)
            try:
                self.assertTrue(agent_stop_requested(job_id))
                self.assertFalse(store.get_job(job_id).get("lease_owner"))
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)
            finally:
                clear_agent_stop(job_id)

    def test_real_answer_still_completes_with_that_answer(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            record_playwright_exec(
                f"page.goto('{ACCOUNT}')",
                {"url": ACCOUNT},
                job_id=job_id,
                db_path=db,
                status="ok",
            )
            try:
                reply = guard_chat_response(db, job_id, ANSWER)
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)
                self.assertIn("GL-100", reply)
                self.assertIn("Harbor", reply)
                self.assertNotEqual(reply.strip(), COULD_NOT_FINISH)
            finally:
                clear_agent_stop(job_id)


class StuckTabSignOutTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_session_probe(None)

    def test_blank_shell_and_login_redirect_continues_when_fresh_page_is_in(self):
        stuck = _FakePage(
            ACCOUNT,
            html="<app-root></app-root>",
            body="",
        )
        context = _FakeContext()
        stuck.context = context
        context.pages.append(stuck)
        context.authenticated = True

        def root_goto(url: str, *args, **kwargs) -> str:
            del args, kwargs
            stuck.gotos.append(url)
            if url.rstrip("/") == "https://app.ezlynx.com":
                stuck.url = LOGIN
                stuck.html = "<app-root></app-root>"
                stuck.body_text = ""
            return stuck.url

        stuck.goto = root_goto
        stuck.goto("https://app.ezlynx.com")
        self.assertIn("/auth/account/login", stuck.url)
        result = assess_session_after_stuck_tab(stuck, context, account_url=ACTIVITY)
        self.assertFalse(result["signed_out"])
        self.assertIn("/web/account/", result["probe_url"])
        open_pages = [page for page in context.pages if not page.closed]
        self.assertEqual(len(open_pages), 1)
        self.assertFalse(open_pages[0].closed)
        set_session_probe(lambda: ACTIVITY)
        self.assertEqual(sign_out_posture(SIGN_OUT_TEXT), "suppress")
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            record_playwright_exec(
                f"page.goto('{LOGIN}')",
                {"url": LOGIN},
                job_id=job_id,
                db_path=db,
                status="ok",
            )
            reply = guard_chat_response(db, job_id, SIGN_OUT_TEXT)
            self.assertEqual(reply, "")
            self.assertNotIn("sign in", reply.casefold())
            self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)

    def test_real_sign_out_posts_the_plain_line_without_codes(self):
        context = _FakeContext()
        context.authenticated = False
        stuck = _FakePage(LOGIN, html="<app-root></app-root>", body="")
        stuck.context = context
        context.pages.append(stuck)
        result = assess_session_after_stuck_tab(stuck, context, account_url=ACTIVITY)
        self.assertTrue(result["signed_out"])
        self.assertIn("/auth/account/login", result["probe_url"])
        set_session_probe(lambda: LOGIN)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            reply = guard_chat_response(db, job_id, SIGN_OUT_TEXT)
            self.assertEqual(reply.strip(), SIGN_IN_QUESTION)
            self.assertNotIn("PLAYWRIGHT_BLOCKED", reply)
            self.assertNotIn("ROBIE_BLOCKED", reply)
            self.assertEqual(
                store.get_job(job_id)["status"],
                JobStatus.NEEDS_CLARIFICATION.value,
            )

    def test_bound_account_opens_on_a_fresh_page_and_keeps_one_tab(self):
        context = _FakeContext()
        stuck = _FakePage(
            "https://app.ezlynx.com/applicantportal/Search/Index?searchPhrase=buster+brown",
            html="<app-root></app-root>",
            body="",
        )
        stuck.context = context
        context.pages.append(stuck)
        opened = open_bound_account_in_fresh_page(context, FOUND)
        self.assertTrue(opened["opened"])
        self.assertFalse(opened["signed_out"])
        self.assertIn(FOUND, opened["url"])
        open_pages = [page for page in context.pages if not page.closed]
        self.assertEqual(len(open_pages), 1)

    def test_goto_on_a_blank_shell_switches_to_the_fresh_page(self):
        context = _FakeContext()
        stuck = _FakePage(ACCOUNT, html="<app-root></app-root>", body="")
        stuck.context = context
        context.pages.append(stuck)
        scope = {"page": stuck, "context": context}
        install_stuck_tab_recovery(scope)
        scope["page"].goto(ACCOUNT)
        self.assertIsNot(scope["page"], stuck)
        self.assertIn(FOUND, scope["page"].url)
        self.assertTrue(stuck.closed)
        open_pages = [page for page in context.pages if not page.closed]
        self.assertEqual(len(open_pages), 1)
