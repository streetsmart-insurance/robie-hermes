"""Weekday holiday office-closure alerts (staff.holiday.alert).

Source of truth is the Holiday Schedule Google Doc, read fresh every run.
Dates in that Doc are smart-chip spans. The Docs API text payload drops them
("Monday, " with no date), so this job exports HTML (or accepts a markdown
table) and fails closed when the schedule table cannot be parsed.

Live email and Chat stay off unless Carlo sets the send flags. The default
run only plans alerts. Each event can send twice: a heads-up while it is
inside the T-14 window, then a shorter out-of-office nudge inside the T-3
window. The send ledger key is (event_date, status_kind, phase).
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .idempotency import assert_durable_path
from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from . import staff_jobs_common as common

ACTION_TYPE = "staff.holiday.alert"
TASK_NAME = "StreetSmart holiday office alerts"
WORKER_NAME = "staff-holiday-alert"
TIMEZONE = "America/New_York"

# First alert: from T-14 through the day before the nudge window.
# Nudge: from T-3 through the morning of the event (America/New_York).
# Past dates are skipped. A weekday run inside each window sends that phase once.
LEAD_DAYS_HEADS_UP = 14
LEAD_DAYS_NUDGE = 3
PHASE_HEADS_UP = "heads_up"
PHASE_NUDGE = "nudge"

HOLIDAY_DOC_ID = "1_sE6cCkyu0SuqucWU6z-FOnKqGBQkTTe02HtoeFv784"
DOC_URL = f"https://docs.google.com/document/d/{HOLIDAY_DOC_ID}/edit"
DOC_TITLE = "Holiday Schedule & Out-of-Office SOP"

SENDER = "robie@streetsmart.insurance"
EMAIL_TO = "StreetSmart@streetsmart.insurance"

SEND_ENV = "ROBIE_HOLIDAY_ALERT_SEND"
EMAIL_ENV = "ROBIE_HOLIDAY_ALERT_EMAIL"
CHAT_ENV = "ROBIE_HOLIDAY_ALERT_CHAT"
HEADS_UP_ENV = "ROBIE_HOLIDAY_ALERT_LEAD_DAYS_HEADS_UP"
NUDGE_ENV = "ROBIE_HOLIDAY_ALERT_LEAD_DAYS_NUDGE"
FIXTURE_ENV = "ROBIE_HOLIDAY_ALERT_ALLOW_FIXTURE"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_DATE_RE = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AP]M)\b", re.IGNORECASE)
_YEAR_SPAN_RE = re.compile(r"20\d{2}\s*-\s*20\d{2}")


class HolidayScheduleError(RuntimeError):
    """The holiday table could not be parsed. Callers must not send."""


@dataclass(frozen=True)
class HolidayAlert:
    holiday: str
    event_date: date
    status_kind: str  # "closed" | "early_close"
    close_time: str | None = None
    source: str = "explicit"  # "explicit" | "observed"
    phase: str = ""  # "heads_up" | "nudge" once the alert is due

    def with_phase(self, phase: str) -> HolidayAlert:
        if phase not in {PHASE_HEADS_UP, PHASE_NUDGE}:
            raise HolidayScheduleError(f"unknown holiday alert phase: {phase!r}")
        return HolidayAlert(
            self.holiday,
            self.event_date,
            self.status_kind,
            self.close_time,
            self.source,
            phase,
        )

    def ledger_key(self) -> str:
        if self.phase not in {PHASE_HEADS_UP, PHASE_NUDGE}:
            raise HolidayScheduleError("ledger key requires phase heads_up or nudge")
        return f"{self.event_date.isoformat()}|{self.status_kind}|{self.phase}"

    def as_dict(self) -> dict[str, str]:
        return {
            "holiday": self.holiday,
            "event_date": self.event_date.isoformat(),
            "status_kind": self.status_kind,
            "close_time": self.close_time or "",
            "source": self.source,
            "phase": self.phase,
            "ledger_key": self.ledger_key() if self.phase else "",
        }


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize(text: str) -> str:
    cleaned = (
        str(text or "")
        .replace("\xa0", " ")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_schedule_heading(text: str) -> bool:
    cleaned = _normalize(text).casefold()
    return "holiday schedule" in cleaned and _YEAR_SPAN_RE.search(cleaned) is not None


def _parse_dates(text: str, *, context: str) -> list[date]:
    found: list[date] = []
    for match in _DATE_RE.finditer(text):
        weekday_name, month_name, day_text, year_text = match.groups()
        month = _MONTHS[month_name.casefold()[:3]]
        try:
            parsed = date(int(year_text), month, int(day_text))
        except ValueError as exc:
            raise HolidayScheduleError(
                f"invalid date {match.group(0)!r} in {context}"
            ) from exc
        expected = _WEEKDAYS[weekday_name.casefold()]
        if parsed.weekday() != expected:
            raise HolidayScheduleError(
                f"weekday mismatch in {context}: {match.group(0)} is a "
                f"{parsed.strftime('%A')}"
            )
        found.append(parsed)
    return found


def _normalize_time(hour: str, minute: str, ampm: str) -> str:
    return f"{int(hour)}:{minute} {ampm.upper()}"


def _early_close_time(text: str, *, context: str) -> str:
    matches = list(_TIME_RE.finditer(text))
    if len(matches) != 1:
        raise HolidayScheduleError(
            f"early close needs exactly one time in {context}: {text!r}"
        )
    hour, minute, ampm = matches[0].groups()
    return _normalize_time(hour, minute, ampm)


def expand_status_row(holiday: str, date_text: str, status_text: str) -> list[HolidayAlert]:
    """Turn one schedule row into one or more alerts. Raises if the row is unclear."""
    name = _normalize(holiday)
    when = _normalize(date_text)
    status = _normalize(status_text).rstrip(".")
    if not name or not when or not status:
        raise HolidayScheduleError(
            f"holiday row is missing a column: {holiday!r} | {date_text!r} | {status_text!r}"
        )
    column_dates = _parse_dates(when, context=f"{name} date column")
    if not column_dates:
        raise HolidayScheduleError(f"no parseable date for {name!r}: {when!r}")

    if status.casefold().startswith("observed"):
        observed = _parse_dates(status, context=f"{name} observed status")
        if len(observed) != 1:
            raise HolidayScheduleError(
                f"observed status for {name!r} must name one date: {status!r}"
            )
        return [
            HolidayAlert(name, observed[0], "closed", None, "observed")
        ]

    alerts: list[HolidayAlert] = []
    parts = [part.strip().rstrip(".") for part in status.split(";") if part.strip()]
    if not parts:
        raise HolidayScheduleError(f"empty office status for {name!r}")
    for part in parts:
        folded = part.casefold()
        if folded == "closed":
            for event_date in column_dates:
                alerts.append(HolidayAlert(name, event_date, "closed", None, "explicit"))
            continue
        if "close early" in folded:
            close_time = _early_close_time(part, context=name)
            embedded = _parse_dates(part, context=f"{name} early close")
            targets = embedded or column_dates
            for event_date in targets:
                alerts.append(
                    HolidayAlert(name, event_date, "early_close", close_time, "explicit")
                )
            continue
        raise HolidayScheduleError(
            f"unrecognized office status for {name!r}: {part!r}"
        )
    if not alerts:
        raise HolidayScheduleError(f"row produced no alerts: {name!r}")
    return alerts


def dedupe_observed_alerts(alerts: list[HolidayAlert]) -> list[HolidayAlert]:
    """Drop an observed-day alert when that calendar day already has an explicit row.

    Independence Day 2026 is a Saturday observed Friday, Jul 3, and Jul 3 already
    has its own early-close row. One day must not produce two emails.
    """
    explicit_dates = {alert.event_date for alert in alerts if alert.source == "explicit"}
    kept: list[HolidayAlert] = []
    seen: set[tuple[date, str]] = set()
    for alert in alerts:
        if alert.source == "observed" and alert.event_date in explicit_dates:
            continue
        key = (alert.event_date, alert.status_kind)
        if key in seen:
            continue
        seen.add(key)
        kept.append(alert)
    return kept


class _HolidayHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.markers: list[tuple[str, int]] = []
        self.tables: list[list[list[str]]] = []
        self._skip = 0
        self._in_table = 0
        self._rows: list[list[str]] | None = None
        self._in_cell = False
        self._cell: list[str] = []
        self._in_block = False
        self._block: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "table":
            self._in_table += 1
            if self._in_table == 1:
                self._rows = []
        elif self._in_table == 1 and tag == "tr" and self._rows is not None:
            self._rows.append([])
        elif self._in_table == 1 and tag in {"td", "th"}:
            self._in_cell = True
            self._cell = []
        elif tag == "br" and self._in_cell:
            self._cell.append(" ")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p"} and self._in_table == 0:
            self._in_block = True
            self._block = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag in {"td", "th"} and self._in_cell and self._rows:
            self._rows[-1].append(_normalize("".join(self._cell)))
            self._in_cell = False
            self._cell = []
        elif tag == "tr":
            return
        elif tag == "table" and self._in_table:
            self._in_table -= 1
            if self._in_table == 0 and self._rows is not None:
                self.tables.append(self._rows)
                self._rows = None
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p"} and self._in_block:
            text = _normalize("".join(self._block))
            if text:
                self.markers.append((text, len(self.tables)))
            self._in_block = False
            self._block = []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_cell:
            self._cell.append(data)
        if self._in_block:
            self._block.append(data)


def _header_ok(cells: list[str]) -> bool:
    if len(cells) < 3:
        return False
    holiday, when, status = (cell.casefold() for cell in cells[:3])
    return "holiday" in holiday and "date" in when and ("status" in status or "office" in status)


def _rows_from_cells(table: list[list[str]]) -> list[tuple[str, str, str]]:
    content = [row for row in table if any(cell.strip() for cell in row)]
    if not content:
        raise HolidayScheduleError("holiday table is empty")
    header, body = content[0], content[1:]
    if not _header_ok(header):
        raise HolidayScheduleError(
            "holiday table header is not Holiday / Date / Office status: "
            + " | ".join(header)
        )
    rows: list[tuple[str, str, str]] = []
    for index, row in enumerate(body, start=1):
        if len(row) < 3:
            raise HolidayScheduleError(
                f"holiday table row {index} has {len(row)} cells, expected 3"
            )
        holiday, when, status = row[0], row[1], row[2]
        if not (holiday and when and status):
            raise HolidayScheduleError(
                f"holiday table row {index} has an empty column: {row[:3]!r}"
            )
        rows.append((holiday, when, status))
    if not rows:
        raise HolidayScheduleError("holiday table has a header but no data rows")
    return rows


def _rows_from_html(document: str) -> list[tuple[str, str, str]]:
    parser = _HolidayHTMLParser()
    parser.feed(document)
    parser.close()
    heading_at: int | None = None
    for text, tables_before in parser.markers:
        if _is_schedule_heading(text):
            heading_at = tables_before
            break
    if heading_at is None:
        raise HolidayScheduleError(
            "2026-2027 Holiday Schedule heading not found; refusing to guess a table"
        )
    if heading_at >= len(parser.tables):
        raise HolidayScheduleError(
            "Holiday Schedule heading has no following table"
        )
    return _rows_from_cells(parser.tables[heading_at])


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [_normalize(cell) for cell in stripped.split("|")]


def _rows_from_markdown(document: str) -> list[tuple[str, str, str]]:
    lines = document.splitlines()
    heading_at: int | None = None
    for index, line in enumerate(lines):
        if _is_schedule_heading(line.strip().lstrip("#").strip()):
            heading_at = index
            break
    if heading_at is None:
        raise HolidayScheduleError(
            "2026-2027 Holiday Schedule heading not found; refusing to guess a table"
        )
    table_lines: list[str] = []
    started = False
    for line in lines[heading_at + 1:]:
        if "|" in line:
            started = True
            table_lines.append(line)
            continue
        if started:
            break
    if not table_lines:
        raise HolidayScheduleError("Holiday Schedule heading has no following markdown table")
    cells = [
        _split_markdown_row(line)
        for line in table_lines
        if not re.match(r"^\s*\|?\s*:?-{3,}", line)
    ]
    cells = [row for row in cells if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in row)]
    return _rows_from_cells(cells)


def parse_schedule_rows(document: str) -> list[tuple[str, str, str]]:
    """Parse every data row under the holiday-schedule heading."""
    text = str(document or "").strip()
    if not text:
        raise HolidayScheduleError("empty holiday schedule document")
    if "<table" in text.casefold():
        return _rows_from_html(text)
    if "|" in text:
        return _rows_from_markdown(text)
    raise HolidayScheduleError(
        "holiday schedule is neither an HTML table nor a markdown table"
    )


def load_schedule_alerts(document: str) -> tuple[list[tuple[str, str, str]], list[HolidayAlert]]:
    """Read every row and expand alerts. Fail closed on any unreadable row."""
    rows = parse_schedule_rows(document)
    expanded: list[HolidayAlert] = []
    for holiday, when, status in rows:
        alerts = expand_status_row(holiday, when, status)
        if not alerts:
            raise HolidayScheduleError(f"row produced no alerts: {holiday!r}")
        expanded.extend(alerts)
    deduped = dedupe_observed_alerts(expanded)
    if not deduped:
        raise HolidayScheduleError("deduped holiday schedule is empty")
    return rows, deduped


def _check_leads(heads_up_days: int, nudge_days: int) -> None:
    if nudge_days < 0 or heads_up_days <= nudge_days or heads_up_days > 60:
        raise HolidayScheduleError(
            f"lead windows must satisfy 0 <= nudge < heads_up <= 60, "
            f"got heads_up={heads_up_days} nudge={nudge_days}"
        )


def phase_for_delta(
    delta_days: int,
    *,
    heads_up_days: int = LEAD_DAYS_HEADS_UP,
    nudge_days: int = LEAD_DAYS_NUDGE,
) -> str | None:
    """Return the phase whose window contains this many days before the event."""
    _check_leads(heads_up_days, nudge_days)
    if delta_days < 0:
        return None
    if delta_days <= nudge_days:
        return PHASE_NUDGE
    if delta_days <= heads_up_days:
        return PHASE_HEADS_UP
    return None


def select_due(
    alerts: list[HolidayAlert],
    today: date,
    *,
    heads_up_days: int = LEAD_DAYS_HEADS_UP,
    nudge_days: int = LEAD_DAYS_NUDGE,
) -> list[HolidayAlert]:
    """Attach heads_up or nudge. Each event is in at most one phase per morning."""
    due: list[HolidayAlert] = []
    for alert in alerts:
        phase = phase_for_delta(
            (alert.event_date - today).days,
            heads_up_days=heads_up_days,
            nudge_days=nudge_days,
        )
        if phase:
            due.append(alert.with_phase(phase))
    return sorted(
        due,
        key=lambda alert: (
            alert.event_date,
            0 if alert.phase == PHASE_HEADS_UP else 1,
            alert.status_kind,
            alert.holiday,
        ),
    )


def _long_date(event_date: date) -> str:
    return event_date.strftime("%A, %B %-d, %Y")


def _short_date(event_date: date) -> str:
    return event_date.strftime("%A, %B %-d")


def _hours_sentence(alert: HolidayAlert) -> str:
    when = _long_date(alert.event_date)
    if alert.status_kind == "closed":
        return f"the office will be CLOSED on {when} for {alert.holiday}."
    return (
        f"the office will CLOSE EARLY at {alert.close_time} on {when} "
        f"for {alert.holiday}."
    )


def email_subject(alert: HolidayAlert) -> str:
    when = _short_date(alert.event_date)
    if alert.phase == PHASE_NUDGE:
        if alert.status_kind == "closed":
            return f"Reminder: set out-of-office — office closed {when}"
        return f"Reminder: set out-of-office — early close {when} at {alert.close_time}"
    if alert.status_kind == "closed":
        return f"Office closed {when} — {alert.holiday}"
    return f"Office closes early {when} at {alert.close_time} — {alert.holiday}"


def email_body(alert: HolidayAlert) -> str:
    hours = _hours_sentence(alert)
    if alert.phase == PHASE_NUDGE:
        return (
            "Hi team,\n\n"
            f"3-day reminder: {hours}\n\n"
            "Please set up your out-of-office today:\n"
            "- Turn on your Gmail vacation responder. Use the template in the holiday schedule Doc.\n"
            "- Block the day in Google Calendar and update your availability.\n"
            "- Set RingCentral to forward all calls to your line-of-business queue "
            "(Personal Lines, Commercial, or Trucking), as the Doc describes.\n\n"
            f"Holiday schedule: {DOC_URL}\n\n"
            "— Robie (StreetSmart automation)"
        )
    return (
        "Hi team,\n\n"
        f"Heads up: {hours}\n\n"
        "Please plan ahead and make sure clients know about the hours change. "
        "Claims still go to the carrier.\n\n"
        f"Holiday schedule: {DOC_URL}\n\n"
        "— Robie (StreetSmart automation)"
    )


def chat_text(alert: HolidayAlert) -> str:
    when = _short_date(alert.event_date)
    if alert.status_kind == "closed":
        headline = f"🏢 Office closed {when} — {alert.holiday}"
        detail = f"We will be CLOSED on {_long_date(alert.event_date)}."
    else:
        headline = f"⏰ Office closes early {when} at {alert.close_time} — {alert.holiday}"
        detail = (
            f"We will CLOSE EARLY at {alert.close_time} on {_long_date(alert.event_date)}."
        )
    if alert.phase == PHASE_NUDGE:
        return (
            f"{headline}\n\n"
            f"{detail} Please set up out-of-office today:\n"
            "• Gmail vacation responder — use the Doc template\n"
            "• Block the day in Google Calendar\n"
            "• RingCentral: forward all calls to your queue\n\n"
            f"Holiday schedule: {DOC_URL}"
        )
    return (
        f"{headline}\n\n"
        f"{detail}\n\n"
        "Please plan ahead and let clients know.\n\n"
        f"Holiday schedule: {DOC_URL}"
    )


def resolve_channels(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Live channels stay off unless the master flag and that channel flag are set.

    Payload dry_run=true forces a dry run even when the env flags are on.
    Payload dry_run=false does not enable sending by itself.
    """
    payload = dict(payload or {})
    forced_dry = "dry_run" in payload and _truthy(payload.get("dry_run"))
    master = _truthy(os.environ.get(SEND_ENV))
    email = master and _truthy(os.environ.get(EMAIL_ENV)) and not forced_dry
    chat = master and _truthy(os.environ.get(CHAT_ENV)) and not forced_dry
    dry_run = forced_dry or not (email or chat)
    if forced_dry:
        reason = "payload dry_run=true"
    elif not master:
        reason = f"{SEND_ENV} is off"
    elif not email and not chat:
        reason = f"{SEND_ENV} is on but neither {EMAIL_ENV} nor {CHAT_ENV} is on"
    else:
        reason = ""
    return {"dry_run": dry_run, "email": email, "chat": chat, "reason": reason}


def _read_lead(payload: dict[str, Any], key: str, env_name: str, default: int) -> int:
    raw = payload.get(key)
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get(env_name, "")
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise HolidayScheduleError(f"{key} must be an integer: {raw!r}") from exc


def resolve_leads(payload: dict[str, Any] | None) -> tuple[int, int]:
    payload = dict(payload or {})
    heads_up_days = _read_lead(payload, "heads_up_days", HEADS_UP_ENV, LEAD_DAYS_HEADS_UP)
    nudge_days = _read_lead(payload, "nudge_days", NUDGE_ENV, LEAD_DAYS_NUDGE)
    _check_leads(heads_up_days, nudge_days)
    return heads_up_days, nudge_days


def resolve_today(payload: dict[str, Any] | None) -> date:
    payload = dict(payload or {})
    raw = str(payload.get("today") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise HolidayScheduleError(f"today must be YYYY-MM-DD: {raw!r}") from exc
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def fetch_holiday_doc_html(*, drive: Any = None, doc_id: str = HOLIDAY_DOC_ID) -> str:
    """Export the live Doc as HTML so smart-chip dates survive."""
    drive = drive or common.drive_client()
    raw = drive.files().export(fileId=doc_id, mimeType="text/html").execute()
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    else:
        text = str(raw or "")
    if "<table" not in text.casefold():
        raise HolidayScheduleError("Drive HTML export has no table; refusing to guess dates")
    return text


class HolidaySendLedger:
    """Send ledger in the job database. One row per (event_date, status_kind, phase)."""

    def __init__(self, db_path: str) -> None:
        self.path = str(assert_durable_path(db_path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS holiday_alert_sends (
                    event_date TEXT NOT NULL,
                    status_kind TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    holiday TEXT NOT NULL,
                    close_time TEXT,
                    email_message_id TEXT,
                    chat_posted INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (event_date, status_kind, phase)
                )
                """
            )

    def _key(self, alert: HolidayAlert) -> tuple[str, str, str]:
        if alert.phase not in {PHASE_HEADS_UP, PHASE_NUDGE}:
            raise HolidayScheduleError("ledger row requires phase heads_up or nudge")
        return (alert.event_date.isoformat(), alert.status_kind, alert.phase)

    def get(self, alert: HolidayAlert) -> dict[str, Any] | None:
        event_date, status_kind, phase = self._key(alert)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM holiday_alert_sends
                   WHERE event_date=? AND status_kind=? AND phase=?""",
                (event_date, status_kind, phase),
            ).fetchone()
        return dict(row) if row else None

    def claim(self, alert: HolidayAlert) -> str:
        """Return claimed, sent, or pending. pending means do not send again."""
        event_date, status_kind, phase = self._key(alert)
        stamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT state FROM holiday_alert_sends
                   WHERE event_date=? AND status_kind=? AND phase=?""",
                (event_date, status_kind, phase),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO holiday_alert_sends
                       (event_date, status_kind, phase, holiday, close_time,
                        email_message_id, chat_posted, state, sent_at)
                       VALUES (?,?,?,?,?, '', 0, 'pending', ?)""",
                    (
                        event_date,
                        status_kind,
                        phase,
                        alert.holiday,
                        alert.close_time,
                        stamp,
                    ),
                )
                conn.commit()
                return "claimed"
            conn.commit()
            return "sent" if row["state"] == "sent" else "pending"

    def note_email(self, alert: HolidayAlert, message_id: str) -> None:
        event_date, status_kind, phase = self._key(alert)
        with self._connect() as conn:
            conn.execute(
                """UPDATE holiday_alert_sends SET email_message_id=?, sent_at=?
                   WHERE event_date=? AND status_kind=? AND phase=?""",
                (
                    message_id,
                    datetime.now(timezone.utc).isoformat(),
                    event_date,
                    status_kind,
                    phase,
                ),
            )

    def complete(self, alert: HolidayAlert, *, email_message_id: str, chat_posted: bool) -> None:
        event_date, status_kind, phase = self._key(alert)
        with self._connect() as conn:
            conn.execute(
                """UPDATE holiday_alert_sends
                   SET state='sent', email_message_id=?, chat_posted=?, sent_at=?
                   WHERE event_date=? AND status_kind=? AND phase=?""",
                (
                    email_message_id,
                    1 if chat_posted else 0,
                    datetime.now(timezone.utc).isoformat(),
                    event_date,
                    status_kind,
                    phase,
                ),
            )

    def release(self, alert: HolidayAlert) -> None:
        event_date, status_kind, phase = self._key(alert)
        with self._connect() as conn:
            conn.execute(
                """DELETE FROM holiday_alert_sends
                   WHERE event_date=? AND status_kind=? AND phase=? AND state='pending'
                     AND COALESCE(email_message_id, '')='' AND chat_posted=0""",
                (event_date, status_kind, phase),
            )


def resolve_ledger_path(payload: dict[str, Any] | None) -> str:
    payload = dict(payload or {})
    explicit = str(payload.get("ledger_db") or os.environ.get("ROBIE_JOB_DB") or "").strip()
    if explicit:
        return explicit
    return "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"


def _load_document(payload: dict[str, Any], channels: dict[str, Any]) -> str:
    fixture = str(payload.get("schedule_html") or "").strip()
    live_send = bool(channels["email"] or channels["chat"])
    allow_fixture = _truthy(os.environ.get(FIXTURE_ENV))
    if fixture and (not live_send or allow_fixture):
        return fixture
    doc_id = str(payload.get("document_id") or HOLIDAY_DOC_ID).strip() or HOLIDAY_DOC_ID
    if doc_id != HOLIDAY_DOC_ID and not allow_fixture:
        raise HolidayScheduleError(
            "refusing a non-default holiday document id without "
            f"{FIXTURE_ENV}"
        )
    return fetch_holiday_doc_html(doc_id=doc_id)


def _deliver(alert: HolidayAlert, channels: dict[str, Any], ledger: HolidaySendLedger) -> dict[str, Any]:
    existing = ledger.get(alert) or {}
    email_id = str(existing.get("email_message_id") or "")
    chat_posted = bool(existing.get("chat_posted"))
    if channels["email"] and not email_id:
        email_id = common.send_gmail(
            sender=SENDER,
            to=[EMAIL_TO],
            subject=email_subject(alert),
            body=email_body(alert),
        )
        ledger.note_email(alert, email_id)
    if channels["chat"] and not chat_posted:
        chat_posted = bool(common.post_chat_webhook(chat_text(alert)))
        if not chat_posted:
            raise RuntimeError(f"chat webhook did not return 2xx for {alert.ledger_key()}")
    ledger.complete(alert, email_message_id=email_id, chat_posted=chat_posted)
    return {"email_message_id": email_id, "chat_posted": chat_posted, "alert": alert.as_dict()}


class HolidayAlertWorker:
    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        del idempotency_key
        try:
            return self._perform(dict(job.get("payload") or {}))
        except HolidayScheduleError as exc:
            return WorkerResult(
                False,
                ACTION_TYPE,
                {"task": TASK_NAME},
                retryable=False,
                error=f"Holiday schedule parse failed closed: {exc}",
            )
        except Exception as exc:
            return WorkerResult(
                False,
                ACTION_TYPE,
                {"task": TASK_NAME},
                retryable=True,
                error=f"Holiday alert failed: {type(exc).__name__}: {exc}",
            )

    def _perform(self, payload: dict[str, Any]) -> WorkerResult:
        channels = resolve_channels(payload)
        today = resolve_today(payload)
        heads_up_days, nudge_days = resolve_leads(payload)
        document = _load_document(payload, channels)
        rows, alerts = load_schedule_alerts(document)
        due = select_due(alerts, today, heads_up_days=heads_up_days, nudge_days=nudge_days)
        destination: dict[str, Any] = {
            "dry_run": channels["dry_run"],
            "dry_run_reason": channels["reason"],
            "parsed_row_count": len(rows),
            "parsed_event_count": len(alerts),
            "today": today.isoformat(),
            "heads_up_days": heads_up_days,
            "nudge_days": nudge_days,
            "email_enabled": channels["email"],
            "chat_enabled": channels["chat"],
            "email_to": EMAIL_TO,
            "doc_url": DOC_URL,
            "planned": [],
            "email_message_ids": [],
            "chat_posted_keys": [],
            "skipped_already_sent": [],
            "held_pending": [],
        }
        if channels["dry_run"]:
            destination["planned"] = [alert.as_dict() for alert in due]
            for alert in due:
                print(
                    "HOLIDAY_ALERT_DRY_RUN "
                    f"{alert.ledger_key()} {alert.holiday} "
                    f"subject={email_subject(alert)!r}"
                )
            return WorkerResult(
                True,
                ACTION_TYPE,
                destination,
                detail={
                    "mode": "dry_run",
                    "reason": channels["reason"],
                    "due": len(due),
                    "parsed_event_count": len(alerts),
                },
                retryable=True,
            )

        ledger = HolidaySendLedger(resolve_ledger_path(payload))
        sent: list[dict[str, Any]] = []
        for alert in due:
            state = ledger.claim(alert)
            if state == "sent":
                destination["skipped_already_sent"].append(alert.ledger_key())
                continue
            if state == "pending":
                existing = ledger.get(alert) or {}
                if not existing.get("email_message_id") and not existing.get("chat_posted"):
                    destination["held_pending"].append(alert.ledger_key())
                    continue
            try:
                receipt = _deliver(alert, channels, ledger)
            except Exception:
                ledger.release(alert)
                raise
            sent.append(receipt)
            if receipt["email_message_id"]:
                destination["email_message_ids"].append(receipt["email_message_id"])
            if receipt["chat_posted"]:
                destination["chat_posted_keys"].append(alert.ledger_key())
            destination["planned"].append(alert.as_dict())
        if destination["held_pending"]:
            return WorkerResult(
                False,
                ACTION_TYPE,
                destination,
                retryable=False,
                error=(
                    "holiday alert ledger holds pending sends without receipts: "
                    + ", ".join(destination["held_pending"])
                    + ". Refusing to send again."
                ),
            )
        return WorkerResult(
            True,
            ACTION_TYPE,
            destination,
            detail={
                "mode": "send",
                "sent": len(sent),
                "skipped_already_sent": len(destination["skipped_already_sent"]),
                "parsed_event_count": len(alerts),
            },
            retryable=True,
        )


class HolidayAlertVerifier:
    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        del job
        destination = action.get("destination") or {}
        parsed_rows = int(destination.get("parsed_row_count") or 0)
        parsed_events = int(destination.get("parsed_event_count") or 0)
        captured = datetime.now(tz=timezone.utc).isoformat()
        expected = {
            "parsed_row_count": parsed_rows,
            "parsed_event_count": parsed_events,
            "dry_run": bool(destination.get("dry_run")),
        }
        if parsed_rows < 1 or parsed_events < 1:
            observed = {"parsed_row_count": parsed_rows, "parsed_event_count": parsed_events}
            return VerificationResult(
                False,
                VerificationEvidence(
                    method="holiday_schedule_parse",
                    source=DOC_URL,
                    expected=expected,
                    observed=observed,
                    authoritative=True,
                    captured_at=captured,
                    locator=DOC_URL,
                ),
                retryable=False,
                error="Holiday schedule parse produced no rows; refusing to verify",
                hold_status=JobStatus.FAILED,
            )
        if destination.get("dry_run"):
            observed = {
                "dry_run": True,
                "planned": len(destination.get("planned") or []),
                "parsed_row_count": parsed_rows,
                "parsed_event_count": parsed_events,
                "reason": destination.get("dry_run_reason") or "",
            }
            return VerificationResult(
                True,
                VerificationEvidence(
                    method="holiday_schedule_dry_run",
                    source=DOC_URL,
                    expected=expected,
                    observed=observed,
                    authoritative=True,
                    captured_at=captured,
                    locator=DOC_URL,
                ),
            )

        email_ids = [str(item) for item in destination.get("email_message_ids") or []]
        planned = destination.get("planned") or []
        email_ok = True
        if destination.get("email_enabled"):
            email_ok = len(email_ids) == len(planned) and all(
                common.gmail_message_exists(message_id, sender=SENDER) for message_id in email_ids
            )
        chat_ok = True
        if destination.get("chat_enabled"):
            chat_ok = len(destination.get("chat_posted_keys") or []) == len(planned)
        verified = bool(email_ok and chat_ok)
        observed = {
            "dry_run": False,
            "email_ok": email_ok,
            "chat_ok": chat_ok,
            "email_message_ids": email_ids,
            "skipped_already_sent": destination.get("skipped_already_sent") or [],
        }
        return VerificationResult(
            verified,
            VerificationEvidence(
                method="holiday_alert_gmail_and_chat_readback",
                source="gmail.users.messages.get + chat webhook status",
                expected={**expected, "planned": len(planned)},
                observed=observed,
                authoritative=True,
                captured_at=captured,
                locator=(
                    f"https://mail.google.com/mail/#all/{email_ids[0]}" if email_ids else DOC_URL
                ),
            ),
            retryable=not verified,
            error=None if verified else f"Holiday alert verification failed: {observed}",
            hold_status=None if verified else JobStatus.FAILED,
        )
