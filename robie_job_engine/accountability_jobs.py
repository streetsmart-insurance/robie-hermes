"""Durable, read-only accountability report jobs and verification."""

from __future__ import annotations

import hashlib
import json
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
    "sales_json": "--sales-json",
    "retention": "--retention",
    "retention_summary_json": "--retention-summary-json",
    "submissions": "--submissions",
    "email_json": "--email-json",
    "appsheet_json": "--appsheet-json",
    "magellan_json": "--magellan-json",
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
            for key, flag in SOURCE_FLAGS.items():
                value = (manifest.get("sources") or {}).get(key)
                if value:
                    arguments.extend([flag, str(Path(str(value)).expanduser().resolve())])
            for key, value in sorted(((manifest.get("sources") or {}).get("trackers") or {}).items()):
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
        except Exception as exc:
            return WorkerResult(
                False,
                action,
                {"manifest_path": str(manifest_path)},
                retryable=True,
                error=f"accountability report generation failed: {type(exc).__name__}: {exc}",
            )
        return WorkerResult(
            True,
            action,
            {"artifact_path": str(output)},
            {
                "sha256": _checksum(output),
                "mode": mode,
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
        evidence = VerificationEvidence(
            method="FRESH_FILESYSTEM_READBACK",
            source="accountability-report-artifact",
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
