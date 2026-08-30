import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job, pre_execution_hold_reason
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore

ROOT = Path(__file__).resolve().parents[1]


class GoogleChatCardTests(unittest.TestCase):
    def test_decorated_text_widget_supports_button(self):
        adapter_code = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('if widget.get("button"):', adapter_code)
        self.assertIn('decorated["button"] = _button_to_chat(widget["button"])', adapter_code)

    def test_send_clarify_wraps_long_choices_and_patches_card(self):
        adapter_code = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("has_long_choice = any(len(str(c).strip()) > 24 for c in choices)", adapter_code)
        self.assertIn('"type": "decorated_text"', adapter_code)
        self.assertIn('"wrap_text": True', adapter_code)
        self.assertIn('"UPDATE_MESSAGE"', adapter_code)
        self.assertIn('"cardsV2": []', adapter_code)

    def test_complete_new_business_request_does_not_add_generic_confirmation_hold(self):
        self.assertIsNone(
            pre_execution_hold_reason(
                "Create a new business policy for the exact applicant",
                {"target_match_count": 1},
            )
        )

    def test_bound_policy_actions_always_require_clarify_or_hitl(self):
        for action in ("renewal", "endorsement", "cancellation", "reassignment"):
            with self.subTest(action=action):
                self.assertIn(
                    "already-bound policy",
                    pre_execution_hold_reason(
                        f"Process this {action} with every field supplied",
                        {"target_match_count": 1},
                    ),
                )

    def test_ambiguous_conflicting_and_nonunique_targets_require_hold(self):
        self.assertIn("ambiguous", pre_execution_hold_reason("new policy", {"ambiguous_fields": ["carrier"]}))
        self.assertIn("conflicting", pre_execution_hold_reason("new policy", {"conflicting_fields": ["date"]}))
        self.assertIn("exactly one", pre_execution_hold_reason("new policy", {"target_match_count": 2}))

    def test_bound_policy_hold_changes_actual_job_state(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "behavioral-policy-hold",
                "Please process this renewal for applicant 123",
                action_payload={"target_match_count": 1},
            )
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["status"], JobStatus.NEEDS_CLARIFICATION.value)
            self.assertIn("already-bound policy", job["last_error"])


if __name__ == "__main__":
    unittest.main()
