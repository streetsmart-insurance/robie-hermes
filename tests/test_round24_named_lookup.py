"""A named lookup is COMPLETE only after a real EZLynx read.

The applicant id comes from this job's applicant-search results or from
the user's message. An id copied from docs, such as 220250093, is not used.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import build_chat_execution_text
from robie_job_engine.client_name_lookup import (
    LOOKUP_MISS,
    QUICK_SEARCH_SELECTOR,
    APPLICANT_SEARCH_SELECTOR,
    choose_client_search_page,
    is_readonly_client_search,
    prepare_named_client_lookup,
    read_applicant_search,
    refuse_named_lookup_navigation,
)
from robie_job_engine.ezlynx_account_nav import install_account_nav_guard
from robie_job_engine.models import JobStatus
from robie_job_engine.playwright_observability import record_playwright_exec
from robie_job_engine.store import JobStore
from robie_job_engine.user_reply import SIGN_IN_QUESTION
from test_round10_reply_lifecycle import SPACE, _adapter_module, _chat, _outbound_text

ASK = "whats the GL policy number and carrier for buster brown"
DOCS_ID = "220250093"
FOUND_ID = "26356199"
FALSE_MISS = "The live EZLynx lookup failed."
ANSWER = "Buster Brown's GL policy number is GL-100 with Harbor."
ACCOUNT = f"https://app.ezlynx.com/web/account/{FOUND_ID}/policies"
DOCS_ACCOUNT = f"https://app.ezlynx.com/web/account/{DOCS_ID}/policies"
SIGN_IN_URL = "https://app.ezlynx.com/auth/account/login"


def _job(store: JobStore, *, applicant_id: str = "") -> str:
    payload = {"text": ASK, "conversation_id": SPACE, "requested_by": "Carlo"}
    if applicant_id:
        payload["applicant_id"] = applicant_id
    job = store.create_job("hermes.google_chat_task", payload)
    store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
    return job["id"]


def _searcher(matches, *, status: str = "ok"):
    def _run(name: str) -> dict:
        return {"status": status, "matches": matches, "name": name}

    return _run


class _Box:
    def __init__(self) -> None:
        self.fills: list[str] = []
        self.pressed: list[str] = []

    def count(self) -> int:
        return 1

    def fill(self, value: str) -> None:
        self.fills.append(value)

    def press(self, key: str) -> None:
        self.pressed.append(key)


class _Link:
    def __init__(self, href: str, text: str) -> None:
        self.href = href
        self.text = text

    def get_attribute(self, name: str) -> str:
        return self.href if name == "href" else ""

    def inner_text(self) -> str:
        return self.text


class _Links:
    def __init__(self, rows: list[_Link]) -> None:
        self.rows = rows

    def count(self) -> int:
        return len(self.rows)

    def nth(self, index: int) -> _Link:
        return self.rows[index]


class _Missing:
    def count(self) -> int:
        return 0

    def nth(self, index: int) -> _Link:
        raise IndexError(index)


class _Body:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self) -> str:
        return self.text


class _Inputs:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    def count(self) -> int:
        return len(self.rows)

    def nth(self, index: int):
        return self.rows[index]


class _InputNode:
    def __init__(self, ident: str, placeholder: str) -> None:
        self.ident = ident
        self.placeholder = placeholder

    def get_attribute(self, name: str) -> str:
        if name == "id":
            return self.ident
        if name == "placeholder":
            return self.placeholder
        return ""


class _Page:
    def __init__(
        self,
        url: str,
        links: list[_Link] | None = None,
        *,
        boxes: dict | None = None,
        body: str = "",
        inputs: list[_InputNode] | None = None,
    ) -> None:
        self.url = url
        self.gotos: list[str] = []
        self.box = _Box()
        self.links = _Links(links or [])
        self.boxes = boxes if boxes is not None else {QUICK_SEARCH_SELECTOR: self.box}
        self.body = _Body(body)
        self.inputs = _Inputs(inputs or [])

    def goto(self, url: str, **_kwargs) -> str:
        self.gotos.append(str(url))
        return str(url)

    def locator(self, selector: str):
        if selector in self.boxes:
            return self.boxes[selector]
        if selector == "body":
            return self.body
        if selector == "input":
            return self.inputs
        if (
            "/web/account/" in selector
            or "/applicantportal/" in selector
            or "listbox" in selector
            or "option" in selector
        ):
            return self.links
        return _Missing()


class LookupCloseTests(unittest.TestCase):
    def _send(self, db: str, job_id: str, text: str):
        adapter = _adapter_module()
        chat = _chat(db)
        with patch.object(adapter, "ROBIE_JOB_DB", db):
            result = asyncio.run(
                chat.send(SPACE, text, metadata={"robie_job_id": job_id})
            )
        return chat, result

    def test_no_browser_use_is_not_complete(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            chat, result = self._send(db, job_id, FALSE_MISS)
            posted = "\n".join(_outbound_text(chat))
            self.assertTrue(result.success)
            self.assertIn(LOOKUP_MISS, posted)
            self.assertNotIn("live EZLynx lookup failed", posted.casefold())
            self.assertNotEqual(
                store.get_job(job_id)["status"], JobStatus.COMPLETE.value
            )
            self.assertEqual(store.list_playwright_exec(job_id), [])

    def test_sign_in_page_is_not_complete(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            record_playwright_exec(
                f"page.goto('{SIGN_IN_URL}')",
                {"url": SIGN_IN_URL},
                job_id=job_id,
                db_path=db,
                status="ok",
            )
            chat, result = self._send(db, job_id, FALSE_MISS)
            posted = "\n".join(_outbound_text(chat))
            self.assertTrue(result.success)
            self.assertIn("sign in to EZLynx", posted)
            self.assertNotIn("live EZLynx lookup failed", posted.casefold())
            self.assertNotEqual(
                store.get_job(job_id)["status"], JobStatus.COMPLETE.value
            )
            self.assertEqual(
                store.get_job(job_id)["status"],
                JobStatus.NEEDS_CLARIFICATION.value,
            )

    def test_real_read_completes_with_the_answer(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store)
            store.checkpoint(
                job_id,
                "client_name_search",
                {
                    "resolved": True,
                    "source": "search",
                    "applicant_ids": [FOUND_ID],
                    "user_line": "",
                },
            )
            payload = dict(store.get_job(job_id).get("payload") or {})
            payload["applicant_id"] = FOUND_ID
            store.update_payload(job_id, payload)
            record_playwright_exec(
                f"page.goto('{ACCOUNT}')",
                {"url": ACCOUNT},
                job_id=job_id,
                db_path=db,
                status="ok",
            )
            chat, result = self._send(db, job_id, ANSWER)
            posted = "\n".join(_outbound_text(chat))
            self.assertTrue(result.success)
            self.assertIn("GL-100", posted)
            self.assertIn("Harbor", posted)
            self.assertEqual(
                store.get_job(job_id)["status"], JobStatus.COMPLETE.value
            )


class NameSearchTests(unittest.TestCase):
    def test_search_types_the_name_and_does_not_guess_a_url(self):
        page = _Page(
            "https://app.ezlynx.com/",
            [_Link(f"https://app.ezlynx.com/web/account/{FOUND_ID}/overview", "Buster Brown")],
        )
        outcome = read_applicant_search(page, "buster brown")
        self.assertEqual(page.box.fills, ["buster brown"])
        self.assertEqual(page.box.pressed, ["Enter"])
        self.assertEqual(page.gotos, [])
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["matches"][0]["applicant_id"], FOUND_ID)

    def test_one_match_binds_only_the_search_result(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store, applicant_id=DOCS_ID)
            line = prepare_named_client_lookup(
                store,
                job_id,
                searcher=_searcher([{"applicant_id": FOUND_ID, "name": "Buster Brown"}]),
            )
            payload = store.get_job(job_id)["payload"]
            self.assertIsNone(line)
            self.assertEqual(payload["applicant_id"], FOUND_ID)
            self.assertNotEqual(payload.get("applicant_id"), DOCS_ID)
            self.assertNotIn(DOCS_ID, payload.values())
            from robie_job_engine.chat_guard import stop_generic_chat_job_heartbeat

            try:
                execution = build_chat_execution_text(db, job_id, ASK)
                self.assertIn(FOUND_ID, execution)
                self.assertNotIn(DOCS_ID, execution)
                self.assertIn("Do not guess an EZLynx URL", execution)
            finally:
                stop_generic_chat_job_heartbeat(db, job_id)

    def test_several_matches_ask_and_do_not_bind(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store, applicant_id=DOCS_ID)
            line = prepare_named_client_lookup(
                store,
                job_id,
                searcher=_searcher(
                    [
                        {"applicant_id": FOUND_ID, "name": "Buster Brown"},
                        {"applicant_id": "88001123", "name": "Buster Brown"},
                    ]
                ),
            )
            payload = store.get_job(job_id)["payload"]
            self.assertIn("I found more than one Buster Brown.", line or "")
            self.assertIn(f"1. Buster Brown, account {FOUND_ID}", line or "")
            self.assertIn("2. Buster Brown, account 88001123", line or "")
            self.assertTrue(str(line or "").endswith("Which one should I use?"))
            self.assertNotIn("applicant_id", line or "")
            self.assertNotIn("ROBIE_BLOCKED", line or "")
            self.assertFalse(str(payload.get("applicant_id") or ""))
            self.assertNotIn(DOCS_ID, payload.values())
            self.assertNotEqual(
                store.get_job(job_id)["status"], JobStatus.COMPLETE.value
            )

    def test_no_match_says_so_in_one_line(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store, applicant_id=DOCS_ID)
            line = prepare_named_client_lookup(
                store, job_id, searcher=_searcher([])
            )
            self.assertEqual(line, "I couldn't find a client named Buster Brown.")
            self.assertNotIn("\n", line or "")
            payload = store.get_job(job_id)["payload"]
            self.assertFalse(str(payload.get("applicant_id") or ""))
            self.assertNotIn(DOCS_ID, payload.values())

    def test_docs_applicant_is_never_used_for_a_named_client(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store, applicant_id=DOCS_ID)
            reason = refuse_named_lookup_navigation(
                store,
                store.get_job(job_id),
                f"page.goto('{DOCS_ACCOUNT}')",
            )
            self.assertIsNotNone(reason)
            self.assertIn(DOCS_ID, reason or "")
            self.assertNotEqual(
                store.get_job(job_id)["payload"].get("applicant_id"), FOUND_ID
            )
            prepare_named_client_lookup(
                store,
                job_id,
                searcher=_searcher([{"applicant_id": FOUND_ID, "name": "Buster Brown"}]),
            )
            self.assertEqual(
                store.get_job(job_id)["payload"]["applicant_id"], FOUND_ID
            )
            still = refuse_named_lookup_navigation(
                store,
                store.get_job(job_id),
                f"page.goto('{DOCS_ACCOUNT}')",
            )
            self.assertIn(DOCS_ID, still or "")
            allowed = refuse_named_lookup_navigation(
                store,
                store.get_job(job_id),
                f"page.goto('{ACCOUNT}')",
            )
            self.assertIsNone(allowed)
            guessed = refuse_named_lookup_navigation(
                store,
                store.get_job(job_id),
                "page.goto('https://app.ezlynx.com/web/search?q=buster')",
            )
            self.assertIn("search URL", guessed or "")

            class Page:
                def __init__(self) -> None:
                    self.urls: list[str] = []

                def goto(self, url, **_kwargs):
                    self.urls.append(str(url))
                    return url

            scope = {"Page": Page}
            with patch.dict(
                os.environ,
                {"ROBIE_JOB_ID": job_id, "ROBIE_JOB_DB": db},
                clear=False,
            ):
                install_account_nav_guard(scope)
                page = Page()
                with self.assertRaisesRegex(RuntimeError, DOCS_ID):
                    page.goto(DOCS_ACCOUNT)
            self.assertEqual(page.urls, [])

    def test_sign_in_during_search_asks_instead_of_binding(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = _job(store, applicant_id=DOCS_ID)
            line = prepare_named_client_lookup(
                store,
                job_id,
                searcher=_searcher([], status="sign_in"),
            )
            self.assertEqual(line, SIGN_IN_QUESTION)
            self.assertFalse(
                str(store.get_job(job_id)["payload"].get("applicant_id") or "")
            )


class SearchBoxTests(unittest.TestCase):
    def test_dashboard_box_is_tried_before_applicant_search(self):
        quick = _Box()
        older = _Box()
        page = _Page(
            "https://app.ezlynx.com/web/",
            [_Link(f"https://app.ezlynx.com/web/account/{FOUND_ID}/overview", "Buster Brown")],
            boxes={
                QUICK_SEARCH_SELECTOR: quick,
                APPLICANT_SEARCH_SELECTOR: older,
            },
        )
        outcome = read_applicant_search(page, "buster brown")
        self.assertEqual(quick.fills, ["buster brown"])
        self.assertEqual(older.fills, [])
        self.assertEqual(outcome["matches"][0]["applicant_id"], FOUND_ID)
        self.assertTrue(is_readonly_client_search("fill", QUICK_SEARCH_SELECTOR))

    def test_header_placeholder_is_the_last_search_box(self):
        header = _Box()
        page = _Page(
            "https://app.ezlynx.com/web/",
            [_Link(f"https://app.ezlynx.com/web/account/{FOUND_ID}/overview", "Buster Brown")],
            boxes={"header input[placeholder='Search']": header},
        )
        outcome = read_applicant_search(page, "buster brown")
        self.assertEqual(header.fills, ["buster brown"])
        self.assertEqual(outcome["status"], "ok")

    def test_unreadable_search_logs_the_url_and_not_other_clients(self):
        page = _Page(
            "https://app.ezlynx.com/web/",
            body="Welcome Alice Example",
            boxes={},
            inputs=[_InputNode("quickSearchInput", "Search")],
        )
        with self.assertLogs("robie.health", level="INFO") as logs:
            outcome = read_applicant_search(page, "buster brown")
        self.assertEqual(outcome["status"], "error")
        blob = "\n".join(logs.output)
        self.assertIn("https://app.ezlynx.com/web/", blob)
        self.assertIn("buster brown", blob)
        self.assertIn("quickSearchInput", blob)
        self.assertNotIn("Alice", blob)

    def test_dashboard_is_used_instead_of_an_unsaved_form(self):
        class _Dash:
            url = "https://app.ezlynx.com/web/"

        class _Form:
            url = "https://app.ezlynx.com/applicantportal/policy/actions/edit/1/2"

            def evaluate(self, _script: str) -> bool:
                return True

        form = _Form()
        dash = _Dash()
        self.assertIs(choose_client_search_page([form, dash]), dash)
        self.assertIsNone(choose_client_search_page([form]))

        class _Clean(_Form):
            def evaluate(self, _script: str) -> bool:
                return False

        clean = _Clean()
        self.assertIs(choose_client_search_page([clean]), clean)

    def test_default_named_lookup_never_attaches_shared_browser(self):
        # A name does not authorize this backport to acquire or use CDP.
        sync_api = types.ModuleType("playwright.sync_api")
        def forbidden():
            raise AssertionError("shared browser must not be touched")
        sync_api.sync_playwright = forbidden
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            job_id = _job(store, applicant_id=DOCS_ID)
            async def prepare():
                return prepare_named_client_lookup(store, job_id)
            with patch.dict(sys.modules, {"playwright.sync_api": sync_api}):
                line = asyncio.run(prepare())
            self.assertEqual(line, "Which EZLynx applicant ID should I use?")
            self.assertNotIn(DOCS_ID, store.get_job(job_id)["payload"].values())
            self.assertEqual(store.get_checkpoint(job_id, "client_name_search")["source"], "needs_id")
