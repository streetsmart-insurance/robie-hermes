from __future__ import annotations

import unittest

from robie_job_engine.hitl import (
    classify_human_reply,
    human_reply_value,
    interaction_for_blocker,
    sanitize_hitl_chat_text,
    structured_blocker_reason,
)


class HumanInTheLoopContractTests(unittest.TestCase):
    def test_missing_field_prompt_is_specific_and_resumable(self):
        state = interaction_for_blocker(
            "MISSING_REQUIRED_FIELD: NAICS code",
            action_type="ezlynx.form_setup",
        )
        self.assertEqual(state["awaiting"], "human_input")
        self.assertEqual(state["field_name"], "NAICS code")
        self.assertEqual(state["field_label"], "Industry Code (NAICS)")
        self.assertIn("Industry Code (NAICS)", state["prompt"])

    def test_generic_playwright_blocker_requests_retry_or_correction(self):
        state = interaction_for_blocker(
            "PLAYWRIGHT_BLOCKED: submit control not found",
            action_type="ezlynx.submission_audit",
        )
        self.assertEqual(state["field_name"], "operator_response")
        self.assertIn("RETRY", state["prompt"])
        self.assertIn("Reply in this Chat thread", state["prompt"])
        self.assertNotIn("PLAYWRIGHT_BLOCKED", state["prompt"])
        self.assertNotIn("Listen up", state["prompt"])
        self.assertNotIn("ain't", state["prompt"])

    def test_cowboy_slang_hitl_is_rewritten_before_send(self):
        cowboy = (
            "Listen up, Jake! it ain't my fault I cannot open "
            "/opt/streetsmart-hermes/robie-job-engine/data/artifacts/"
            "eb96f620-f8c3-4006-8eb4-d3a41af0e-ca7f-4a57-8cae-67d3ca55c1c5/"
            "quote.pdf"
        )
        rewritten = sanitize_hitl_chat_text(cowboy)
        self.assertNotEqual(rewritten, cowboy)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", rewritten)
        self.assertIn("/artifacts/", rewritten)
        self.assertIn("RETRY", rewritten)
        self.assertNotIn("Listen up", rewritten)
        self.assertNotIn("ain't", rewritten)
        self.assertNotIn("Jake!", rewritten)

        posted: list[str] = []

        class _Create:
            def __init__(self, kwargs: dict) -> None:
                posted.append(kwargs["body"]["text"])

            def execute(self) -> dict:
                return {
                    "name": "spaces/x/messages/1",
                    "thread": {"name": "spaces/x/threads/1"},
                }

        class _Messages:
            def create(self, **kwargs):
                return _Create(kwargs)

        class _Spaces:
            def messages(self) -> _Messages:
                return _Messages()

        class _Chat:
            def spaces(self) -> _Spaces:
                return _Spaces()

        from robie_job_engine.chat_app_post import post_as_chat_app

        post_as_chat_app("spaces/hitl", cowboy, chat=_Chat())
        self.assertEqual(len(posted), 1)
        self.assertNotIn("PLAYWRIGHT_BLOCKED", posted[0])
        self.assertNotIn("Listen up", posted[0])
        self.assertNotIn("ain't", posted[0])

        import tempfile

        from robie_job_engine.store import JobStore
        from robie_job_engine.worker_contract import sanitize_worker_response

        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(f"{tmp}/jobs.db")
            job = store.create_job("hermes.google_chat_task", {"text": "retry"})
            payload = sanitize_worker_response(store, job["id"], cowboy)
        self.assertTrue(payload["rewritten"])
        self.assertNotIn("PLAYWRIGHT_BLOCKED", payload["response_text"])
        self.assertNotIn("Listen up", payload["response_text"])
        self.assertNotIn("ain't", payload["response_text"])

    def test_fein_value_may_be_requested_in_chat(self):
        state = interaction_for_blocker(
            "MISSING_REQUIRED_FIELD: FEIN",
            action_type="ezlynx.form_setup",
            requester_name="Carlo",
            job_id="50b9e6a1-23dc-4665-b2d7-535eca7cc3ff",
            subject_name="Example Company",
        )
        self.assertTrue(state["accepts_value"])
        self.assertEqual(
            state["field_label"],
            "Federal Employer Identification Number (FEIN)",
        )
        self.assertIn("Carlo, I need a quick hand.", state["prompt"])
        self.assertIn("Example Company", state["prompt"])
        self.assertIn("Reply in this Chat thread", state["prompt"])
        self.assertNotIn("Job ID:", state["prompt"])

    def test_other_sensitive_field_values_are_never_requested_in_chat(self):
        for field_name in (
            "SSN",
            "password",
            "MFA",
            "payment",
            "card number",
            "bank account",
            "routing number",
        ):
            with self.subTest(field_name=field_name):
                state = interaction_for_blocker(
                    f"MISSING_REQUIRED_FIELD: {field_name}",
                    action_type="ezlynx.form_setup",
                )
                self.assertFalse(state["accepts_value"])
                self.assertIn("Do not send that value in Chat", state["prompt"])

    def test_empty_reply_is_rejected(self):
        with self.assertRaises(ValueError):
            human_reply_value("   ")

    def test_fein_and_naics_data_are_resumption_answers(self):
        self.assertEqual(
            classify_human_reply(
                "12-3456789",
                {"field_name": "FEIN", "accepts_value": True},
            ),
            "ANSWER",
        )
        self.assertEqual(
            classify_human_reply(
                "NAICS 541611",
                {"field_name": "NAICS code", "accepts_value": True},
            ),
            "ANSWER",
        )

    def test_new_dm_question_is_not_consumed_as_human_input(self):
        interaction = {"field_name": "FEIN", "accepts_value": True}
        self.assertEqual(
            classify_human_reply(
                "Show me all my currently scheduled jobs",
                interaction,
            ),
            "NEW_INTENT",
        )
        self.assertEqual(
            classify_human_reply("What jobs are pending?", interaction),
            "NEW_INTENT",
        )

    def test_invalid_field_data_keeps_the_job_parked(self):
        self.assertEqual(
            classify_human_reply(
                "123",
                {"field_name": "FEIN", "accepts_value": True},
            ),
            "INVALID",
        )

    def test_sensitive_field_accepts_retry_but_not_a_value(self):
        interaction = {"field_name": "SSN", "accepts_value": False}
        self.assertEqual(classify_human_reply("RETRY", interaction), "ANSWER")
        self.assertEqual(classify_human_reply("123-45-6789", interaction), "INVALID")

    def test_coverage_amount_reply_is_an_answer_not_a_new_job(self):
        interaction = {"field_name": "operator_response", "accepts_value": True}
        reply = (
            "Coverage A $1,200,000; B $120,000; C $500,000; D $500,000; F $10,000"
        )
        self.assertEqual(classify_human_reply(reply, interaction), "ANSWER")
        self.assertEqual(
            classify_human_reply("What jobs are pending?", interaction),
            "NEW_INTENT",
        )


class StructuredBlockerTriggerTests(unittest.TestCase):
    """The HITL trigger must catch the raw guard error, not only the
    ROBIE_BLOCKED:-prefixed form. Workers paste raw PLAYWRIGHT_BLOCKED
    lines; requiring the prefix was why HITL never fired (2026-09-10)."""

    def test_prefixed_blocker_still_triggers(self):
        reason = structured_blocker_reason(
            "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: submit control not found"
        )
        self.assertIsNotNone(reason)
        self.assertIn("PLAYWRIGHT_BLOCKED", reason)

    def test_bare_playwright_blocked_line_triggers(self):
        reason = structured_blocker_reason(
            "Tried the combobox twice.\n"
            "PLAYWRIGHT_BLOCKED: write target matched 3 fields; refuse to guess"
        )
        self.assertIsNotNone(reason)
        self.assertIn("matched 3 fields", reason)

    def test_bare_missing_field_line_triggers(self):
        reason = structured_blocker_reason(
            "I looked through the application.\nMISSING_REQUIRED_FIELD: FEIN"
        )
        self.assertIsNotNone(reason)
        self.assertIn("FEIN", reason)

    def test_ordinary_prose_does_not_trigger(self):
        self.assertIsNone(
            structured_blocker_reason(
                "I verified the mortgagee clause on screen. Everything looks good."
            )
        )

    def test_mention_inside_a_sentence_does_not_trigger(self):
        # The marker must be a full line of its own, not mid-sentence prose.
        self.assertIsNone(
            structured_blocker_reason(
                "The last run ended with a PLAYWRIGHT_BLOCKED error, "
                "but I worked around it and finished."
            )
        )


if __name__ == "__main__":
    unittest.main()
