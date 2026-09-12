from __future__ import annotations

import os
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.ezlynx_session import (
    PROFILE_ID,
    RESOURCE_ID,
    EzlynxSessionRefreshWorker,
    EzlynxSessionVerifier,
)
from robie_job_engine.job_schema import EXECUTABLE_SKILL_CONTRACTS
from robie_job_engine.models import JobStatus
from robie_job_engine.operations import OperationsStore
from robie_job_engine.scheduler import _ensure_default_schedules, _next_daily
from robie_job_engine.scheduler import run_once
from robie_job_engine.store import JobStore
from robie_job_engine.submission_audit import BoundedProcessError
from robie_job_engine.test_runtime import build_runtime_engine


def _job() -> dict:
    return {
        "payload": {"resource_id": RESOURCE_ID, "profile_id": PROFILE_ID},
    }


class EzlynxSessionRefreshTests(unittest.TestCase):
    @patch("robie_job_engine.ezlynx_session.exclusive_session", return_value=nullcontext())
    @patch("robie_job_engine.ezlynx_session.run_login_helper", return_value="AUTHENTICATED")
    def test_worker_refreshes_gmail_and_ezlynx_without_exposing_secrets(self, helper, lock):
        result = EzlynxSessionRefreshWorker().perform(_job(), idempotency_key="morning")
        self.assertTrue(result.succeeded)
        self.assertEqual(result.destination["resource_id"], RESOURCE_ID)
        self.assertTrue(result.destination["gmail_identity_verified"])
        self.assertNotIn("password", repr(result).casefold())
        self.assertNotIn("cookie", repr(result).casefold())
        helper.assert_called_once_with(verify_only=False)

    @patch("robie_job_engine.ezlynx_session.exclusive_session", return_value=nullcontext())
    @patch("robie_job_engine.ezlynx_session.run_login_helper", return_value="AUTHENTICATED")
    def test_verifier_performs_fresh_verify_only_readback(self, helper, lock):
        result = EzlynxSessionVerifier().verify(_job(), {})
        self.assertTrue(result.verified)
        self.assertTrue(result.evidence.authoritative)
        self.assertEqual(result.evidence.locator, RESOURCE_ID)
        helper.assert_called_once_with(verify_only=True)

    @patch("robie_job_engine.ezlynx_session.exclusive_session", return_value=nullcontext())
    @patch(
        "robie_job_engine.ezlynx_session.run_login_helper",
        side_effect=BoundedProcessError("MFA_CODE_NOT_FOUND"),
    )
    def test_mfa_failure_is_needs_auth_not_complete(self, helper, lock):
        result = EzlynxSessionRefreshWorker().perform(_job(), idempotency_key="morning")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.hold_status, JobStatus.NEEDS_AUTH)

    def test_refresh_contract_has_sensitive_recording_exemption(self):
        contract = EXECUTABLE_SKILL_CONTRACTS["ezlynx.session_refresh"]
        self.assertEqual(contract.recording_policy, "EXEMPT")
        self.assertEqual(contract.independent_verifier, "EzlynxSessionVerifier")

    def test_daily_schedule_is_persisted_for_530_eastern(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            with patch.dict(
                os.environ,
                {
                    "ROBIE_ENABLE_EZLYNX_SESSION_REFRESH": "1",
                    "ROBIE_EZLYNX_SESSION_REFRESH_LOCAL_TIME": "05:30",
                    "ROBIE_EZLYNX_SESSION_REFRESH_TIMEZONE": "America/New_York",
                    "ROBIE_ARTIFACT_ROOT": str(root / "artifacts"),
                },
                clear=False,
            ):
                ops = OperationsStore(str(root / "jobs.db"))
                _ensure_default_schedules(ops)
                with ops._connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM schedules WHERE action_type=?",
                        ("ezlynx.session_refresh",),
                    ).fetchone()
                self.assertIsNotNone(row)
                schedule = ops.get_schedule(row["id"])
                self.assertEqual(schedule["payload"]["_daily_local_time"], "05:30")
                self.assertEqual(schedule["payload"]["worker"], "session-refresh")

    def test_530_eastern_schedule_tracks_dst_in_utc(self):
        winter = _next_daily(
            "05:30",
            "America/New_York",
            now=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        )
        summer = _next_daily(
            "05:30",
            "America/New_York",
            now=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(winter.endswith("10:30:00+00:00"))
        self.assertTrue(summer.endswith("09:30:00+00:00"))

    def test_existing_schedule_is_reconciled_to_530_eastern(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            with patch.dict(
                os.environ,
                {
                    "ROBIE_ENABLE_EZLYNX_SESSION_REFRESH": "1",
                    "ROBIE_EZLYNX_SESSION_REFRESH_LOCAL_TIME": "05:30",
                    "ROBIE_EZLYNX_SESSION_REFRESH_TIMEZONE": "America/New_York",
                    "ROBIE_ARTIFACT_ROOT": str(root / "artifacts"),
                },
                clear=False,
            ):
                ops = OperationsStore(str(root / "jobs.db"))
                original = ops.create_schedule(
                    "Daily EZLynx and Gmail session refresh",
                    "ezlynx.session_refresh",
                    {"_daily_local_time": "06:00", "_schedule_timezone": "UTC"},
                    1440,
                    next_run_at="2099-01-01T06:00:00+00:00",
                )
                _ensure_default_schedules(ops)
            updated = ops.get_schedule(original["id"])
            self.assertEqual(updated["payload"]["_daily_local_time"], "05:30")
            self.assertEqual(
                updated["payload"]["_schedule_timezone"], "America/New_York"
            )
            self.assertNotEqual(updated["next_run_at"], "2099-01-01T06:00:00+00:00")

    def test_browser_service_uses_one_canonical_persistent_profile(self):
        service = (Path(__file__).resolve().parents[1] / "deploy/systemd/robie-ezlynx-browser.service").read_text()
        self.assertIn("--remote-debugging-address=127.0.0.1", service)
        self.assertIn("--remote-debugging-port=9222", service)
        self.assertIn("--user-data-dir=/opt/streetsmart-hermes/.hermes/browser-profiles/ezlynx", service)
        self.assertIn("StartLimitBurst=", service)
        self.assertIn("StartLimitIntervalSec=", service)
        self.assertIn("Do not add a daily Chrome restart", service)
        self.assertIn("monitor-ezlynx-session.yml", service)

    def test_scheduler_unit_does_not_enable_session_refresh_owner(self):
        root = Path(__file__).resolve().parents[1] / "deploy/systemd"
        scheduler = (root / "robie-scheduler.service").read_text()
        self.assertNotIn("Environment=ROBIE_ENABLE_EZLYNX_SESSION_REFRESH=1", scheduler)
        self.assertNotIn("Environment=ROBIE_EZLYNX_SESSION_REFRESH_LOCAL_TIME=", scheduler)
        self.assertNotIn("Environment=ROBIE_EZLYNX_SESSION_REFRESH_TIMEZONE=", scheduler)
        self.assertIn("monitor-ezlynx-session.yml", scheduler)

    def test_retired_session_owner_units_are_absent(self):
        root = Path(__file__).resolve().parents[1] / "deploy/systemd"
        self.assertFalse((root / "robie-chrome-refresh.timer").exists())
        self.assertFalse((root / "robie-chrome-refresh.service").exists())
        self.assertFalse((root / "robie-ezlynx-session.timer").exists())
        self.assertFalse((root / "robie-ezlynx-session.service").exists())

    def test_session_refresh_enqueue_defaults_off(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            env = {
                key: value
                for key, value in os.environ.items()
                if key != "ROBIE_ENABLE_EZLYNX_SESSION_REFRESH"
            }
            env["ROBIE_ARTIFACT_ROOT"] = str(root / "artifacts")
            with patch.dict(os.environ, env, clear=True):
                ops = OperationsStore(str(root / "jobs.db"))
                _ensure_default_schedules(ops)
                with ops._connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM schedules WHERE action_type=?",
                        ("ezlynx.session_refresh",),
                    ).fetchone()
                    recurring = conn.execute(
                        "SELECT * FROM scheduled_jobs WHERE action_type=?",
                        ("ezlynx.session_refresh",),
                    ).fetchone()
            self.assertIsNone(row)
            self.assertIsNone(recurring)

    def test_gateway_and_scheduler_pin_helper_to_immutable_current_release(self):
        root = Path(__file__).resolve().parents[1] / "deploy/systemd"
        gateway = (root / "zz-hermes-gateway-job-engine-path.conf").read_text()
        scheduler = (root / "robie-scheduler.service").read_text()
        expected = (
            "ROBIE_EZLYNX_LOGIN_HELPER=/opt/streetsmart-hermes/releases/current/"
            "ezlynx_login_bootstrap.py"
        )
        self.assertIn(expected, gateway)
        self.assertIn(expected, scheduler)
        self.assertIn(
            "WorkingDirectory=/opt/streetsmart-hermes/releases/current", scheduler
        )
        self.assertIn(
            "Environment=PYTHONPATH=/opt/streetsmart-hermes/releases/current", scheduler
        )
        self.assertNotIn(
            "Environment=PYTHONPATH=/opt/streetsmart-hermes/robie-job-engine", scheduler
        )

    def test_refresh_completes_only_after_independent_verify_only_readback(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "ezlynx.session_refresh",
                {
                    "worker": "session-refresh",
                    "resource_id": RESOURCE_ID,
                    "profile_id": PROFILE_ID,
                    "perform_timeout_seconds": 5,
                },
            )
            with patch.dict(
                os.environ,
                {"ROBIE_EZLYNX_SESSION_LOCK": str(root / "session.lock")},
                clear=False,
            ), patch(
                "robie_job_engine.ezlynx_session.run_login_helper",
                return_value="AUTHENTICATED",
            ) as helper:
                final = build_runtime_engine(store).run(job["id"])
            self.assertEqual(final["status"], JobStatus.COMPLETE)
            self.assertEqual(
                [call.kwargs["verify_only"] for call in helper.call_args_list],
                [False, True],
            )
            exemption = store.get_checkpoint(job["id"], "recording_exemption")
            self.assertIn("MFA", exemption["reason"])
            self.assertTrue(store.list_evidence(job["id"])[0]["verified"])

    def test_pending_refresh_survives_reconstruction_and_is_picked_up_once(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            first = JobStore(db)
            job = first.create_job(
                "ezlynx.session_refresh",
                {"worker": "session-refresh", "resource_id": RESOURCE_ID, "profile_id": PROFILE_ID},
                idempotency_key="daily:2026-08-26",
            )
            second = JobStore(db)
            self.assertEqual(
                second.list_pending(frozenset({"ezlynx.session_refresh"})),
                [job["id"]],
            )
            same = second.create_job(
                "ezlynx.session_refresh",
                {"worker": "session-refresh", "resource_id": RESOURCE_ID, "profile_id": PROFILE_ID},
                idempotency_key="daily:2026-08-26",
            )
            self.assertEqual(same["id"], job["id"])

    def test_scheduler_hands_pending_refresh_to_bounded_engine(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            job = JobStore(db).create_job(
                "ezlynx.session_refresh",
                {"worker": "session-refresh", "resource_id": RESOURCE_ID, "profile_id": PROFILE_ID},
            )
            with patch.dict(
                os.environ,
                {
                    "ROBIE_ENABLE_EZLYNX_SESSION_REFRESH": "0",
                    "ROBIE_ARTIFACT_ROOT": str(root / "artifacts"),
                },
                clear=False,
            ), patch(
                "robie_job_engine.test_runtime.maybe_run_bounded_job", return_value=True
            ) as run:
                result = run_once(db)
            self.assertEqual(result["executed"], 1)
            run.assert_called_once_with(db, job["id"])


if __name__ == "__main__":
    unittest.main()
