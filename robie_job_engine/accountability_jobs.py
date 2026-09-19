"""Durable, read-only accountability report jobs and verification."""

from __future__ import annotations

import hashlib
import csv
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .accountability_cli import main as build_report
from .business_calendar import previous_business_day
from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult


ACTION_TO_MODE = {
    "accountability.daily": "daily",
    "accountability.weekly": "weekly",
    "accountability.monthly": "monthly",
}

SOURCE_FLAGS = {
    "ringcentral": "--ringcentral",
    "tasks": "--tasks",
    "activities": "--activities",
    "sales": "--sales",
    "sales_json": "--sales-json",
    "retention": "--retention",
    "retention_summary_json": "--retention-summary-json",
    "submissions": "--submissions",
    "submissions_json": "--submissions-json",
    "email_json": "--email-json",
    "appsheet_json": "--appsheet-json",
    "magellan_json": "--magellan-json",
    "roles_json": "--roles-json",
    "monthly_kpis_json": "--monthly-kpis-json",
    "churn_json": "--churn-json",
}


def _previous_business_day(value: datetime, *, holiday_calendar: str | None = None) -> date:
    eastern = value.astimezone(ZoneInfo("America/New_York")).date()
    return previous_business_day(eastern, holiday_calendar=holiday_calendar)


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("accountability manifest must be a JSON object")
    return data


class AccountabilityReportWorker:
    """Build one report from a manifest of explicit evidence sources."""

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        action = str(job.get("action_type") or "")
        mode = ACTION_TO_MODE.get(action)
        payload = dict(job.get("payload") or {})
        manifest_value = payload.get("manifest_path")
        if mode is None or not manifest_value:
            return WorkerResult(
                False,
                action,
                {},
                retryable=False,
                error="accountability job requires a supported action and manifest_path",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        manifest_path = Path(str(manifest_value)).expanduser().resolve()
        if not manifest_path.exists():
            return WorkerResult(
                False,
                action,
                {"manifest_path": str(manifest_path)},
                retryable=True,
                error="accountability connection manifest is unavailable",
                hold_status=JobStatus.NEEDS_AUTH,
            )
        try:
            manifest = _manifest(manifest_path)
            reports_5 = dict((manifest.get("collection") or {}).get("ezlynx_reports_5") or {})
            if reports_5:
                from .ezlynx_reports_5 import validate_reports_5_config

                validate_reports_5_config(reports_5)
            output_dir = Path(str(manifest.get("output_dir") or manifest_path.parent / "reports")).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            run_at = datetime.now(timezone.utc)
            holiday_calendar = (manifest.get("rules") or {}).get("holiday_calendar")
            report_date = _previous_business_day(run_at, holiday_calendar=holiday_calendar) if mode == "daily" else None
            output = output_dir / f"streetsmart-{mode}-{run_at:%Y%m%dT%H%M%SZ}.md"
            arguments = [mode, "--as-of", run_at.isoformat(), "--output", str(output)]
            if report_date is not None:
                arguments.extend(["--report-date", report_date.isoformat()])
            sources = dict(manifest.get("sources") or {})
            sources["trackers"] = dict(sources.get("trackers") or {})
            collection = dict(manifest.get("collection") or {})
            scheduled_reports = dict(collection.get("scheduled_reports_email") or {})
            if scheduled_reports.get("enabled"):
                from .ringcentral_email_sync import build_keyless_report_mailbox_service
                from .scheduled_report_email_sync import collect_scheduled_tabular_reports

                mailbox = str(
                    scheduled_reports.get("mailbox")
                    or os.environ.get("ACCOUNTABILITY_REPORT_MAILBOX", "")
                ).strip()
                service_account = os.environ.get(
                    "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", ""
                ).strip()
                if not mailbox or not service_account:
                    raise RuntimeError(
                        "scheduled report collection requires the Robie report mailbox "
                        "and delegated service account"
                    )
                collected = collect_scheduled_tabular_reports(
                    build_keyless_report_mailbox_service(service_account, mailbox),
                    output_dir=output_dir / "scheduled-reports",
                    config=scheduled_reports,
                    as_of=run_at,
                )
                for key, value in dict(collected.get("sources") or {}).items():
                    if key in sources and sources[key] and Path(str(sources[key])).expanduser().is_file():
                        raise RuntimeError(
                            f"scheduled report source {key} conflicts with a configured file source"
                        )
                    sources[key] = str(value)
                for key, value in dict(collected.get("trackers") or {}).items():
                    if key in sources["trackers"] and Path(
                        str(sources["trackers"][key])
                    ).expanduser().is_file():
                        raise RuntimeError(
                            f"scheduled report tracker {key} conflicts with a configured tracker"
                        )
                    sources["trackers"][key] = str(value)
            google_sheets = dict(manifest.get("google_sheets") or {})
            if google_sheets.get("enabled"):
                from .google_sheets_accountability import collect_allowlisted_tables, role_registry_from_snapshot

                sheet_snapshot = collect_allowlisted_tables(google_sheets, as_of=run_at, holiday_calendar=holiday_calendar)
                sheet_path = output_dir / f"appsheet-backing-sheet-{run_at:%Y%m%dT%H%M%SZ}.json"
                sheet_path.write_text(json.dumps(sheet_snapshot, indent=2, default=str), encoding="utf-8")
                if mode == "weekly" and not (manifest.get("sources") or {}).get("appsheet_json"):
                    arguments.extend(["--appsheet-json", str(sheet_path)])
                configured_roles = sources.get("roles_json")
                roles_available = bool(configured_roles and Path(str(configured_roles)).expanduser().is_file())
                if not roles_available:
                    roles = role_registry_from_snapshot(sheet_snapshot, google_sheets)
                    roles_path = output_dir / f"employee-roles-{run_at:%Y%m%dT%H%M%SZ}.json"
                    roles_path.write_text(json.dumps(roles, indent=2, default=str), encoding="utf-8")
                    sources["roles_json"] = str(roles_path)
            google_sheet_trackers = dict(manifest.get("google_sheet_trackers") or {})
            if google_sheet_trackers.get("enabled"):
                from .google_sheets_accountability import collect_allowlisted_tables, write_allowlisted_table_csv

                for tracker_key, raw_tracker in sorted(dict(google_sheet_trackers.get("trackers") or {}).items()):
                    tracker = dict(raw_tracker or {})
                    snapshot = collect_allowlisted_tables(
                        {
                            "spreadsheet_id": tracker.get("spreadsheet_id"),
                            "tables": {
                                tracker_key: {
                                    "range": tracker.get("range"),
                                    "allowed_columns": tracker.get("allowed_columns"),
                                }
                            },
                        },
                        as_of=run_at,
                        holiday_calendar=holiday_calendar,
                    )
                    tracker_path = output_dir / f"google-sheet-{tracker_key}-{run_at:%Y%m%dT%H%M%SZ}.csv"
                    write_allowlisted_table_csv(snapshot, tracker_key, tracker_path)
                    sources["trackers"][tracker_key] = str(tracker_path)
            ringcentral_email = dict(collection.get("ringcentral_email") or {})
            ringcentral_api = dict(collection.get("ringcentral_api") or {})
            if mode == "daily" and ringcentral_api.get("enabled"):
                if report_date is None:
                    raise RuntimeError("RingCentral daily API collection requires a resolved report date")
                from .ringcentral_accountability_api import collect_ringcentral_api_evidence
                from .ringcentral_client import RingCentralClient

                roster_config = {**ringcentral_email, **ringcentral_api}
                required_users = [
                    str(value).strip()
                    for value in roster_config.get("required_users", [])
                    if str(value).strip()
                ]
                if not required_users:
                    role_source = Path(str(sources.get("roles_json") or "")).expanduser()
                    if role_source.is_file():
                        role_data = _manifest(role_source)
                        required_users = [
                            str(value).strip()
                            for value in (role_data.get("employees") or {})
                            if str(value).strip()
                        ]
                excluded_users = {
                    str(value).strip().casefold()
                    for value in roster_config.get("excluded_users", [])
                }
                required_users = [
                    value for value in required_users
                    if value.casefold() not in excluded_users
                ]
                required_queues = [
                    str(value).strip()
                    for value in roster_config.get("required_queues", [])
                    if str(value).strip()
                ]
                required_queue_members = {
                    str(queue).strip(): [
                        str(member).strip()
                        for member in members
                        if str(member).strip()
                    ]
                    for queue, members in dict(
                        roster_config.get("required_queue_members") or {}
                    ).items()
                }
                api_path = output_dir / (
                    f"ringcentral-api-{report_date.isoformat()}-"
                    f"{run_at:%Y%m%dT%H%M%SZ}.json"
                )
                sources["ringcentral"] = str(
                    collect_ringcentral_api_evidence(
                        RingCentralClient.from_env(),
                        output_path=api_path,
                        report_kind="daily",
                        target_date=report_date,
                        required_users=required_users,
                        required_queues=required_queues,
                        required_queue_members=required_queue_members,
                    )
                )
            if ringcentral_email.get("enabled") and not sources.get("ringcentral"):
                from .ringcentral_email_sync import collect_scheduled_ringcentral_report

                mailbox = str(ringcentral_email.get("mailbox") or os.environ.get("ACCOUNTABILITY_REPORT_MAILBOX", "")).strip()
                service_account = os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
                required_users = [str(value).strip() for value in ringcentral_email.get("required_users", []) if str(value).strip()]
                if not required_users:
                    role_source = Path(str(sources.get("roles_json") or "")).expanduser()
                    if role_source.is_file():
                        role_data = _manifest(role_source)
                        required_users = [str(value).strip() for value in (role_data.get("employees") or {}) if str(value).strip()]
                excluded_users = {str(value).strip().casefold() for value in ringcentral_email.get("excluded_users", [])}
                required_users = [value for value in required_users if value.casefold() not in excluded_users]
                required_queues = [str(value).strip() for value in ringcentral_email.get("required_queues", []) if str(value).strip()]
                required_queue_members = {
                    str(queue).strip(): [str(member).strip() for member in members if str(member).strip()]
                    for queue, members in dict(ringcentral_email.get("required_queue_members") or {}).items()
                }
                configured_sheets = dict(ringcentral_email.get("required_sheets") or {})
                sources["ringcentral"] = str(collect_scheduled_ringcentral_report(
                    service_account_email=service_account,
                    mailbox=mailbox,
                    output_dir=output_dir / "ringcentral",
                    report_kind="daily" if mode == "daily" else "weekly",
                    required_users=required_users,
                    required_queues=required_queues,
                    required_queue_members=required_queue_members,
                    required_sheets=configured_sheets.get(mode),
                    max_age_hours=int(ringcentral_email.get("max_age_hours") or (96 if mode == "daily" else 36)),
                    target_date=report_date,
                ))
            submission_center = dict(collection.get("ezlynx_submission_center") or {})
            submission_modes = {
                str(value).strip().casefold()
                for value in submission_center.get("modes", ["weekly"])
            }
            if mode in submission_modes and submission_center.get("enabled"):
                from .submission_audit import DEFAULT_SCOPE, EzlynxSubmissionAuditWorker

                audit = EzlynxSubmissionAuditWorker().perform(
                    {
                        "payload": {
                            "read_only": True,
                            "scope": DEFAULT_SCOPE,
                            "expected_postcondition": {
                                "source_status": "available",
                                "status_aria_sort": "ascending",
                                "first_row_non_closed": True,
                                "first_closed_row_inspected": True,
                                "day_31_qualifies": True,
                                "email_delivery_enabled": False,
                            },
                        }
                    },
                    idempotency_key=f"{idempotency_key}:submission-center",
                )
                if not audit.succeeded:
                    return WorkerResult(
                        False,
                        action,
                        {},
                        retryable=audit.retryable,
                        error=f"Submission Center evidence unavailable: {audit.error}",
                        hold_status=audit.hold_status,
                    )
                submission_snapshot = dict(audit.destination.get("expected_postcondition") or {})
                submission_path = output_dir / f"submission-center-{run_at:%Y%m%dT%H%M%SZ}.json"
                submission_path.write_text(
                    json.dumps(submission_snapshot, indent=2, default=str),
                    encoding="utf-8",
                )
                sources["submissions_json"] = str(submission_path)
            magellan = dict(collection.get("magellan") or {})
            if mode == "daily" and magellan.get("enabled"):
                if report_date is None:
                    raise RuntimeError("Magellan daily collection requires a resolved report date")
                from .magellan_collection import collect_magellan_snapshot

                magellan_path = output_dir / f"magellan-{report_date.isoformat()}-{run_at:%Y%m%dT%H%M%SZ}.json"
                sources["magellan_json"] = str(
                    collect_magellan_snapshot(
                        target_date=report_date,
                        output_path=magellan_path,
                        cdp_url=str(magellan.get("cdp_url") or "http://127.0.0.1:9223"),
                        project=str(magellan.get("project") or "streetsmart-hermes-poc"),
                        max_pages=max(1, int(magellan.get("max_pages") or 40)),
                    )
                )
            for key, flag in SOURCE_FLAGS.items():
                value = sources.get(key)
                if value:
                    arguments.extend([flag, str(Path(str(value)).expanduser().resolve())])
            if mode in {"daily", "weekly"} and not sources.get("email_json"):
                from .gmail_accountability import (
                    approved_mailboxes_from_role_registry,
                    collect_agency_summary,
                )

                gmail_config = dict(collection.get("gmail_accountability") or {})
                approved_users = None
                if gmail_config.get("enabled"):
                    role_source = Path(str(sources.get("roles_json") or "")).expanduser()
                    if not role_source.is_file():
                        raise RuntimeError(
                            "Gmail accountability requires a current approved active employee roster"
                        )
                    approved_users = approved_mailboxes_from_role_registry(
                        _manifest(role_source),
                        approved_domain=str(
                            gmail_config.get("approved_domain") or "streetsmart.insurance"
                        ),
                    )
                gmail_snapshot = collect_agency_summary(
                    environment=os.environ,
                    approved_users=approved_users,
                )
                gmail_path = output_dir / f"gmail-{run_at:%Y%m%dT%H%M%SZ}.json"
                gmail_path.write_text(json.dumps(gmail_snapshot, indent=2, default=str), encoding="utf-8")
                arguments.extend(["--email-json", str(gmail_path)])
            sales_untouched_days = (manifest.get("rules") or {}).get("sales_untouched_days")
            if sales_untouched_days is not None:
                arguments.extend(["--sales-untouched-days", str(max(1, int(sales_untouched_days)))])
            for key, value in sorted(dict(sources.get("trackers") or {}).items()):
                arguments.extend(["--tracker", f"{key}={Path(str(value)).expanduser().resolve()}"])
            appsheet = dict(manifest.get("appsheet") or {})
            if mode == "weekly" and appsheet.get("enabled"):
                from .appsheet_client import AppSheetConfig, AppSheetReadClient

                config = AppSheetConfig.from_env()
                snapshot: dict[str, Any]
                if config is None:
                    snapshot = {"source_status": "missing AppSheet app ID or application access key"}
                else:
                    tables = {}
                    client = AppSheetReadClient(config)
                    for label, table_name in sorted(dict(appsheet.get("tables") or {}).items()):
                        rows = client.find_rows(str(table_name))
                        tables[str(label)] = {"table_name": str(table_name), "row_count": len(rows), "rows": rows}
                    snapshot = {
                        "source_status": "available",
                        "total_rows": sum(item["row_count"] for item in tables.values()),
                        "tables": tables,
                    }
                snapshot_path = output_dir / f"appsheet-{run_at:%Y%m%dT%H%M%SZ}.json"
                snapshot_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
                arguments.extend(["--appsheet-json", str(snapshot_path)])
            exit_code = build_report(arguments)
            if exit_code != 0 or not output.exists():
                raise RuntimeError(f"report builder exited {exit_code}")
            report_content = output.read_text(encoding="utf-8", errors="replace")
            require_complete = bool((manifest.get("rules") or {}).get("require_complete_evidence", True))
            if require_complete and "⚠️ *DATA LIMITATIONS*" in report_content:
                return WorkerResult(
                    False,
                    action,
                    {"artifact_path": str(output)},
                    {"sha256": _checksum(output), "mode": mode, "manifest_path": str(manifest_path)},
                    retryable=True,
                    error="accountability report contains missing, stale, partial, or unreconciled required evidence",
                )
            dashboard_receipt: dict[str, Any] = {}
            dashboard = dict(manifest.get("dashboard") or {})
            if mode == "weekly" and dashboard.get("enabled"):
                from .accountability_dashboard import publish_weekly_dashboard
                dashboard_receipt = publish_weekly_dashboard(output, run_at=run_at, config=dashboard)
        except Exception as exc:
            return WorkerResult(
                False,
                action,
                {"manifest_path": str(manifest_path)},
                retryable=True,
                error=f"accountability report generation failed: {type(exc).__name__}: {exc}",
            )
        receipts: list[dict[str, Any]] = []
        delivery = dict(manifest.get("delivery") or {})
        if delivery.get("enabled"):
            try:
                from .accountability_delivery import deliver_report

                receipts = deliver_report(output, mode=mode, delivery=delivery, environment=os.environ)
            except Exception as exc:
                return WorkerResult(
                    False,
                    action,
                    {"artifact_path": str(output)},
                    {"sha256": _checksum(output), "delivery_receipts": receipts},
                    retryable=True,
                    error=f"accountability delivery failed: {type(exc).__name__}: {exc}",
                )
        return WorkerResult(
            True,
            action,
            {"artifact_path": str(output), "delivery_receipts": receipts, "dashboard_receipt": dashboard_receipt},
            {
                "sha256": _checksum(output),
                "mode": mode,
                "dashboard_receipt": dashboard_receipt,
                "manifest_path": str(manifest_path),
                "idempotency_key": idempotency_key,
            },
            retryable=False,
        )


class AccountabilityReportVerifier:
    """Independently read the report artifact and verify its checksum/mode."""

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = dict(action.get("destination") or {})
        detail = dict(action.get("detail") or {})
        path = Path(str(destination.get("artifact_path") or ""))
        mode = ACTION_TO_MODE.get(str(job.get("action_type") or ""), "")
        expected_title = {
            "daily": "STREETSMART DAILY SERVICE & PHONE WATCHDOG",
            "weekly": "STREETSMART WEEKLY EXECUTIVE PERFORMANCE SCORECARD",
            "monthly": "STREETSMART MONTHLY EXECUTIVE AUDIT",
        }.get(mode, "")
        observed: dict[str, Any] = {"exists": path.is_file()}
        verified = False
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            observed.update({
                "sha256": _checksum(path),
                "contains_expected_title": expected_title in content,
                "simulation_marker": "SIMULATION ONLY" in content,
            })
            verified = (
                observed["sha256"] == detail.get("sha256")
                and observed["contains_expected_title"]
                and not observed["simulation_marker"]
            )
        receipts = list(destination.get("delivery_receipts") or [])
        dashboard_receipt = dict(destination.get("dashboard_receipt") or {})
        if verified and dashboard_receipt:
            try:
                from .accountability_dashboard import verify_weekly_dashboard
                dashboard_verified, dashboard_observed = verify_weekly_dashboard(dashboard_receipt)
            except Exception as exc:
                dashboard_verified, dashboard_observed = False, {"error": f"{type(exc).__name__}: {exc}"}
            observed["dashboard"] = dashboard_observed
            verified = verified and dashboard_verified
        if verified and receipts:
            try:
                from .accountability_delivery import verify_delivery_receipts

                delivery_verified, delivery_observed = verify_delivery_receipts(receipts)
            except Exception as exc:
                delivery_verified, delivery_observed = False, [{"error": f"{type(exc).__name__}: {exc}"}]
            observed["delivery"] = delivery_observed
            verified = verified and delivery_verified
        evidence = VerificationEvidence(
            method="FRESH_DESTINATION_AND_FILESYSTEM_READBACK" if (receipts or dashboard_receipt) else "FRESH_FILESYSTEM_READBACK",
            source="accountability-report-and-destinations" if (receipts or dashboard_receipt) else "accountability-report-artifact",
            expected={"sha256": detail.get("sha256"), "mode": mode, "simulation_marker": False},
            observed=observed,
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=str(path),
        )
        return VerificationResult(
            verified,
            evidence,
            retryable=False,
            error=None if verified else "generated accountability report failed read-back verification",
        )
