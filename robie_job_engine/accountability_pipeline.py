"""Weekday collect → publish → deliver path for the department-tab Google Doc.

This is the only accountability report: the persistent Team Lead Doc on
streetsmart-accountability-prod, emailed Mon–Fri at 9:00 AM ET. It is not a
MacBook Air digest and not a Hermes Job Engine markdown duplicate.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .accountability_delivery import deliver_report, verify_delivery_receipts
from .business_calendar import previous_business_day
from .scheduled_report_ingest import (
    EXACT_EZLYNX_SUBJECTS,
    MISSING_SUBJECTS_EXIT,
    ScheduledReportEvidenceError,
    collect_exact_daily_sources,
)


TEAM_LEAD_DOC_ID = "1lnhIplYM8DLdFyITL9tCUVrImYzYy1Vbfax7eeAkdhg"
TEAM_LEAD_DOC_URL = f"https://docs.google.com/document/d/{TEAM_LEAD_DOC_ID}/edit"
COLLECT_HOUR = 6
COLLECT_MINUTE = 45
PUBLISH_HOUR = 9
PUBLISH_MINUTE = 0
LATE_CHECK_HOUR = 9
LATE_CHECK_MINUTE = 20


def reporting_date(now: datetime | None = None, *, holiday_calendar: str | None = "US-FEDERAL") -> str:
    eastern = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    return previous_business_day(eastern.date(), holiday_calendar=holiday_calendar).isoformat()


def _state_dir(root: Path, report_date: str) -> Path:
    path = Path(root) / report_date
    path.mkdir(parents=True, exist_ok=True)
    return path


def collect(
    service: Any,
    *,
    state_root: Path,
    now: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    at = now or datetime.now(timezone.utc)
    day = reporting_date(at)
    output = _state_dir(state_root, day) / "collect"
    try:
        snapshot = collect_exact_daily_sources(service, output_dir=output, config=config, as_of=at)
    except ScheduledReportEvidenceError as exc:
        payload = {
            "ok": False,
            "exit_code": MISSING_SUBJECTS_EXIT,
            "report_date": day,
            "error": str(exc),
            "required_subjects": list(EXACT_EZLYNX_SUBJECTS.values()),
        }
        (_state_dir(state_root, day) / "collect-failed.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        raise SystemExit(MISSING_SUBJECTS_EXIT) from exc
    snapshot["report_date"] = day
    snapshot["ok"] = True
    return snapshot


def publish(
    *,
    state_root: Path,
    report_date: str | None = None,
    now: datetime | None = None,
    document_url: str = TEAM_LEAD_DOC_URL,
) -> dict[str, Any]:
    """Mark the existing department-tab Doc as the publish target.

    This does not generate a second Hermes or MacBook digest. If collect is
    missing, 9:00 AM refuses to publish. The dedicated VM publisher may rewrite
    the same Doc; this step records that destination after sources are complete.
    """
    day = report_date or reporting_date(now)
    folder = _state_dir(state_root, day)
    collect_files = sorted(folder.joinpath("collect").glob("collect-*.json"))
    if not collect_files:
        raise ScheduledReportEvidenceError(
            f"publish refused: no collect snapshot for {day}"
        )
    snapshot = json.loads(collect_files[-1].read_text(encoding="utf-8"))
    receipt = {
        "ok": True,
        "report_date": day,
        "document_id": TEAM_LEAD_DOC_ID,
        "document_url": document_url,
        "report_kind": "department_tab_google_doc",
        "collect_snapshot": str(collect_files[-1]),
        "sources": snapshot.get("sources") or {},
        "published_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
    path = folder / "publish.json"
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    receipt["path"] = str(path)
    return receipt


def _team_lead_body(report_date: str, document_url: str) -> str:
    return (
        "Team Leads,\n\n"
        f"Yesterday's department accountability report is ready:\n{document_url}\n\n"
        "This version includes:\n"
        "- department-only Google Doc tabs\n"
        "- hourly queue calls by department\n"
        "- direct, queue, and outbound talk time\n"
        "- missed-call callback evidence\n"
        "- metadata-only employee email reply rates, unresolved client mail,\n"
        "genuine client-Spam risks folded into the combined risk table, and\n"
        "grouped EZLynx SMS verification flags\n"
        "- cross-channel phone, email, SMS, Magellan, EZLynx, Submission\n"
        "Center, COI, and policy-tracker evidence\n\n"
        "Please scrutinize the report and reply with corrections, missing\n"
        "context, or recommended changes.\n"
    )


def deliver(
    *,
    state_root: Path,
    report_date: str | None = None,
    now: datetime | None = None,
    delivery: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
    send: Any | None = None,
) -> dict[str, Any]:
    day = report_date or reporting_date(now)
    folder = _state_dir(state_root, day)
    publish_path = folder / "publish.json"
    if not publish_path.is_file():
        raise ScheduledReportEvidenceError(f"deliver refused: no publish receipt for {day}")
    published = json.loads(publish_path.read_text(encoding="utf-8"))
    body = _team_lead_body(day, str(published.get("document_url") or TEAM_LEAD_DOC_URL))
    report_path = folder / "team-lead-email.txt"
    report_path.write_text(body, encoding="utf-8")
    config = dict(delivery or {})
    config.setdefault("email_sender", "robie@streetsmart.insurance")
    config.setdefault(
        "email_recipients",
        {
            "daily": [
                "carlo@streetsmart.insurance",
                "jake@streetsmart.insurance",
                "ashley@streetsmart.insurance",
                "gabrielac@streetsmart.insurance",
                "sandy@streetsmart.insurance",
            ]
        },
    )
    if send is not None:
        sent = send(body, config)
        receipts = [sent]
        delivered = bool(sent.get("delivered") or sent.get("message_id"))
        verified, observed = delivered, [{"delivered": delivered}]
    else:
        receipts = deliver_report(
            report_path,
            mode="daily",
            delivery=config,
            environment=dict(environment or os.environ),
            subject=f"StreetSmart Yesterday Accountability — {day}",
        )
        verified, observed = verify_delivery_receipts(
            receipts, environment=dict(environment or os.environ)
        )
        delivered = bool(verified)
    result = {
        "ok": delivered,
        "delivered": delivered,
        "report_date": day,
        "subject": f"StreetSmart Yesterday Accountability — {day}",
        "document_url": published.get("document_url"),
        "receipts": receipts,
        "verification": observed,
    }
    path = folder / "deliver.json"
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    if delivered:
        (folder / "success.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "report_date": day,
                    "delivered": True,
                    "document_url": published.get("document_url"),
                    "marked_at": (now or datetime.now(timezone.utc)).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    result["path"] = str(path)
    return result


def late_check(
    *,
    state_root: Path,
    report_date: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """9:20 AM ET check. The 6:50 AM wording is stale and must not be reused."""
    day = report_date or reporting_date(now)
    folder = Path(state_root) / day
    success = folder / "success.json"
    if success.is_file():
        return {"ok": True, "late": False, "report_date": day, "deadline": "9:20 AM Eastern"}
    return {
        "ok": False,
        "late": True,
        "report_date": day,
        "deadline": "9:20 AM Eastern",
        "subject": f"LATE — StreetSmart accountability report not delivered — {day}",
        "body": (
            "The weekday Production accountability report has no verified success\n"
            "marker by 9:20 AM Eastern.\n\n"
            "Collect runs at 6:45 AM Eastern. Publish and deliver run at 9:00 AM\n"
            "Eastern. The server may still be collecting a late source, or the\n"
            "job may have failed. No successful team delivery has been recorded."
        ),
    }


def needs_gmail_service(
    *,
    collect_enabled: bool,
    publish_enabled: bool,
    state_root: Path,
    now: datetime | None = None,
) -> bool:
    """Publish-only does not need Gmail when the 6:45 collect snapshot exists."""
    if collect_enabled:
        return True
    if publish_enabled:
        day = reporting_date(now)
        collect_dir = Path(state_root) / day / "collect"
        return not list(collect_dir.glob("collect-*.json"))
    return False


def run_stages(
    *,
    collect_enabled: bool,
    publish_enabled: bool,
    deliver_enabled: bool,
    late_check_enabled: bool,
    service: Any | None,
    state_root: Path,
    now: datetime | None = None,
    delivery: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
    send: Any | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"report_date": reporting_date(now)}
    if collect_enabled:
        if service is None:
            raise ValueError("collect requires a Gmail service")
        result["collect"] = collect(service, state_root=state_root, now=now)
    if publish_enabled:
        # 9:00 AM retries collect when the 6:45 snapshot is absent.
        if service is not None and not list((_state_dir(state_root, result["report_date"]) / "collect").glob("collect-*.json")):
            result["collect"] = collect(service, state_root=state_root, now=now)
        result["publish"] = publish(state_root=state_root, report_date=result["report_date"], now=now)
    if deliver_enabled:
        result["deliver"] = deliver(
            state_root=state_root,
            report_date=result["report_date"],
            now=now,
            delivery=delivery,
            environment=environment,
            send=send,
        )
    if late_check_enabled:
        result["late_check"] = late_check(state_root=state_root, report_date=result["report_date"], now=now)
    return result
