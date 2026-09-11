"""Scheduler OnFailure Chat alert: Robie space + local fallback."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_app_post import DEFAULT_CHAT_SPACE
from robie_job_engine.scheduler_alert import (
    DEFAULT_FAILED_UNIT,
    format_alert,
    journalctl_hint,
    main,
    run_scheduler_alert,
)


ROOT = Path(__file__).resolve().parents[1]
ALERT_UNIT = (
    ROOT / "deploy/systemd/robie-scheduler-alert@.service"
).read_text(encoding="utf-8")
SCHEDULER_UNIT = (
    ROOT / "deploy/systemd/robie-scheduler.service"
).read_text(encoding="utf-8")
DROP_IN = (
    ROOT / "deploy/systemd/robie-scheduler.service.d/on-failure-chat.conf"
).read_text(encoding="utf-8")
MIRRORED_ALERT_UNIT = (
    ROOT / "systemd/robie-scheduler-alert@.service"
).read_text(encoding="utf-8")


class FormatTests(unittest.TestCase):
    def test_alert_text_is_dry_critical_with_journalctl_hint(self):
        text = format_alert(
            "robie-scheduler.service",
            now=datetime(2026, 9, 11, 14, 30, 0, tzinfo=timezone.utc),
        )
        self.assertIn("CRITICAL: robie-scheduler.service failed", text)
        self.assertIn("utc: 2026-09-11T14:30:00Z", text)
        self.assertIn("2026-09-11 10:30:00 EDT", text)
        self.assertEqual(journalctl_hint("robie-scheduler.service"), "journalctl -u robie-scheduler -n 50")
        self.assertIn("hint: journalctl -u robie-scheduler -n 50", text)
        self.assertNotIn("@robie", text.casefold())


class NotifyTests(unittest.TestCase):
    def _dm_finder(self, email: str) -> str:
        return {
            "carlo@streetsmart.insurance": "spaces/dm-carlo",
            "jake@streetsmart.insurance": "spaces/dm-jake",
        }[email]

    def test_posts_critical_text_to_robie_space(self):
        posted: list[tuple[str, str]] = []

        def fake_post(space: str, text: str, **_kwargs):
            posted.append((space, text))
            return {"name": "messages/1"}

        with durable_temporary_directory() as tmp, patch(
            "robie_job_engine.chat_app_post.post_as_chat_app",
            side_effect=fake_post,
        ), patch(
            "robie_job_engine.chat_app_post.find_direct_message_space",
            side_effect=self._dm_finder,
        ):
            log_path = Path(tmp) / "scheduler-alert.log"
            result = run_scheduler_alert(
                "robie-scheduler.service",
                now=datetime(2026, 9, 11, 14, 30, 0, tzinfo=timezone.utc),
                log_path=log_path,
                logger_argv=["true"],
            )
            written = log_path.read_text()

        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["chat_posted"])
        self.assertIsNone(result["chat_error"])
        self.assertEqual(posted[0][0], DEFAULT_CHAT_SPACE)
        self.assertEqual(
            [space for space, _text in posted],
            [DEFAULT_CHAT_SPACE, "spaces/dm-carlo", "spaces/dm-jake"],
        )
        text = posted[0][1]
        self.assertIn("CRITICAL: robie-scheduler.service failed", text)
        self.assertIn("utc: 2026-09-11T14:30:00Z", text)
        self.assertIn("hint: journalctl -u robie-scheduler -n 50", text)
        self.assertNotIn("@robie", text.casefold())
        self.assertIn("CRITICAL: robie-scheduler.service failed", written)

    def test_chat_failure_writes_local_log_and_exits_zero(self):
        with durable_temporary_directory() as tmp, patch(
            "robie_job_engine.chat_app_post.post_as_chat_app",
            side_effect=RuntimeError("chat down"),
        ), patch(
            "robie_job_engine.chat_app_post.find_direct_message_space",
            side_effect=AssertionError("DM must not run after space post fails"),
        ):
            log_path = Path(tmp) / "scheduler-alert.log"
            result = run_scheduler_alert(
                DEFAULT_FAILED_UNIT,
                now=datetime(2026, 9, 11, 14, 30, 0, tzinfo=timezone.utc),
                log_path=log_path,
                logger_argv=["true"],
            )
            written = log_path.read_text()
            exit_code = main(["robie-scheduler.service", "--log", str(log_path)])

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(exit_code, 0)
        self.assertFalse(result["chat_posted"])
        self.assertIn("chat down", result["chat_error"] or "")
        self.assertIn("CRITICAL: robie-scheduler.service failed", written)
        self.assertIn("chat_post_failed:", written)


class UnitFileTests(unittest.TestCase):
    def test_alert_unit_calls_python_module_and_pins_robie_space(self):
        self.assertIn("Type=oneshot", ALERT_UNIT)
        self.assertIn(
            "ExecStart=/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python "
            "-m robie_job_engine.scheduler_alert %i",
            ALERT_UNIT,
        )
        self.assertIn("ROBIE_PREFLIGHT_CHAT_SPACE=spaces/AAQAZbLJO78", ALERT_UNIT)
        self.assertIn(
            "ROBIE_PREFLIGHT_FAIL_NOTIFY=carlo@streetsmart.insurance,"
            "jake@streetsmart.insurance",
            ALERT_UNIT,
        )
        self.assertIn(
            "ROBIE_SCHEDULER_ALERT_LOG=/opt/streetsmart-hermes/robie-job-engine/data/scheduler-alert.log",
            ALERT_UNIT,
        )
        self.assertNotIn("OnFailure=", ALERT_UNIT)
        self.assertNotIn("@robie", ALERT_UNIT)
        self.assertEqual(ALERT_UNIT, MIRRORED_ALERT_UNIT)

    def test_scheduler_unit_and_drop_in_wire_onfailure_template(self):
        self.assertIn("OnFailure=robie-scheduler-alert@%n.service", SCHEDULER_UNIT)
        self.assertIn("OnFailure=robie-scheduler-alert@%n.service", DROP_IN)
        self.assertNotIn("@robie", DROP_IN)


if __name__ == "__main__":
    unittest.main()
