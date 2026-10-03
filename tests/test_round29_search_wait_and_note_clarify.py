"""Search results load after Enter, and an ambiguous note stays a question."""

from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_turn_control import (
    agent_output_blocked,
    agent_stop_requested,
    clear_agent_stop,
)
from robie_job_engine.client_name_lookup import (
    SEARCH_RESULT_TIMEOUT_SECONDS,
    read_applicant_search,
)
from robie_job_engine.ezlynx_discussions import (
    ambiguous_discussion_question,
    recent_discussion_titles,
    select_discussion_for_note,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingStore
from robie_job_engine.store import JobStore
from robie_job_engine.turn_finalization import (
    COULD_NOT_FINISH,
    close_turn_after_visible_line,
    visible_fallback_line,
)
from robie_job_engine.user_reply import format_user_reply
from test_tonight_fix_bundle import _load_hermes_tool, _restore_modules


FOUND = "26356199"
SPACE = "spaces/round29"


class _Box:
    def __init__(self) -> None:
        self.fills: list[str] = []

    def count(self) -> int:
        return 1

    def fill(self, value: str) -> None:
        self.fills.append(value)

    def press(self, _key: str) -> None:
        return None


class _Missing:
    def count(self) -> int:
        return 0


class DelayedSearchTests(unittest.TestCase):
    def test_results_are_read_after_the_legacy_page_loads(self):
        class _Link:
            def get_attribute(self, name: str) -> str:
                if name == "href":
                    return f"https://app.ezlynx.com/web/account/{FOUND}/overview"
                return ""

            def inner_text(self) -> str:
                return "Buster Brown"

        class _Links:
            def __init__(self, page: "_Page") -> None:
                self.page = page

            def count(self) -> int:
                return 1 if self.page.ready else 0

            def nth(self, _index: int) -> _Link:
                return _Link()

            def wait_for(self, *, state: str = "attached", timeout: int = 0) -> None:
                self.page.waits.append(timeout)
                self.page.ready = True

        class _Page:
            def __init__(self) -> None:
                self.url = "https://app.ezlynx.com/web/"
                self.ready = False
                self.box = _Box()
                self.links = _Links(self)
                self.waits: list[int] = []
                self.url_timeout = 0

            def wait_for_url(self, _predicate, timeout: int = 0) -> None:
                self.url_timeout = timeout
                self.url = (
                    "https://app.ezlynx.com/applicantportal/Search/Index"
                    "?searchPhrase=buster+brown"
                )

            def locator(self, selector: str):
                if selector == "#quickSearchInput":
                    return self.box
                if "/web/account/" in selector or "listbox" in selector or "option" in selector:
                    return self.links
                if selector == "body":
                    return type("Body", (), {"inner_text": lambda self: "loading"})()
                return _Missing()

        page = _Page()
        with self.assertNoLogs("robie.health", level="INFO"):
            outcome = read_applicant_search(page, "buster brown")
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["matches"][0]["applicant_id"], FOUND)
        self.assertIn("Search/Index", page.url)
        self.assertGreaterEqual(page.url_timeout, 1000)
        self.assertLessEqual(page.url_timeout, SEARCH_RESULT_TIMEOUT_SECONDS * 1000)
        self.assertTrue(page.waits)
        self.assertLessEqual(page.waits[0], SEARCH_RESULT_TIMEOUT_SECONDS * 1000)


class AmbiguousDiscussionTests(unittest.TestCase):
    def test_several_matches_ask_and_leave_the_job_open(self):
        titles = [
            "Renewal March",
            "Renewal April",
            "Renewal May",
            "Renewal June",
            "Renewal July",
            "Renewal August",
        ]
        rows = [
            {"id": "old", "title": "Renewal 2020", "updatedAt": "2020-01-01"},
            {"id": "new", "title": "Renewal 2026", "updatedAt": "2026-09-01"},
            {"id": "mid", "title": "Renewal 2024", "updatedAt": "2024-01-01"},
        ]
        self.assertEqual(
            recent_discussion_titles(rows, limit=2),
            ["Renewal 2026", "Renewal 2024"],
        )
        try:
            select_discussion_for_note(rows, title_hint="Renewal")
        except Exception as exc:
            self.assertEqual(getattr(exc, "code", ""), "AMBIGUOUS_DISCUSSIONS")
            self.assertEqual(exc.matches[0], "Renewal 2026")
        question = ambiguous_discussion_question(titles, hint="Renewal")
        self.assertTrue(question.endswith("?"))
        self.assertIn("Renewal July", question)
        self.assertNotIn("Renewal August", question)

        tool, previous, created = _load_hermes_tool(
            "ezlynx_note_tool_round29", "ezlynx_note_tool.py"
        )
        try:
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                store = JobStore(db)
                job = store.create_job(
                    "hermes.plain_english",
                    {"text": "add a note on Renewal", "conversation_id": SPACE},
                )
                job_id = job["id"]
                store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
                sent: list[str] = []

                def _add(*_args, **_kwargs):
                    return {
                        "status": "pending",
                        "reason_code": "AMBIGUOUS_DISCUSSIONS",
                        "reason": "title hint 'Renewal' matched 62 discussions",
                        "matches": titles,
                        "applicant_id": "26356199",
                    }

                from unittest.mock import patch

                with patch(
                    "robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion",
                    side_effect=_add,
                ):
                    tool.ezlynx_discussion_note_handler(
                        {
                            "applicant_id": "26356199",
                            "note_text": "Robie was here",
                            "title_hint": "Renewal",
                        },
                        job_id=job_id,
                        db_path=db,
                        outcome_poster=lambda _space, text, _thread, _job: sent.append(text),
                    )
                closed = store.get_job(job_id)
                self.assertEqual(closed["status"], JobStatus.NEEDS_CLARIFICATION.value)
                self.assertEqual(len(sent), 1)
                self.assertIn("Renewal March", sent[0])
                self.assertNotIn("Renewal August", sent[0])
                self.assertTrue(sent[0].endswith("?"))
                self.assertIn("Renewal", store.get_checkpoint(job_id, "clarification")["question"])
        finally:
            clear_agent_stop(locals().get("job_id"))
            _restore_modules(previous, created)


class OutboundRequestTests(unittest.TestCase):
    def test_a_request_for_a_title_stays_and_becomes_a_question(self):
        shown = format_user_reply(
            "The note could not be completed automatically.\n"
            "Please provide the discussion title."
        )
        self.assertEqual(shown, "Please provide the discussion title?")
        self.assertNotIn("could not be completed", shown)
        coded = format_user_reply(
            "ROBIE_BLOCKED: The note could not be completed automatically. "
            "Please provide the discussion title."
        )
        self.assertEqual(coded, "Please provide the discussion title?")
        self.assertNotIn("ROBIE_BLOCKED", coded)


class SilentCloseTests(unittest.TestCase):
    def test_the_fallback_is_sent_before_the_job_closes(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "file the note", "conversation_id": SPACE},
            )
            job_id = job["id"]
            store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
            stop = Path(tmp) / "capture.stop"
            RecordingStore(db).create(job_id, Path(tmp) / "capture.webm", stop)
            try:
                line = visible_fallback_line(db, job_id)
                self.assertEqual(line, COULD_NOT_FINISH)
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
                self.assertFalse(agent_stop_requested(job_id))
                self.assertIsNone(agent_output_blocked(job_id, store))
                self.assertFalse(stop.is_file())
                close_turn_after_visible_line(db, job_id, "")
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.RUNNING.value)
                close_turn_after_visible_line(db, job_id, line)
                self.assertEqual(store.get_job(job_id)["status"], JobStatus.UNVERIFIED.value)
                self.assertTrue(agent_stop_requested(job_id))
                self.assertIsNotNone(agent_output_blocked(job_id, store))
                self.assertTrue(stop.is_file())
            finally:
                clear_agent_stop(job_id)

    def test_a_terminal_close_writes_the_capture_stop_file(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": "file the note", "conversation_id": SPACE},
            )
            job_id = job["id"]
            store.transition(job_id, JobStatus.RUNNING, expected={JobStatus.PENDING})
            stop = Path(tmp) / "recorder.stop"
            RecordingStore(db).create(job_id, Path(tmp) / "recorder.webm", stop)
            self.assertFalse(stop.is_file())
            store.transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error="stopped",
                release_lease=True,
            )
            self.assertTrue(stop.is_file())


if __name__ == "__main__":
    unittest.main()
