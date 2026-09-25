"""Weekday end-of-day Team Lead Chat wrap-up for the current Eastern day.

Reads an already-prepared accountability snapshot for today
(America/New_York). When that snapshot is missing or does not verify, posts
a short status that sources were not ready. Does not invent metrics, send
leadership email, or rewrite the department Google Doc.

Posts only through :mod:`src.reporters.team_lead_chat`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src.reporters.team_lead_chat import (
    TeamLeadChatPostError,
    TeamLeadChatWebhookMissing,
    post_team_lead_chat,
    resolve_team_lead_chat_webhook,
)

EASTERN = ZoneInfo("America/New_York")
PREPARED_MAX_AGE_HOURS = 4
DEPARTMENT_NAMES = (
    "Personal Lines",
    "Commercial Lines",
    "Trucking and Transportation",
    "Business Development",
    "Operations",
    "Executive & Unverified",
)
REQUIRED_DEPARTMENT_KEYS = frozenset({
    "phone_metrics",
    "queues",
    "hourly_queues",
    "missed_calls",
    "email_performance",
    "email_pending",
    "email_spam_alerts",
    "sms_alerts",
    "customer_response_risks",
    "overdue_tasks",
    "sales_untouched",
    "policy_changes",
    "cois",
    "submissions",
    "magellan_sad",
})
READY_SOURCE_STATUSES = frozenset({"ready", "ready_api", "ready_exact_date_seed"})
OPEN_ITEM_FIELDS = (
    ("overdue_tasks", "Overdue tasks"),
    ("missed_calls", "Missed calls"),
    ("policy_changes", "Policy changes"),
    ("cois", "Pending COIs"),
    ("submissions", "Submission Center rows"),
)
DOC_LINE = (
    "This is a Chat wrap-up only. The official weekday deliverable is still "
    "the 9:00 AM department Doc."
)
PHONE_RUN = re.compile(r"\d{7,}")


@dataclass
class DaySources:
    """What EOD is allowed to say about one Eastern calendar day."""

    day: date
    departments: dict[str, Any] | None = None
    snapshot_verified: bool = False
    snapshot_problem: str = ""
    magellan_cache: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)


def eastern_today(now: datetime | None = None) -> date:
    moment = now or datetime.now(EASTERN)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=EASTERN)
    return moment.astimezone(EASTERN).date()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_paths(app_root: Path, day: date) -> tuple[Path, Path, Path]:
    stamp = day.isoformat()
    outputs = app_root / "data" / "outputs"
    return (
        outputs / f"department_dashboard_{stamp}.json",
        outputs / f"department_dashboard_{stamp}.prepared.json",
        app_root / "data" / "inbox" / stamp / "manifest.json",
    )


def _list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def magellan_section_unverified(departments: Mapping[str, Any] | None) -> bool:
    """Match the publish empty-gate: unverified Magellan must not look like zero.

    Same rule as ``scripts.magellan_empty_gate.snapshot_magellan_blocks_delivery``
    without reading ``MAGELLAN_ALLOW_EMPTY``. End-of-day Chat states the gap
    instead of refusing to speak.
    """
    if not isinstance(departments, Mapping) or not departments:
        return True
    sad_rows = 0
    saw_call_count = False
    any_calls = False
    all_verified_empty = True
    saw_verified_flag = False
    for data in departments.values():
        if not isinstance(data, dict):
            return True
        sad = data.get("magellan_sad")
        if not isinstance(sad, list):
            return True
        sad_rows += len(sad)
        if "magellan_calls_on_target_date" in data:
            saw_call_count = True
            count = data.get("magellan_calls_on_target_date")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                return True
            if count > 0:
                any_calls = True
        else:
            all_verified_empty = False
        if "magellan_extract_verified_empty" in data:
            saw_verified_flag = True
            if data.get("magellan_extract_verified_empty") is not True:
                all_verified_empty = False
        else:
            all_verified_empty = False
    if sad_rows > 0 or any_calls:
        return False
    if saw_call_count and saw_verified_flag and all_verified_empty:
        return False
    return True


def _load_verified_snapshot(app_root: Path, day: date, now: datetime) -> tuple[dict[str, Any] | None, str]:
    """Return departments when the prepared snapshot still verifies."""
    snapshot, prepared, manifest = _snapshot_paths(app_root, day)
    if not snapshot.is_file() or not prepared.is_file() or not manifest.is_file():
        return None, "prepared snapshot for this day is missing"
    try:
        metadata = json.loads(prepared.read_text(encoding="utf-8"))
        source_manifest = json.loads(manifest.read_text(encoding="utf-8"))
        departments = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "prepared snapshot could not be read"
    if not isinstance(metadata, dict) or metadata.get("target_date") != day.isoformat():
        return None, "prepared snapshot target date does not match"
    try:
        prepared_at = datetime.fromisoformat(
            str(metadata.get("prepared_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return None, "prepared snapshot has no timestamp"
    if prepared_at.tzinfo is None:
        prepared_at = prepared_at.replace(tzinfo=timezone.utc)
    age_hours = (now.astimezone(timezone.utc) - prepared_at.astimezone(timezone.utc)).total_seconds() / 3600
    if age_hours < 0 or age_hours > PREPARED_MAX_AGE_HOURS:
        return None, "prepared snapshot is stale"
    if _sha256(snapshot) != metadata.get("snapshot_sha256"):
        return None, "prepared snapshot hash does not verify"
    if _sha256(manifest) != metadata.get("manifest_sha256"):
        return None, "prepared source manifest hash does not verify"
    sources = source_manifest.get("sources") if isinstance(source_manifest, dict) else None
    if not isinstance(sources, dict) or not sources:
        return None, "prepared source manifest is not fully ready"
    statuses = {item.get("status") for item in sources.values() if isinstance(item, dict)}
    if not statuses or not statuses.issubset(READY_SOURCE_STATUSES):
        return None, "prepared source manifest is not fully ready"
    if not isinstance(departments, dict) or set(departments) != set(DEPARTMENT_NAMES):
        return None, "prepared snapshot department set does not verify"
    for department, data in departments.items():
        if not isinstance(data, dict):
            return None, f"prepared snapshot {department} is not an object"
        missing = REQUIRED_DEPARTMENT_KEYS - set(data)
        if missing:
            return None, f"prepared snapshot {department} is missing required sections"
    return departments, ""


def _load_magellan_cache(app_root: Path, day: date) -> dict[str, Any] | None:
    path = app_root / "data" / "raw" / "magellan_latest.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("target_date") or "") != day.isoformat():
        return None
    return payload


def load_day_sources(app_root: Path, day: date, now: datetime | None = None) -> DaySources:
    moment = now or datetime.now(EASTERN)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=EASTERN)
    sources = DaySources(day=day)
    try:
        departments, problem = _load_verified_snapshot(app_root, day, moment)
    except OSError:
        departments, problem = None, "prepared snapshot could not be read"
    sources.departments = departments
    sources.snapshot_verified = departments is not None
    sources.snapshot_problem = problem
    if problem:
        sources.problems.append(problem)
    sources.magellan_cache = _load_magellan_cache(app_root, day)
    return sources


def _sad_rows(departments: Mapping[str, Any]) -> int:
    total = 0
    for data in departments.values():
        if isinstance(data, dict):
            total += _list_len(data.get("magellan_sad"))
    return total


def _stamped_call_count(departments: Mapping[str, Any]) -> int | None:
    seen: int | None = None
    for data in departments.values():
        if not isinstance(data, dict) or "magellan_calls_on_target_date" not in data:
            continue
        count = data.get("magellan_calls_on_target_date")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        if seen is None:
            seen = count
        elif seen != count:
            return None
    return seen


def _cache_call_count(cache: Mapping[str, Any]) -> int | None:
    """Return a Magellan call count only when the extract is complete enough to cite."""
    if str(cache.get("source_status") or "").casefold() != "available":
        return None
    if cache.get("pages_complete") is not True:
        return None
    calls = cache.get("calls")
    if not isinstance(calls, list):
        return None
    if calls:
        return len(calls)
    rows = cache.get("rows_inspected")
    if (
        cache.get("older_boundary_reached") is True
        and isinstance(rows, int)
        and not isinstance(rows, bool)
        and rows > 0
    ):
        return 0
    return None


def _cache_sad_count(cache: Mapping[str, Any]) -> int | None:
    if _cache_call_count(cache) is None:
        return None
    calls = cache.get("calls")
    if not isinstance(calls, list):
        return None
    sad = 0
    for row in calls:
        if not isinstance(row, dict):
            continue
        sentiment = str(row.get("sentiment") or row.get("Sentiment") or "").casefold()
        tags = row.get("tags") or row.get("Topics") or []
        tag_text = " ".join(str(tag) for tag in tags) if isinstance(tags, list) else str(tags)
        if sentiment in {"sad", "negative", "at risk", "at-risk"} or "risk" in tag_text.casefold():
            sad += 1
    return sad


def _open_item_lines(departments: Mapping[str, Any]) -> list[str]:
    lines = []
    for key, label in OPEN_ITEM_FIELDS:
        total = 0
        for data in departments.values():
            if isinstance(data, dict):
                total += _list_len(data.get(key))
        lines.append(f"- {label}: {total}")
    return lines


def _magellan_lines(sources: DaySources) -> list[str]:
    if sources.snapshot_verified and sources.departments is not None:
        departments = sources.departments
        if magellan_section_unverified(departments):
            lines = [
                "Magellan/SAD: not verified for this day. No sad-call count is shown."
            ]
        else:
            sad = _sad_rows(departments)
            lines = [f"Magellan/SAD: {sad} sad or at-risk calls in the prepared snapshot."]
            calls = _stamped_call_count(departments)
            if calls is not None:
                lines.append(f"Magellan calls recorded for this day: {calls}.")
        return lines
    cache = sources.magellan_cache
    if not cache:
        return [
            "Magellan/SAD: no prepared snapshot and no Magellan cache for this day. "
            "No call count is shown."
        ]
    calls = _cache_call_count(cache)
    sad = _cache_sad_count(cache)
    if calls is None or sad is None:
        return [
            "Magellan cache for this day was not a verified extract. No call count is shown."
        ]
    if calls == 0:
        return ["Magellan cache for this day: verified no calls."]
    return [
        f"Magellan cache for this day: {calls} calls, pages complete. "
        f"Sad or at-risk rows in that cache: {sad}."
    ]


def format_eod_message(sources: DaySources) -> str:
    """Plain-English Chat text. Counts only; no names, phones, or email addresses."""
    heading = (
        f"Team Lead Chat — end of day {sources.day.isoformat()} (America/New_York)."
    )
    if not sources.snapshot_verified:
        text = "\n".join([
            heading,
            "",
            "Today's accountability snapshot was not ready. Evening collect is "
            "incomplete or the prepared snapshot did not verify. Open-item counts "
            "are omitted.",
            "",
            *_magellan_lines(sources),
            "",
            DOC_LINE,
        ])
    else:
        assert sources.departments is not None
        text = "\n".join([
            heading,
            "",
            *_magellan_lines(sources),
            "",
            "Open items in today's prepared snapshot:",
            *_open_item_lines(sources.departments),
            "",
            DOC_LINE,
        ])
    if "@" in text or PHONE_RUN.search(text):
        raise TeamLeadChatPostError(
            "refusing to post Team Lead Chat text that contains a phone or email"
        )
    return text


def _receipt_path(app_root: Path, day: date) -> Path:
    return app_root / "data" / "run_state" / f"team_lead_chat_eod_{day.isoformat()}.json"


def _read_receipt(app_root: Path, day: date) -> dict[str, Any] | None:
    path = _receipt_path(app_root, day)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_receipt(app_root: Path, day: date, receipt: Mapping[str, Any]) -> None:
    path = _receipt_path(app_root, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(dict(receipt), indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def run_eod(
    *,
    app_root: Path,
    day: date | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    secret_reader=None,
    opener=None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build today's message and post it, or refuse when the webhook is missing."""
    moment = now or datetime.now(EASTERN)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=EASTERN)
    report_day = day or eastern_today(moment)
    sources = load_day_sources(app_root, report_day, moment)
    text = format_eod_message(sources)
    kind = "summary" if sources.snapshot_verified else "incomplete"
    if dry_run:
        return {
            "dry_run": True,
            "delivered": False,
            "status": kind,
            "target_date": report_day.isoformat(),
            "text": text,
        }
    prior = _read_receipt(app_root, report_day)
    if prior and prior.get("status") == "summary":
        return {
            "delivered": True,
            "duplicate": True,
            "status": "summary",
            "target_date": report_day.isoformat(),
            "message_name": str(prior.get("message_name") or ""),
        }
    if prior and prior.get("status") == "incomplete" and kind == "incomplete":
        return {
            "delivered": True,
            "duplicate": True,
            "status": "incomplete",
            "target_date": report_day.isoformat(),
            "message_name": str(prior.get("message_name") or ""),
        }
    try:
        webhook = resolve_team_lead_chat_webhook(
            secret_reader=secret_reader, environ=environ
        )
    except TeamLeadChatWebhookMissing as exc:
        return {
            "delivered": False,
            "status": "webhook_missing",
            "target_date": report_day.isoformat(),
            "reason": str(exc),
        }
    try:
        posted = post_team_lead_chat(text, webhook_url=webhook, opener=opener)
    except TeamLeadChatPostError as exc:
        return {
            "delivered": False,
            "status": "post_failed",
            "target_date": report_day.isoformat(),
            "reason": str(exc),
        }
    _write_receipt(app_root, report_day, {
        "target_date": report_day.isoformat(),
        "status": kind,
        "message_name": posted["message_name"],
        "posted_at": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "delivered": True,
        "duplicate": False,
        "status": kind,
        "target_date": report_day.isoformat(),
        "message_name": posted["message_name"],
    }


def _exit_code(result: Mapping[str, Any]) -> int:
    status = result.get("status")
    if result.get("dry_run"):
        return 0
    if status == "webhook_missing":
        return 3
    if status == "post_failed":
        return 4
    if result.get("delivered"):
        return 0
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post the Team Lead Chat end-of-day wrap-up for today (America/New_York)."
    )
    parser.add_argument("--date", help="Eastern calendar day YYYY-MM-DD. Default: today.")
    parser.add_argument(
        "--app-root",
        default=os.environ.get("ACCOUNTABILITY_APP_ROOT", "/opt/streetsmart-daily-accountability"),
        help="Dedicated accountability application root.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Chat text and do not read the webhook or post.",
    )
    args = parser.parse_args(argv)
    report_day = None
    if args.date:
        report_day = date.fromisoformat(args.date)
    result = run_eod(
        app_root=Path(args.app_root),
        day=report_day,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
