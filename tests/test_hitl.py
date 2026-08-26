from __future__ import annotations

import unittest

from robie_job_engine.hitl import (
    classify_human_reply,
    human_reply_value,
    interaction_for_blocker,
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
        self.assertIn("Hey Carlo, I need a quick hand!", state["prompt"])
        self.assertIn("Example Company", state["prompt"])
        self.assertIn("Please reply in this thread", state["prompt"])
        self.assertIn("Job ID: `50b9e6a1`", state["prompt"])

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
                self.assertIn("Do not send the value in Chat", state["prompt"])

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


if __name__ == "__main__":
    unittest.main()
