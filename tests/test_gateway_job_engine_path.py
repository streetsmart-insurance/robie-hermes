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
            "PYTHONPATH=/opt/streetsmart-hermes/releases/current:"
            "/srv/robie/current/vendor:/srv/robie/current"
        )
        self.assertIn(expected, drop_in)
        self.assertIn(
            "ROBIE_CANONICAL_JOB_ENGINE_ROOT=/opt/streetsmart-hermes/releases/current",
            drop_in,
        )
        self.assertNotIn(
            "PYTHONPATH=/opt/streetsmart-hermes/robie-job-engine:", drop_in
        )
        self.assertIn("ROBIE_CHAT_QUEUE_LEASE_SECONDS=120", drop_in)

    def test_gateway_uses_durable_reply_links_and_lease_heartbeats(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("queue.conversation_job_for_event", adapter)
        self.assertIn("queue.active_conversation_job", adapter)
        self.assertIn("queue.renew_lease", adapter)
        self.assertIn("queue.defer", adapter)
        self.assertNotIn(
            'JobContextManager(ROBIE_JOB_DB).get(event.source.chat_id)', adapter
        )
        self.assertNotIn("JobContextManager(ROBIE_JOB_DB)", adapter)

    def test_dm_new_intent_detaches_parked_hitl_before_routing(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        classify_at = adapter.index("classify_human_reply(text, interaction)")
        detach_at = adapter.index("queue.deactivate_conversation", classify_at)
        admin_at = adapter.index("admin_response = await asyncio.to_thread", detach_at)
        resume_at = adapter.index("queue.resume_human_input", admin_at)
        self.assertLess(classify_at, detach_at)
        self.assertLess(detach_at, admin_at)
        self.assertLess(admin_at, resume_at)
        self.assertIn('source_space_type in {"DIRECT_MESSAGE", "DM"}', adapter)

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

    def test_admin_and_workspace_links_bypass_playwright_download_routing(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("handle_admin_command", adapter)
        self.assertIn("_expand_workspace_text_links", adapter)
        self.assertIn("GoogleDriveSkillSource(drive).read_file(meta)", adapter)
        self.assertLess(
            adapter.index("admin_response = await asyncio.to_thread"),
            adapter.index("job_id = await asyncio.to_thread(\n                open_chat_job"),
        )


if __name__ == "__main__":
    unittest.main()
