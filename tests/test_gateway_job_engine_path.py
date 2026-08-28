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
        self.assertIn(
            "ExecStartPost=-/home/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python "
            "-m robie_job_engine.production_preflight",
            drop_in,
        )

    def test_gateway_uses_durable_reply_links_and_lease_heartbeats(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("queue.conversation_job_for_event", adapter)
        self.assertIn("queue.active_conversation_job", adapter)
        self.assertIn("queue.renew_lease", adapter)
        self.assertIn("queue.defer", adapter)
        self.assertIn("start_generic_chat_job_heartbeat", adapter)
        self.assertIn("_run_generic_chat_job(job_id, event)", adapter)
        chat_guard = (ROOT / "robie_job_engine/chat_guard.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("start_generic_chat_job_heartbeat(db_path, current[\"id\"])", chat_guard)
        self.assertIn("start_generic_chat_job_heartbeat(db_path, job_id)", chat_guard)
        self.assertIn("maybe_audit_terminal_job", chat_guard)
        self.assertIn("_post_job_audit_note", chat_guard)
        engine = (ROOT / "robie_job_engine/engine.py").read_text(encoding="utf-8")
        self.assertIn("maybe_audit_terminal_job(self.store.path, job_id)", engine)
        post_job_audit = (
            ROOT / "robie_job_engine/post_job_audit.py"
        ).read_text(encoding="utf-8")
        production_preflight = (
            ROOT / "robie_job_engine/production_preflight.py"
        ).read_text(encoding="utf-8")
        self.assertIn("maybe_cleanup_terminal_job_tabs", post_job_audit)
        self.assertIn("maybe_flush_orphaned_tabs", production_preflight)
        self.assertIn("tab_cleanup", post_job_audit)
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

    def test_hitl_direct_resume_reopens_and_runs_generic_chat_job(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        resume_at = adapter.index("queue.resume_human_input")
        ack_at = adapter.index(
            "Resuming from the saved checkpoint.", resume_at
        )
        reopen_at = adapter.index("_resume_direct_generic_chat_job(", ack_at)
        open_at = adapter.index("open_chat_job,", reopen_at)
        run_at = adapter.index("_run_generic_chat_job(job_id, event)", reopen_at)
        self.assertLess(resume_at, ack_at)
        self.assertLess(ack_at, reopen_at)
        self.assertLess(reopen_at, open_at)
        self.assertLess(open_at, run_at)
        self.assertIn("The Chat ack is not a claim", adapter)
        chat_queue = (ROOT / "robie_job_engine/chat_queue.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_reopen_resumed_generic_chat_job(job_id)", chat_queue)
        chat_guard = (ROOT / "robie_job_engine/chat_guard.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def reopen_resumed_generic_chat_job(", chat_guard)
        self.assertIn("def notify_terminal_chat_job(", chat_guard)
        scheduler = (ROOT / "robie_job_engine/scheduler.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("notify_terminal_chat_job(db_path, orphan_id)", scheduler)

    def test_stale_terminal_hitl_bind_releases_before_resume_and_falls_through(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        release_at = adapter.index("queue.release_stale_human_input_bind")
        resume_at = adapter.index("queue.resume_human_input", release_at)
        stale_at = adapter.index("is_stale_human_input_bind_error", resume_at)
        deactivate_at = adapter.index("queue.deactivate_conversation", stale_at)
        open_at = adapter.index("open_chat_job,", deactivate_at)
        self.assertLess(release_at, resume_at)
        self.assertLess(resume_at, stale_at)
        self.assertLess(stale_at, deactivate_at)
        self.assertLess(deactivate_at, open_at)
        self.assertIn("except RuntimeError", adapter)
        chat_queue = (ROOT / "robie_job_engine/chat_queue.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def release_stale_human_input_bind(", chat_queue)
        self.assertIn("def is_stale_human_input_bind_error(", chat_queue)

    def test_pubsub_ack_settles_before_logging_handoff_errors(self):
        ack = (ROOT / "robie_job_engine/pubsub_ack.py").read_text(encoding="utf-8")
        settle_at = ack.index("self._settle(", ack.index("def done("))
        error_at = ack.index("self._invoke_on_error(on_error, error)", settle_at)
        self.assertLess(settle_at, error_at)
        self.assertIn("def _last_ditch_nack(", ack)
        self.assertIn("max_messages=1", ack)
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("Pub/Sub schedule failed", adapter)
        self.assertIn("message.nack()", adapter)

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
