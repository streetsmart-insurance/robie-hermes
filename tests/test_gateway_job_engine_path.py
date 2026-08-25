from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GatewayJobEnginePathTests(unittest.TestCase):
    def test_systemd_path_loads_canonical_job_engine_first(self):
        drop_in = (
            ROOT / "deploy/systemd/zz-hermes-gateway-job-engine-path.conf"
        ).read_text(encoding="utf-8")
        expected = (
            "PYTHONPATH=/opt/streetsmart-hermes/robie-job-engine:"
            "/srv/robie/current/vendor:/srv/robie/current"
        )
        self.assertIn(expected, drop_in)
        self.assertIn(
            "ROBIE_CANONICAL_JOB_ENGINE_ROOT=/opt/streetsmart-hermes/robie-job-engine",
            drop_in,
        )

    def test_google_chat_adapter_fails_closed_on_stale_job_engine_import(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_EXPECTED_JOB_ENGINE_ROOT", adapter)
        self.assertIn("loaded ROBIE Job Engine from a non-canonical path", adapter)

    def test_live_dm_text_routes_to_bounded_submission_audit(self):
        from robie_job_engine.request_routing import classify_request

        request = (
            "Production test: Run one read-only Submission Center audit. "
            "Verify All Submissions; agency Streetsmart Insurance with My "
            "Submissions cleared; exactly 100 mat-row elements; the live pager "
            "total; Status aria-sort=ascending with a visibly non-closed first "
            "row; and inspect through the first closed row. Do not modify "
            "EZLynx records, create notes or tasks, or send emails."
        )
        self.assertEqual(
            classify_request(request).action_type,
            "ezlynx.submission_audit",
        )


if __name__ == "__main__":
    unittest.main()
