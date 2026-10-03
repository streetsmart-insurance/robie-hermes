"""Regressions from the 2026-10-02 Test round on head 68f441d.

A discussion-card click still saw the clarify stop. A named discussion
note was refused as an untrusted applicant and planned as a policy change.
The planned note repeated the prompt.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_turn_control import (
    STOPPED_OUTPUT,
    agent_stop_requested,
    clear_agent_stop,
    refuse_current_tool_call,
    request_agent_stop,
)
from robie_job_engine.ezlynx_write_scope import requested_message_applicant
from robie_job_engine.live_turn_guard import (
    _BOUND_RESUME,
    bind_card_click_resume,
    person_name_in_text,
    refuse_untrusted_applicant,
    set_turn_job,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.request_routing import (
    classify_request,
    discussion_note_body,
    parse_named_discussion_note,
)
from robie_job_engine.store import JobStore
from robie_job_engine.write_verification_loop import coerce_tool_plan

MESSAGE = (
    'Add a note to Buster Brown 26356199 on the discussion '
    '"Policy Change Request Checkup - Mailing Address update": '
    "Round 725 named discussion test, please ignore"
)
TITLE = "Policy Change Request Checkup - Mailing Address update"
BODY = "Round 725 named discussion test, please ignore"
REPEATED = (
    "Round 725 named discussion test, please ignore "
    "Add a note to Buster Brown 26356199: "
    "Round 725 named discussion test, please ignore"
)


class CardClickResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        _BOUND_RESUME.clear()
        set_turn_job("")

    def tearDown(self) -> None:
        _BOUND_RESUME.clear()
        set_turn_job("")

    def test_card_click_after_the_clarify_card_can_read_back(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {"text": MESSAGE, "request_text": MESSAGE},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                job["id"],
                JobStatus.NEEDS_CLARIFICATION,
                expected={JobStatus.RUNNING},
                error="waiting on the user",
                resume_status=JobStatus.PENDING,
                release_lease=True,
            )
            request_agent_stop(job["id"])
            self.assertEqual(
                refuse_current_tool_call({"job_id": job["id"], "db_path": db}),
                STOPPED_OUTPUT,
            )
            bind_card_click_resume(
                None,
                store,
                job["id"],
                "agent:main:google_chat:dm:spaces/ROBY:note",
                "Which discussion?",
            )
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.RUNNING.value)
            self.assertFalse(agent_stop_requested(job["id"]))
            self.assertIsNone(
                refuse_current_tool_call({"job_id": job["id"], "db_path": db})
            )
            clear_agent_stop(job["id"])

    def test_a_real_stop_still_wins_over_the_card_click(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {"text": MESSAGE, "request_text": MESSAGE},
            )
            store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
            store.transition(
                job["id"],
                JobStatus.CANCELLED,
                expected={JobStatus.RUNNING},
                error="Cancelled.",
                release_lease=True,
            )
            store.checkpoint(job["id"], "cancelled", {"by": "/stop", "reason": "Cancelled."})
            request_agent_stop(job["id"])
            bind_card_click_resume(None, store, job["id"], "agent:main:session")
            self.assertEqual(store.get_job(job["id"])["status"], JobStatus.CANCELLED.value)
            self.assertTrue(agent_stop_requested(job["id"]))
            self.assertEqual(
                refuse_current_tool_call({"job_id": job["id"], "db_path": db}),
                STOPPED_OUTPUT,
            )
            clear_agent_stop(job["id"])


class NamedDiscussionNoteTests(unittest.TestCase):
    def test_named_discussion_message_parses_applicant_title_and_body(self):
        parsed = parse_named_discussion_note(MESSAGE)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["applicant_id"], "26356199")
        self.assertEqual(parsed["discussion"], TITLE)
        self.assertEqual(parsed["body"], BODY)
        self.assertEqual(
            requested_message_applicant({"request_text": MESSAGE}),
            "26356199",
        )
        self.assertEqual(
            requested_message_applicant({"text": "Buster Brown 26356199"}),
            "26356199",
        )
        self.assertEqual(
            requested_message_applicant({"text": "26356199"}),
            "26356199",
        )
        self.assertIsNone(
            requested_message_applicant({"text": "Buster Brown 26356199 and 88001122"})
        )
        self.assertEqual(
            requested_message_applicant(
                {"text": "Create policy number TEST-HO-20260911-E01 on applicant 220250093"}
            ),
            "220250093",
        )
        self.assertIsNone(
            requested_message_applicant(
                {"text": "Create policy number TEST-HO-20260911-E01"}
            )
        )
        self.assertIsNone(person_name_in_text("Round 725 named discussion test"))
        self.assertEqual(person_name_in_text("Add a note for Buster Brown"), "Buster Brown")
        self.assertEqual(classify_request(MESSAGE).action_type, "ezlynx.discussion_note")
        self.assertEqual(
            classify_request("Change the mailing address to 1 Main St").action_type,
            "ezlynx.policy_change",
        )
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.discussion_note",
                {"text": MESSAGE, "request_text": MESSAGE},
            )
            self.assertIsNone(refuse_untrusted_applicant(store, store.get_job(job["id"]), "26356199"))

    def test_note_body_is_only_the_text_after_the_colon(self):
        self.assertEqual(discussion_note_body(MESSAGE), BODY)
        self.assertEqual(discussion_note_body(REPEATED), BODY)
        self.assertNotIn("Add a note", discussion_note_body(REPEATED))
        self.assertNotIn("26356199", discussion_note_body(REPEATED))
        plan = coerce_tool_plan(
            {
                "write": "discussion note",
                "target": {"discussion": TITLE, "applicant_id": "26356199"},
                "values": {"note_text": REPEATED},
            },
            {"note_text": REPEATED, "title_hint": TITLE},
        )
        self.assertEqual(plan["values"]["note_text"], BODY)


if __name__ == "__main__":
    unittest.main()
