"""Install the four daily verification worker schedules plus the morning digest."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from robie_job_engine.chat_admin import next_cron_time
from robie_job_engine.operations import OperationsStore


TIMEZONE = "America/New_York"
WORKER_CRON = "0 6 * * *"  # daily 6:00 AM ET — workers finish before the digest
DIGEST_CRON = "0 9 * * *"  # daily 9:00 AM ET — digest delivery lands inside Carlo's 9-10 AM ET window

# (task_name, action_type, payload, cron_spec)
SCHEDULES: tuple[tuple[str, str, dict[str, Any], str], ...] = (
    (
        "StreetSmart daily manual renewal verification",
        "manual_renewal_verification",
        {"worker": "manual-renewal", "report_id": "4247", "read_only": False},
        WORKER_CRON,
    ),
    (
        "StreetSmart daily audit verification",
        "audit_verification",
        {"worker": "audit-verification", "report_id": "4246", "read_only": False},
        WORKER_CRON,
    ),
    (
        "StreetSmart daily mortgagee verification",
        "mortgagee_verification",
        {"worker": "mortgagee-verification", "report_id": "4372", "read_only": False},
        WORKER_CRON,
    ),
    (
        "StreetSmart daily policy change verification",
        "policy_change_verification",
        {"worker": "policy-change-verification", "report_id": "4359", "read_only": False},
        WORKER_CRON,
    ),
    (
        "StreetSmart daily verification digest",
        "daily_verification_digest",
        {"worker": "verification-digest", "read_only": True},
        DIGEST_CRON,
    ),
)


def install_verification_schedules(
    db_path: str,
    *,
    timezone_name: str = TIMEZONE,
    now: datetime | None = None,
    digest_output_dir: str | None = None,
    digest_email_sender: str | None = None,
) -> list[dict[str, Any]]:
    """Install (or reconcile) the 5 verification schedules. Returns reread rows.

    The digest payload requires environment-specific ``output_dir`` (where the
    HTML digest artifact is written) and ``email_sender`` (the mailbox the
    digest is sent from); without them the digest holds at NEEDS_CLARIFICATION.
    """
    ops = OperationsStore(
        db_path,
        artifact_root=str(Path(db_path).expanduser().resolve().parent / "artifacts"),
    )
    installed: list[dict[str, Any]] = []
    for task_name, action_type, payload, cron_spec in SCHEDULES:
        payload = dict(payload)
        if action_type == "daily_verification_digest":
            if digest_output_dir:
                payload["output_dir"] = digest_output_dir
            if digest_email_sender:
                payload["email_sender"] = digest_email_sender
        expected_next = next_cron_time(cron_spec, timezone_name, now=now)
        item = ops.ensure_recurring_job(
            task_name,
            action_type,
            dict(payload),
            cron_spec,
            timezone_name,
            next_run_at=expected_next,
            target_ref=f"ezlynx:report:{payload.get('report_id')}"
            if payload.get("report_id")
            else "verification-digest",
            reconcile=True,
        )
        observed = ops.get_recurring_job(item["id"])
        if any(
            (
                observed.get("action_type") != action_type,
                observed.get("cron_spec") != cron_spec,
                observed.get("timezone") != timezone_name,
                observed.get("parameters") != payload,
                observed.get("enabled") != 1,
            )
        ):
            raise RuntimeError(f"schedule reread verification failed: {task_name}")
        installed.append(observed)
    return installed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install StreetSmart daily verification worker + digest schedules"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--timezone", default=TIMEZONE)
    parser.add_argument(
        "--digest-output-dir",
        default=os.environ.get("VERIFICATION_DIGEST_OUTPUT_DIR"),
        help="Directory for the digest HTML artifact (required for the digest schedule)",
    )
    parser.add_argument(
        "--digest-email-sender",
        default=os.environ.get("VERIFICATION_DIGEST_EMAIL_SENDER"),
        help="Mailbox the digest is sent from (required for the digest schedule)",
    )
    args = parser.parse_args()
    installed = install_verification_schedules(
        args.db,
        timezone_name=args.timezone,
        digest_output_dir=args.digest_output_dir,
        digest_email_sender=args.digest_email_sender,
    )
    print(
        json.dumps(
            [
                {
                    "id": item["id"],
                    "task_name": item["task_name"],
                    "action_type": item["action_type"],
                    "cron_spec": item["cron_spec"],
                    "timezone": item["timezone"],
                    "next_run_at": item["next_run_at"],
                    "enabled": bool(item["enabled"]),
                }
                for item in installed
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
