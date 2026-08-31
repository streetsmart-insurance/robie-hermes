"""Durable, read-only accountability report jobs and verification."""

from __future__ import annotations

import hashlib
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .accountability_cli import main as build_report
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
    "email_json": "--email-json",
    "appsheet_json": "--appsheet-json",
    "magellan_json": "--magellan-json",
    "roles_json": "--roles-json",
    "monthly_kpis_json": "--monthly-kpis-json",
    "churn_json": "--churn-json",
}


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
            output_dir = Path(str(manifest.get("output_dir") or manifest_path.parent / "reports")).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            run_at = datetime.now(timezone.utc)
            output = output_dir / f"streetsmart-{mode}-{run_at:%Y%m%dT%H%M%SZ}.md"
            arguments = [mode, "--as-of", run_at.isoformat(), "--output", str(output)]
            sources = dict(manifest.get("sources") or {})
            collection = dict(manifest.get("collection") or {})
            source_evidence_manifests: list[str] = []
            google_sheets = dict(manifest.get("google_sheets") or {})
            if google_sheets.get("enabled"):
                from .google_sheets_accountability import collect_allowlisted_tables, role_registry_from_snapshot

                sheet_snapshot = collect_allowlisted_tables(google_sheets)
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
            ringcentral_email = dict(collection.get("ringcentral_email") or {})
            if ringcentral_email.get("enabled"):
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
                    max_age_hours=int(ringcentral_email.get("max_age_hours") or 36),
                ))
            scheduled_reports = dict(collection.get("scheduled_reports_email") or {})
            collected_trackers: dict[str, str] = {}
            if scheduled_reports.get("enabled") and mode in set(scheduled_reports.get("modes") or ["weekly"]):
                from .ringcentral_email_sync import build_keyless_report_mailbox_service
                from .scheduled_report_email_sync import collect_scheduled_tabular_reports
                mailbox = str(
                    scheduled_reports.get("mailbox") or os.environ.get("ACCOUNTABILITY_REPORT_MAILBOX", "")
                ).strip()
                service_account = os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
                if not mailbox or not service_account:
                    raise ValueError("scheduled report collection requires report mailbox and delegated service account")
                service = build_keyless_report_mailbox_service(service_account, mailbox)
                collected = collect_scheduled_tabular_reports(
                    service, output_dir=output_dir / "scheduled-reports", config=scheduled_reports, as_of=run_at
                )
                sources.update(collected["sources"])
                collected_trackers.update(collected["trackers"])
                source_evidence_manifests.append(str(collected["manifest"]))
            evidence_email = dict(collection.get("evidence_email") or {})
            if evidence_email.get("enabled"):
                from .evidence_email_sync import collect_scheduled_evidence

                source_specs = {
                    key: value
                    for key, value in dict(evidence_email.get("sources") or {}).items()
                    if not (value or {}).get("modes") or mode in (value or {}).get("modes", [])
                }
                if source_specs:
                    mailbox = str(
                        evidence_email.get("mailbox")
                        or os.environ.get("ACCOUNTABILITY_REPORT_MAILBOX", "")
                    ).strip()
                    service_account = os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
                    collected_sources, evidence_manifest = collect_scheduled_evidence(
                        service_account_email=service_account,
                        mailbox=mailbox,
                        output_dir=output_dir / "source-evidence",
                        source_specs=source_specs,
                    )
                    conflicts = sorted(set(collected_sources).intersection(key for key, value in sources.items() if value))
                    if conflicts:
                        raise ValueError(
                            "scheduled evidence conflicts with configured source paths: " + ", ".join(conflicts)
                        )
                    sources.update(collected_sources)
                    source_evidence_manifests.append(str(evidence_manifest))
            submission_browser = dict(collection.get("ezlynx_submission_browser") or {})
            if mode == "weekly" and submission_browser.get("enabled"):
                from .ezlynx_session_lock import exclusive_session
                from .submission_audit import ensure_ezlynx_login, run_weekly_submission_read
                with exclusive_session():
                    ensure_ezlynx_login()
                    observed_submissions = run_weekly_submission_read(fresh=True)
                submission_path = output_dir / f"submission-center-{run_at:%Y%m%dT%H%M%SZ}.csv"
                headers = [
                    "Submission Title", "Submission URL", "Applicant", "Assigned Producer",
                    "Status", "Quote Due Date", "Effective Date", "Overdue",
                ]
                with submission_path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(observed_submissions["open_records"])
                submission_evidence = output_dir / f"submission-center-evidence-{run_at:%Y%m%dT%H%M%SZ}.json"
                submission_evidence.write_text(json.dumps({
                    "collected_at": run_at.isoformat(),
                    "read_only": True,
                    "all_pages_inspected": True,
                    "pages_inspected": observed_submissions.get("pages_inspected"),
                    "rows_inspected": observed_submissions.get("rows_inspected"),
                    "pager_total": observed_submissions.get("pager_total"),
                    "open_records_deduplicated": observed_submissions.get("open_records_deduplicated"),
                    "normalized_csv_sha256": _checksum(submission_path),
                }, indent=2), encoding="utf-8")
                sources["submissions"] = str(submission_path)
            for key, flag in SOURCE_FLAGS.items():
                value = sources.get(key)
                if value:
                    arguments.extend([flag, str(Path(str(value)).expanduser().resolve())])
            if mode in {"daily", "weekly"} and not sources.get("email_json"):
                from .gmail_accountability import collect_agency_summary

                gmail_snapshot = collect_agency_summary(
                    environment=os.environ,
                    config=dict(collection.get("gmail") or {}),
                )
                gmail_path = output_dir / f"gmail-{run_at:%Y%m%dT%H%M%SZ}.json"
                gmail_path.write_text(json.dumps(gmail_snapshot, indent=2, default=str), encoding="utf-8")
                arguments.extend(["--email-json", str(gmail_path)])
            sales_untouched_days = (manifest.get("rules") or {}).get("sales_untouched_days")
            if sales_untouched_days is not None:
                arguments.extend(["--sales-untouched-days", str(max(1, int(sales_untouched_days)))])
            tracker_sources = dict(((manifest.get("sources") or {}).get("trackers") or {}))
            tracker_sources.update(collected_trackers)
            for key, value in sorted(tracker_sources.items()):
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
                    {"artifact_path": str(output), "source_evidence_manifests": source_evidence_manifests},
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
                    {"artifact_path": str(output), "source_evidence_manifests": source_evidence_manifests},
                    {"sha256": _checksum(output), "delivery_receipts": receipts},
                    retryable=True,
                    error=f"accountability delivery failed: {type(exc).__name__}: {exc}",
                )
        return WorkerResult(
            True,
            action,
            {
                "artifact_path": str(output),
                "delivery_receipts": receipts,
                "dashboard_receipt": dashboard_receipt,
                "source_evidence_manifests": source_evidence_manifests,
            },
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
        source_evidence = []
        for raw_manifest in destination.get("source_evidence_manifests", []) or []:
            evidence_path = Path(str(raw_manifest))
            item: dict[str, Any] = {"manifest": str(evidence_path), "exists": evidence_path.is_file()}
            checks: list[dict[str, Any]] = []
            if item["exists"]:
                try:
                    evidence_data = _manifest(evidence_path)
                    for receipt in evidence_data.get("attachments", []) or []:
                        attachment = Path(str(receipt.get("path") or ""))
                        actual = _checksum(attachment) if attachment.is_file() else None
                        checks.append({
                            "source": receipt.get("source"),
                            "exists": attachment.is_file(),
                            "sha256_matches": bool(actual and actual == receipt.get("sha256")),
                        })
                    paths = {
                        **dict(evidence_data.get("sources") or {}),
                        **dict(evidence_data.get("trackers") or {}),
                    }
                    for source, receipt in dict(evidence_data.get("evidence") or {}).items():
                        if receipt.get("source_status") != "available":
                            continue
                        attachment = Path(str(paths.get(source) or ""))
                        actual = _checksum(attachment) if attachment.is_file() else None
                        checks.append({
                            "source": source,
                            "exists": attachment.is_file(),
                            "sha256_matches": bool(
                                actual and actual == receipt.get("normalized_csv_sha256")
                            ),
                        })
                    item["attachments"] = checks
                    item["verified"] = bool(checks) and all(
                        check["exists"] and check["sha256_matches"] for check in checks
                    )
                except Exception as exc:
                    item.update({"verified": False, "error_type": type(exc).__name__})
            else:
                item["verified"] = False
            source_evidence.append(item)
        if source_evidence:
            observed["source_evidence"] = source_evidence
            verified = verified and all(item.get("verified") for item in source_evidence)
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
            method="FRESH_DESTINATION_AND_FILESYSTEM_READBACK" if (receipts or dashboard_receipt or source_evidence) else "FRESH_FILESYSTEM_READBACK",
            source="accountability-report-and-destinations" if (receipts or dashboard_receipt or source_evidence) else "accountability-report-artifact",
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
