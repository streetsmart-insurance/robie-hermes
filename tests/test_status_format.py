"""Regression tests for the simple shared status format.

Jake's standing template (2026-09-28):
    What happened: <plain words>
    Anything needed: <one line>
    Status: <plain verification state>

Contract enforced here for every renderer (Chat terminal, email reply,
action-gate note):
- The plain-words outcome headline is the first line.
- The three labeled lines appear in order.
- No job IDs or internal codes (ROBIE Job <id>, ROBIE_OUTCOME_UNKNOWN,
  ...) appear above the Details section.
- The job ID is a small reference line at the very bottom.
- Our verification detail is preserved below the three lines.
"""
import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine import status_format
from robie_job_engine.action_gate import format_action_gate_chat_note
from robie_job_engine.chat_guard import _render_chat_terminal
from robie_job_engine.email_guard import _render_email_terminal
from robie_job_engine.models import JobStatus
from robie_job_engine.recording import RecordingManager
from robie_job_engine.store import JobStore

JOB_ID = "9d2098e1-aaaa-bbbb-cccc-d1e5f6a7b8c9"


def _top(text):
    """Everything above the Details section."""
    return text.split("\nDetails")[0]


class RenderSimpleStatusTests(unittest.TestCase):
    def test_headline_first_labels_in_order_ref_last(self):
        text = status_format.render_simple_status(
            headline="Not verified.",
            what_happened="The email job timed out.",
            anything_needed="Check the saved results before retrying.",
            status_line="Not verified — treat as incomplete until confirmed.",
            details="some detail",
            job_id=JOB_ID,
        )
        lines = text.splitlines()
        self.assertEqual(lines[0], "Not verified.")
        self.assertLess(lines.index("What happened: The email job timed out."),
                        lines.index("Anything needed: Check the saved results before retrying."))
        self.assertLess(lines.index("Anything needed: Check the saved results before retrying."),
                        lines.index("Status: Not verified — treat as incomplete until confirmed."))
        self.assertEqual(lines[-1], "Ref: job " + JOB_ID)
        self.assertIn("Details", lines)

    def test_no_details_section_when_empty_and_no_ref_without_job(self):
        text = status_format.render_simple_status(
            headline="Done.",
            what_happened="All good.",
            anything_needed="No.",
            status_line="Verified.",
        )
        self.assertNotIn("Details", text)
        self.assertNotIn("Ref:", text)


class PlainReasonTests(unittest.TestCase):
    def test_strips_outcome_unknown_prefix(self):
        self.assertEqual(
            status_format.plain_reason("ROBIE_OUTCOME_UNKNOWN: Email execution timed out."),
            "The email job timed out.",
        )

    def test_strips_execution_blocked_prefix(self):
        self.assertEqual(
            status_format.plain_reason("ROBIE_EXECUTION_BLOCKED: Email execution requires an active durable job."),
            "Email execution requires an active durable job.",
        )

    def test_translates_worker_sentences(self):
        self.assertIn(
            "stopped before producing a final answer",
            status_format.plain_reason("ROBIE_OUTCOME_UNKNOWN: The agent ended without a complete final turn."),
        )
        self.assertIn(
            "couldn't safely finish",
            status_format.plain_reason("ROBIE could not safely finish the requested work."),
        )

    def test_unknown_text_passes_through(self):
        self.assertEqual(status_format.plain_reason("custom reason here"), "custom reason here")


class EmailTerminalTests(unittest.TestCase):
    def test_unverified_timeout_matches_jakes_template(self):
        text = _render_email_terminal(
            job_id=JOB_ID,
            status=JobStatus.UNVERIFIED,
            response="ROBIE_OUTCOME_UNKNOWN: Email execution timed out. Check saved results before retrying.",
            summary="",
        )
        self.assertTrue(text.startswith("Not verified."))
        self.assertIn("What happened: The email job timed out.", text)
        self.assertIn("Anything needed: Check the saved results before retrying.", text)
        self.assertIn("Status: Not verified", text)
        top = _top(text)
        self.assertNotIn("ROBIE_OUTCOME_UNKNOWN", top)
        self.assertNotIn("ROBIE Job", top)
        self.assertNotIn(JOB_ID, top)
        # Raw worker report is preserved for debugging, below the fold.
        self.assertIn("ROBIE_OUTCOME_UNKNOWN", text)
        self.assertTrue(text.rstrip().endswith("Ref: job " + JOB_ID))

    def test_complete_says_nothing_needed(self):
        text = _render_email_terminal(
            job_id=JOB_ID,
            status=JobStatus.COMPLETE,
            response="done",
            summary="Filed.",
        )
        self.assertTrue(text.startswith("Done."))
        self.assertIn("Anything needed: No.", text)
        self.assertIn("Status: Verified", text)
        self.assertNotIn("ROBIE Job", _top(text))

    def test_failed_headline(self):
        text = _render_email_terminal(
            job_id=JOB_ID,
            status=JobStatus.FAILED,
            response="ROBIE_EXECUTION_BLOCKED: boom",
            summary="",
        )
        self.assertTrue(text.startswith("Couldn't finish."))
        self.assertIn("Status: Failed.", text)
        self.assertNotIn("ROBIE_EXECUTION_BLOCKED", _top(text))


class ChatTerminalTests(unittest.TestCase):
    def _render(self, status, error=""):
        tmp = durable_temporary_directory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "jobs.db")
        store = JobStore(db)
        created = store.create_job("hermes.google_chat_task", {"requested_by": "Jake"},
                                   idempotency_key=f"status-format-{status.value}-{id(self)}")
        job_id = created["id"]
        if status == JobStatus.COMPLETE:
            # The store's complete-guard requires verifier authority plus
            # evidence; this is a renderer test, so set the terminal state
            # directly (the guard itself is covered by its own tests).
            conn = store.connect()
            conn.execute("UPDATE jobs SET status=? WHERE id=?",
                         (JobStatus.COMPLETE.value, job_id))
            conn.commit()
        else:
            store.transition(job_id, status, expected={JobStatus.PENDING},
                             error=error or None, release_lease=True)
        job = store.get_job(job_id)
        return _render_chat_terminal(store, job, "worker said hi", RecordingManager(db))

    def test_complete(self):
        text = self._render(JobStatus.COMPLETE)
        self.assertTrue(text.startswith("Done."))
        self.assertIn("Anything needed: No.", text)
        self.assertIn("Status: Verified", text)
        self.assertNotIn("ROBIE Job", _top(text))

    def test_failed(self):
        text = self._render(JobStatus.FAILED, error="ROBIE_OUTCOME_UNKNOWN: Email execution timed out.")
        self.assertTrue(text.startswith("Couldn't finish."))
        self.assertIn("Status: Failed.", text)
        self.assertIn("The email job timed out.", text)
        self.assertNotIn("ROBIE_OUTCOME_UNKNOWN", _top(text))
        self.assertIn("Technical detail: ROBIE_OUTCOME_UNKNOWN", text)

    def test_unverified(self):
        text = self._render(JobStatus.UNVERIFIED)
        self.assertTrue(text.startswith("Not verified."))
        self.assertIn("don't treat this as done", text)
        self.assertIn("Status: Not verified", text)
        self.assertNotIn("ROBIE Job", _top(text))

    def test_waiting_on_human(self):
        text = self._render(JobStatus.AWAITING_HUMAN_INPUT, error="need FEIN")
        self.assertTrue(text.startswith("Waiting on you."))
        self.assertIn("Status: Not finished.", text)


class ActionGateNoteTests(unittest.TestCase):
    def test_refusal_uses_simple_format(self):
        text = format_action_gate_chat_note({"id": JOB_ID, "last_error": "gated: no test pass"})
        self.assertTrue(text.startswith("Couldn't finish."))
        self.assertIn("What happened:", text)
        self.assertIn("Anything needed:", text)
        self.assertIn("Status:", text)
        self.assertNotIn("No Ascend API request was sent.", text)
        self.assertIn("Nothing was sent and nothing was changed.", text)
        self.assertTrue(text.rstrip().endswith("Ref: job " + JOB_ID))
        self.assertNotIn("ROBIE Job", _top(text))

    def test_ascend_refusal_still_says_no_api_request_was_sent(self):
        text = format_action_gate_chat_note(
            {
                "id": JOB_ID,
                "action_type": "hermes.google_chat_task",
                "last_error": "ACTION_GATE_REFUSED: Test has no clean pass for action ascend.create_program",
                "payload": {"text": "Create a program in Ascend"},
            }
        )
        self.assertIn("No Ascend API request was sent.", text)
        self.assertIn(
            "What happened: This action is blocked until a clean Test pass is on file.",
            text,
        )
        self.assertNotIn("ACTION_GATE_REFUSED", text.split("\nDetails")[0])
        self.assertIn("Technical detail: ACTION_GATE_REFUSED", text)


if __name__ == "__main__":
    unittest.main()
