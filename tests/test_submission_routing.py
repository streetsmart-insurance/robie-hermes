import unittest

from robie_job_engine.submission_routing import (
    choose_active_discussion,
    resolve_submission_route,
    submission_verification_requirements,
)


class SubmissionRoutingTests(unittest.TestCase):
    def test_new_business_submission_uses_submission_folder_and_activity_note(self):
        route = resolve_submission_route(is_renewal=False)
        self.assertIn("Submission Center folder", route.document_destination)
        self.assertEqual(
            route.note_destination,
            "Submission Center activity discussion for this submission",
        )
        self.assertIn("New Business", route.workflow_discussion_candidates)

    def test_high_risk_manual_renewal_starts_workflow_but_note_stays_with_submission(self):
        route = resolve_submission_route(
            is_renewal=True, is_manual_renewal=True, is_high_risk=True
        )
        self.assertEqual(route.workflow_to_start, "High Risk Renewal")
        self.assertEqual(
            route.workflow_discussion_candidates[:2],
            ("Renewal Manual", "High Risk Renewal"),
        )
        self.assertTrue(route.note_destination.startswith("Submission Center activity"))
        checks = submission_verification_requirements(route)
        self.assertEqual(
            checks["workflow"], {"required": True, "name": "High Risk Renewal"}
        )
        self.assertTrue(checks["note"]["untitled_forbidden"])

    def test_discussion_with_activity_wins_over_empty_or_untitled_match(self):
        discussions = [
            {"id": "empty", "title": "Renewal Manual", "activity_count": 0},
            {"id": "wrong", "title": "Untitled", "activity_count": 50},
            {
                "id": "active",
                "title": "High Risk Renewal",
                "activity_count": 3,
                "has_meaningful_activity": True,
                "last_activity_at": "2026-08-22T10:00:00-04:00",
            },
        ]
        chosen = choose_active_discussion(
            discussions, ["Renewal Manual", "High Risk Renewal", "Submission Center"]
        )
        self.assertEqual(chosen["id"], "active")

    def test_more_recent_meaningful_discussion_breaks_tie(self):
        discussions = [
            {
                "id": "old",
                "title": "Renewals",
                "activity_count": 2,
                "last_activity_at": "2026-08-01",
            },
            {
                "id": "new",
                "title": "Renewal Manual",
                "activity_count": 2,
                "last_activity_at": "2026-08-21",
            },
        ]
        chosen = choose_active_discussion(discussions, ["Renewals", "Renewal Manual"])
        self.assertEqual(chosen["id"], "new")


if __name__ == "__main__":
    unittest.main()
