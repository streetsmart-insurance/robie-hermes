"""Unit tests for Bland AI configuration per Jake's specs."""

import unittest

from robie_job_engine.bland_config import (
    CALLBACK_NUMBER,
    CALLER_ID,
    KAREN_VOICE_ID,
    BlandCallConfig,
    BlandRedialPolicy,
    CallAttempt,
    create_bland_call_payload,
)


class TestBlandConfig(unittest.TestCase):
    def test_voice_id(self):
        self.assertEqual(
            KAREN_VOICE_ID, "29158307-9893-4149-8a75-bc9ce313d64e"
        )

    def test_callback_number_carlo_correction(self):
        # Carlo corrected Jake's 732-481-2520 to 732-462-8343
        self.assertEqual(CALLBACK_NUMBER, "732-462-8343")

    def test_caller_id(self):
        self.assertEqual(CALLER_ID, "+17322986745")

    def test_recording_off(self):
        config = BlandCallConfig()
        self.assertFalse(config.record)

    def test_intro_script(self):
        config = BlandCallConfig()
        intro = config.intro_template.format(reason="following up on your policy.")
        self.assertIn("AI assistant", intro)
        self.assertIn("Jake", intro)
        self.assertIn("StreetSmart Insurance", intro)
        self.assertIn("following up on your policy.", intro)


class TestBlandRedialPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = BlandRedialPolicy()

    def test_attempt1_voicemail_should_retry(self):
        attempt = CallAttempt(attempt_number=1, ended_by_voicemail=True)
        self.assertTrue(self.policy.should_retry(attempt))

    def test_attempt1_voicemail_retry_delay(self):
        attempt = CallAttempt(attempt_number=1, ended_by_voicemail=True)
        self.assertEqual(self.policy.get_retry_delay(attempt), 10)

    def test_attempt1_voicemail_no_message(self):
        attempt = CallAttempt(attempt_number=1, ended_by_voicemail=True)
        self.assertFalse(self.policy.should_leave_message(attempt))

    def test_attempt2_voicemail_no_retry(self):
        attempt = CallAttempt(attempt_number=2, ended_by_voicemail=True)
        self.assertFalse(self.policy.should_retry(attempt))

    def test_attempt2_voicemail_leave_message(self):
        attempt = CallAttempt(attempt_number=2, ended_by_voicemail=True)
        self.assertTrue(self.policy.should_leave_message(attempt))

    def test_human_answer_no_retry(self):
        attempt = CallAttempt(attempt_number=1, ended_by_voicemail=False)
        self.assertFalse(self.policy.should_retry(attempt))
        self.assertFalse(self.policy.should_leave_message(attempt))

    def test_voicemail_message_has_callback(self):
        msg = self.policy.build_voicemail_message("test reason")
        self.assertIn("AI assistant", msg)
        self.assertIn("7 3 2, 4 6 2, 8 3 4 3", msg)  # Spoken callback


class TestCreateBlandCallPayload(unittest.TestCase):
    def test_payload_structure(self):
        payload = create_bland_call_payload(
            to_number="+17326688161",
            reason="following up on your renewal.",
        )
        self.assertEqual(payload["phone_number"], "+17326688161")
        self.assertEqual(payload["voice"], KAREN_VOICE_ID)
        self.assertEqual(payload["from"], CALLER_ID)
        self.assertFalse(payload["record"])
        self.assertIn("AI assistant", payload["task"])
        self.assertIn("Jake", payload["task"])


if __name__ == "__main__":
    unittest.main()
