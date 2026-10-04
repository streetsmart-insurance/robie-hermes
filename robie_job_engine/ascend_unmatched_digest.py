"""Daily plain-English list of Ascend notices that matched no single EZLynx client.

The 15-minute poll upserts each unmatched notice into the API store. This
module turns the open rows into one weekday email for the accounting team.
Dry-run is the default: the email is printed to the journal unless
``ASCEND_UNMATCHED_DIGEST_LIVE=1``. An empty list sends nothing.

Near matches are suggestion text only. Nothing in this module files a note,
creates a discussion, or writes a policy.

Policy index
------------
EZLynx PolicyApi search ignores ApplicantName, Email, and PhoneNumber and
returns the whole book (totalSize around 38,000). Those parameters are not
used. PolicyNumber search is exact, so generating every one-character
variant would be hundreds of calls per notice.

The bounded read is one paged GET of the book:

    GET /PolicyApi/policy/v1/search?pageIndex={n}&pageSize=100

Pages are 1-based, matching the ``pageIndex`` field on the search envelope.
Calls stop once ``totalSize`` is covered by the raw rows fetched, on an
empty page, when a page repeats (paging ignored), at 2,000 pages, or at
the 20-minute budget. A short page is not treated as the end unless an
empty page follows or ``totalSize`` is already covered. A missing
``totalSize`` never counts as a complete book on a guess. Rows that lack
a policy number or client id still count toward ``totalSize``; only
usable rows are stored for suggestions. HTTP 429 and 5xx back off. One
401 clears the cached token and grants again. Each page otherwise waits
0.25 seconds. A 38,000-policy book at 100 rows a page is 380 read-only
calls, once on a weekday morning when the saved index is older than 20
hours. The 15-minute poll does not page the book: a suggestion is a local
comparison against the last complete index, which is zero extra PolicyApi
calls. An incomplete index produces no suggestions. A suggestion requires
exactly one EZLynx policy one character off whose client name also matches.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .ascend_api_notice_source import (
    CATCHUP_REVIEW,
    FILE_ATTEMPT_LIMIT,
    EventKeyStore,
    _iso,
    _now,
    cents_to_money,
    db_path,
    dry_run_db_path,
    live_db_path,
    parse_time,
    policy_numbers_of,
)
from .ascend_notice_driver import (
    POLICY_OUTCOME_INCOMPLETE,
    POLICY_OUTCOME_MULTIPLE,
    POLICY_OUTCOME_NO_NUMBER,
    POLICY_OUTCOME_NOT_IN_EZLYNX,
    _IDENTITY_NAME_KEYS,
    _APPLICANT_ID_KEYS,
    _first_present,
    _page_total,
    _policy_rows,
    _resolve_by_policy_numbers,
    _row_policy_number,
    _rows_matching_policy,
    insured_name_cores,
    insured_names_match,
)
from .ascend_notice_triage import _NOTICE_HEADINGS

logger = logging.getLogger(__name__)

LIVE_ENV = "ASCEND_UNMATCHED_DIGEST_LIVE"
STATE_ENV = "ASCEND_UNMATCHED_DIGEST_STATE"
DB_ENV = "ASCEND_UNMATCHED_DIGEST_DB"
MARKER_ENV = "ASCEND_UNMATCHED_DIGEST_MARKER"
TIMER_UNIT = "robie-ascend-unmatched-digest.timer"
ACCOUNTING_TO = "accounting@streetsmart.insurance"
EASTERN = ZoneInfo("America/New_York")
WINDOW = timedelta(days=14)
INDEX_STALE = timedelta(hours=20)
INDEX_PAGE_SIZE = 100
# 38k policies at 100 per page is 380 calls. A smaller honored page (30)
# is about 1,300 calls. 2,000 leaves headroom and still stops a runaway.
INDEX_MAX_PAGES = 2000
INDEX_PAUSE_S = 0.25
# Standalone index refresh budget. A digest run uses RUN_DEADLINE_S for
# paging and the re-check together.
INDEX_TIME_BUDGET_S = 20 * 60
# Shared by index paging and the per-row re-check. TimeoutStartSec is 45
# minutes, so 30 minutes still leaves room to write the state file and send.
RUN_DEADLINE_S = 30 * 60
# One 401 re-grant, then these waits on 429 and 5xx.
PAGE_BACKOFF_S = (1.0, 2.0, 4.0)
DEADLINE_HOUR = 9
DEADLINE_MINUTE = 30
DEFAULT_STATE_PATH = Path(
    "/opt/streetsmart-hermes/robie-job-engine/data/ascend-api/"
    "unmatched-digest-last-run.json"
)
DEFAULT_MARKER_PATH = Path(
    "/opt/streetsmart-hermes/robie-job-engine/data/ascend-api/"
    "unmatched-digest-installed"
)
STORE_FILE_MODE = 0o644
STORE_DIR_NAME = "ascend-api"
STORE_DIR_MODE = 0o755

REASONS = frozenset(
    {
        POLICY_OUTCOME_NO_NUMBER,
        POLICY_OUTCOME_NOT_IN_EZLYNX,
        POLICY_OUTCOME_MULTIPLE,
        POLICY_OUTCOME_INCOMPLETE,
        CATCHUP_REVIEW,
    }
)

_REASON_WORDS = {
    POLICY_OUTCOME_NO_NUMBER: "Ascend did not include a policy number.",
    POLICY_OUTCOME_NOT_IN_EZLYNX: "That policy number is not in EZLynx.",
    POLICY_OUTCOME_MULTIPLE: "More than one EZLynx client matched.",
    POLICY_OUTCOME_INCOMPLETE: "The EZLynx search did not finish, so Robie did not guess.",
    CATCHUP_REVIEW: (
        "This notice is too old to file automatically, please check by hand."
    ),
}

_NOTICE_WORDS = {
    "late_payment": "past-due payment",
    "intent_to_cancel": "intent to cancel",
    "cancellation": "cancellation",
    "reinstatement": "reinstatement",
    "paid_off": "loan paid off",
    "payment_confirmation": "payment",
    "disputed_charge": "disputed charge",
    "return_premium": "return premium",
    "refund": "refund",
    "agency_remittance": "agency remittance",
    "new_program": "new program",
    "processing_payment": "processing payment",
    "underwriting": "underwriting",
}

_POLICY_IGNORABLE = re.compile(r"[\s\-\u2010\u2011\u2012\u2013\u2014]+")
_FIX_LINE_CHECKED = (
    "Fix the policy number in EZLynx or Ascend and it drops off this list "
    "once Robie matches it and files the note."
)
_FIX_LINE_AS_SHOWN = (
    "Fix the policy number in EZLynx and it drops off this list "
    "once Robie matches it and files the note. "
    "The policy number is as Ascend sent it."
)
_READY_WHEN_NOTES_OFF = "Ready, will file once Robie's Ascend notes are on."
_READY_WHEN_NOTES_ON = (
    "Robie matched one client and will file this on the next Ascend run."
)
_READY_WHEN_NOTES_ABSENT = "Robie's Ascend notes haven't run recently."
_HANDED_BACK = (
    "Robie matched this but couldn't file it — please file by hand."
)
# A poll older than this is not evidence that notes are on.
POLL_FRESHNESS = timedelta(hours=2)
_POLICY_ID_LINE = re.compile(r"(?i)^policy id\s+\S+")


def live_enabled() -> bool:
    """True only for the exact env value ``1``."""
    return str(os.environ.get(LIVE_ENV) or "").strip() == "1"


def state_path() -> Path:
    override = str(os.environ.get(STATE_ENV) or "").strip()
    if override:
        return Path(override)
    return DEFAULT_STATE_PATH


def marker_path() -> Path:
    """Installer marker. Present only after --enable-timer or --live."""
    override = str(os.environ.get(MARKER_ENV) or "").strip()
    if override:
        return Path(override)
    return DEFAULT_MARKER_PATH


def timer_is_enabled() -> bool:
    """True when systemd reports the weekday digest timer as enabled."""
    try:
        completed = subprocess.run(
            ["systemctl", "is-enabled", TIMER_UNIT],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def digest_watch_installed() -> bool:
    """Health watches the digest only after install, not on a missing file.

    The installer writes the marker on --enable-timer and --live and removes
    it on rollback. --dry-run-once writes nothing. An enabled timer counts
    even if the marker was removed by hand.
    """
    try:
        if marker_path().is_file():
            return True
    except OSError:
        return False
    return timer_is_enabled()


def policy_compare_key(value: str) -> str:
    """Uppercase, and drop spaces and dashes. Used only for near matches."""
    return _POLICY_IGNORABLE.sub("", str(value or "")).upper()


def one_character_apart(left: str, right: str) -> bool:
    """One substitution, insertion, deletion, or adjacent swap. Equals are not close."""
    if not left or not right or left == right:
        return False
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        diffs = [index for index, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(diffs) == 1:
            return True
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
            first, second = diffs
            return left[first] == right[second] and left[second] == right[first]
        return False
    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    left_index = right_index = skipped = 0
    while left_index < len(longer) and right_index < len(shorter):
        if longer[left_index] == shorter[right_index]:
            left_index += 1
            right_index += 1
            continue
        skipped += 1
        if skipped > 1:
            return False
        left_index += 1
    return True


@dataclass(frozen=True)
class NearMatch:
    """Suggestion text. Never an applicant id that gets filed."""

    client_name: str
    policy_number: str
    applicant_id: str


def suggest_near_match(
    index_rows: list[dict[str, Any]],
    policy_numbers: list[str],
    insured_name: str,
) -> NearMatch | None:
    """Exactly one EZLynx policy is one character off, and that client's name matches.

    Count one-character policies first. Two policies one character off return
    nothing even when only one of those names matches. Zero candidates return
    nothing. The caller must pass a complete index. This function does not
    call EZLynx.
    """
    if not index_rows or not insured_name_cores(insured_name):
        return None
    wanted = [policy_compare_key(number) for number in policy_numbers]
    wanted = [key for key in wanted if key]
    if not wanted:
        return None
    found: dict[tuple[str, str], NearMatch] = {}
    for row in index_rows:
        key = policy_compare_key(str(row.get("policy_number") or ""))
        if not key or not any(one_character_apart(key, target) for target in wanted):
            continue
        applicant_id = str(row.get("applicant_id") or "").strip()
        if not applicant_id:
            continue
        found[(applicant_id, key)] = NearMatch(
            client_name=str(row.get("applicant_name") or "").strip(),
            policy_number=str(row.get("policy_number") or "").strip(),
            applicant_id=applicant_id,
        )
    if len(found) != 1:
        return None
    match = next(iter(found.values()))
    if not insured_names_match(insured_name, match.client_name):
        return None
    return match


def fallback_reason(raw: str, policy_numbers: list[str] | tuple[str, ...]) -> str:
    """Map an older unresolved string when the policy step did not record one."""
    text = str(raw or "").lower()
    if "incomplete" in text or "lacks account" in text:
        return POLICY_OUTCOME_INCOMPLETE
    if "ambiguous" in text:
        return POLICY_OUTCOME_MULTIPLE
    match = re.search(r"(\d+)\s+candidate", text)
    if match and int(match.group(1)) > 1:
        return POLICY_OUTCOME_MULTIPLE
    numbers = [str(number or "").strip() for number in policy_numbers if str(number or "").strip()]
    if not numbers:
        return POLICY_OUTCOME_NO_NUMBER
    return POLICY_OUTCOME_NOT_IN_EZLYNX


def persist_unmatched_notice(
    store: EventKeyStore,
    notice: Any,
    outcome: dict[str, Any],
    *,
    seen_at: str,
) -> str:
    """Upsert one ask-list notice. A near match is stored as text, never filed."""
    detail = outcome.get("detail") if isinstance(outcome.get("detail"), dict) else {}
    reason = str(detail.get("unmatched_reason") or "").strip()
    if reason not in REASONS:
        reason = fallback_reason(str(outcome.get("reason") or ""), notice.policy_numbers)
    suggestion: NearMatch | None = None
    update_suggestion = False
    if reason == POLICY_OUTCOME_NOT_IN_EZLYNX:
        rows, meta = store.policy_index()
        if int(meta.get("complete") or 0) == 1:
            suggestion = suggest_near_match(
                rows, list(notice.policy_numbers), str(notice.insured_name or "")
            )
            update_suggestion = True
    store.upsert_unmatched(
        notice,
        reason=reason,
        seen_at=seen_at,
        suggestion_client=suggestion.client_name if suggestion else "",
        suggestion_policy=suggestion.policy_number if suggestion else "",
        update_suggestion=update_suggestion,
    )
    return reason


def resolve_unmatched_notice(store: EventKeyStore, event_key: str, seen_at: str) -> None:
    """Close the accounting row after a later run matches and files the notice."""
    store.resolve_unmatched(event_key, seen_at)


def exact_applicant_from_index(
    index_rows: list[dict[str, Any]],
    policy_numbers: list[str],
) -> str | None:
    """One applicant id when the saved index has an exact policy hit.

    The same rule as PolicyApi search: exact normalized number, then a
    term-suffix match. Two rows or two applicants return nothing.
    """
    numbers = [str(number or "").strip() for number in policy_numbers if str(number or "").strip()]
    if not numbers or not index_rows:
        return None
    found: list[str] = []
    for number in numbers:
        matched = _rows_matching_policy(index_rows, number)
        if not matched:
            continue
        if len(matched) != 1:
            return None
        applicant_id = _first_present(matched[0], _APPLICANT_ID_KEYS)
        if not applicant_id:
            return None
        found.append(applicant_id)
    unique = set(found)
    if len(unique) != 1:
        return None
    return found[0]


def _policy_numbers_of(row: dict[str, Any]) -> list[str]:
    raw = row.get("policy_numbers") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(number).strip() for number in raw if str(number or "").strip()]


class PolicyBudgetExpired(Exception):
    """Index paging and the re-check share one deadline. Time ran out."""


class _BackoffPolicySearch:
    """Policy-number search with the same backoff the page loop uses.

    ``_resolve_by_policy_numbers`` swallows search errors, so a deadline
    stop is recorded on ``expired`` and re-raised by the caller.
    """

    def __init__(
        self,
        client: Any,
        *,
        pause: Callable[[float], None],
        deadline: float | None,
        clock: Callable[[], float] | None,
    ) -> None:
        self._client = client
        self._pause = pause
        self._deadline = deadline
        self._clock = clock
        self.expired = False

    def search_policy_by_number(self, number: str) -> Any:
        if self._past_deadline():
            self.expired = True
            raise PolicyBudgetExpired()
        try:
            payload, _attempts = call_with_policy_backoff(
                self._client,
                lambda: self._client.search_policy_by_number(number),
                pause=self._pause,
                deadline=self._deadline,
                clock=self._clock,
            )
        except PolicyBudgetExpired:
            self.expired = True
            raise
        return payload

    def _past_deadline(self) -> bool:
        if self._deadline is None:
            return False
        ticks = self._clock or time.monotonic
        return ticks() >= self._deadline


def notes_state_from_polls(
    stores: list[EventKeyStore], now: datetime
) -> str:
    """``live``, ``dry``, or ``absent`` from the newest poll in the last 2 hours.

    The digest unit does not receive ``ASCEND_API_SOURCE_LIVE``. The poll
    writes the flag on each run. No recent run is treated as off.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    latest: dict[str, Any] | None = None
    latest_at: datetime | None = None
    for store in stores:
        try:
            row = store.latest_poll_run()
        except Exception as exc:  # noqa: BLE001 - a bad store is not a recent run
            logger.warning("digest could not read a poll run: %s", type(exc).__name__)
            continue
        if not row:
            continue
        started = parse_time(row.get("started_at"))
        if started is None:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if latest_at is None or started > latest_at:
            latest = row
            latest_at = started
    if latest is None or latest_at is None:
        return "absent"
    if latest_at < now - POLL_FRESHNESS or latest_at > now + timedelta(minutes=5):
        return "absent"
    return "live" if latest.get("live") else "dry"


def _replace_policy_id_lines(body: str, numbers: list[str]) -> str:
    """Put the policy numbers just read from Ascend on the stored notice."""
    lines = str(body or "").splitlines()
    rewritten: list[str] = []
    inserted = False
    for line in lines:
        if _POLICY_ID_LINE.match(line.strip()):
            if not inserted:
                rewritten.extend(f"Policy ID {number}" for number in numbers)
                inserted = True
            continue
        rewritten.append(line)
    if not inserted:
        rewritten = [f"Policy ID {number}" for number in numbers] + rewritten
    return "\n".join(rewritten)


def _read_program_policy_numbers(
    row: dict[str, Any],
    ascend_client: Any | None,
    *,
    deadline: float | None,
    clock: Callable[[], float] | None,
    persist: bool,
) -> list[str] | None:
    """The program's current policy numbers. Read only. None keeps the saved ones.

    Uses the same deadline as the EZLynx re-check. A failed read keeps the
    numbers saved when the notice was first seen. The store is updated only
    when this digest run is persisting. A dry run still uses the fresh
    numbers for this email.
    """
    if ascend_client is None:
        return None
    program_id = str(row.get("program_id") or "").strip()
    if not program_id:
        return None
    getter = getattr(ascend_client, "get_program", None)
    if not callable(getter):
        return None
    ticks = clock or time.monotonic
    if deadline is not None and ticks() >= deadline:
        raise PolicyBudgetExpired()
    try:
        payload = getter(program_id)
    except PolicyBudgetExpired:
        raise
    except Exception as exc:  # noqa: BLE001 - keep the saved number and continue
        logger.warning(
            "digest recheck could not read Ascend program %s: %s",
            program_id,
            type(exc).__name__,
        )
        return None
    record = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        record = payload["data"]
    if not isinstance(record, dict):
        return None
    numbers = [str(number).strip() for number in policy_numbers_of(record) if str(number).strip()]
    if not numbers:
        # Program records do not carry a policy number. The billable does.
        # This read does not use the poll's cache, so a correction in Ascend
        # is visible on the next digest. The same deadline covers this GET.
        billable_getter = getattr(ascend_client, "get_billables", None)
        if not callable(billable_getter):
            return None
        if deadline is not None and ticks() >= deadline:
            raise PolicyBudgetExpired()
        try:
            billable_payload = billable_getter(program_id)
        except PolicyBudgetExpired:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the saved number and continue
            logger.warning(
                "digest recheck could not read Ascend billables for %s: %s",
                program_id,
                type(exc).__name__,
            )
            return None
        rows = billable_payload.get("data") if isinstance(billable_payload, dict) else None
        if not isinstance(rows, list):
            rows = []
        record = dict(record)
        record["billables"] = [row for row in rows if isinstance(row, dict)]
        numbers = [
            str(number).strip() for number in policy_numbers_of(record) if str(number).strip()
        ]
    if not numbers:
        return None
    row["policy_numbers"] = numbers
    body = _replace_policy_id_lines(str(row.get("body") or ""), numbers)
    row["body"] = body
    encoded = json.dumps(record, default=str)
    row["program_json"] = encoded
    row["_policy_rechecked"] = True
    store = row.get("_store")
    key = str(row.get("event_key") or "").strip()
    if persist and isinstance(store, EventKeyStore) and key:
        try:
            store.refresh_unmatched_policy(key, numbers, body, encoded)
        except Exception as exc:  # noqa: BLE001 - the search can still use the new numbers
            logger.warning(
                "digest recheck could not store the Ascend policy number: %s",
                type(exc).__name__,
            )
    return numbers


def _resolve_open_row(
    row: dict[str, Any],
    *,
    ezlynx_client: Any | None,
    ascend_client: Any | None,
    index_rows: list[dict[str, Any]],
    index_complete: bool,
    pause: Callable[[float], None],
    deadline: float | None,
    clock: Callable[[], float] | None,
    persist: bool,
) -> bool:
    """True when this open row now belongs to exactly one EZLynx client.

    A live PolicyApi search wins and uses the paging backoff. Zero rows,
    several clients, or a failed search stay open and do not fall back to
    the saved index. With no client, only a complete index can match the
    row. Nothing here files a note or writes a policy.
    """
    fresh = _read_program_policy_numbers(
        row, ascend_client, deadline=deadline, clock=clock, persist=persist
    )
    numbers = fresh if fresh else _policy_numbers_of(row)
    if not numbers:
        return False
    if ezlynx_client is not None:
        wrapped = _BackoffPolicySearch(
            ezlynx_client, pause=pause, deadline=deadline, clock=clock
        )
        try:
            resolution, _reason, _fall_through = _resolve_by_policy_numbers(
                wrapped, numbers
            )
        except PolicyBudgetExpired:
            raise
        except Exception as exc:  # noqa: BLE001 - leave the row on the list
            logger.warning("digest recheck search failed: %s", type(exc).__name__)
            return False
        if wrapped.expired:
            raise PolicyBudgetExpired()
        return resolution is not None
    if not index_complete:
        return False
    return exact_applicant_from_index(index_rows, numbers) is not None


def recheck_open_unmatched(
    rows: list[dict[str, Any]],
    *,
    ezlynx_client: Any | None,
    index_rows: list[dict[str, Any]],
    index_complete: bool,
    now: datetime,
    persist: bool,
    notes_state: str,
    pause: Callable[[float], None] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] | None = None,
    ascend_client: Any | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Mark an exact one-client match ready to file. Leave every row open.

    A live digest writes ``ready_at``. A dry run does not write ready or
    resolved state. Filing waits for the API poll. When the deadline has
    passed, the remaining rows stay open and unmarked.
    """
    moment = _iso(now)
    sleeper = pause or time.sleep
    ticks = clock or time.monotonic
    still_open: list[dict[str, Any]] = []
    ready = 0
    stopped = False
    for row in rows:
        # Older than the catch-up window stays on the email for a person.
        # The digest does not mark it ready, so the poll does not file it.
        if str(row.get("reason") or "") == CATCHUP_REVIEW:
            still_open.append(row)
            continue
        if stopped or (deadline is not None and ticks() >= deadline):
            stopped = True
            still_open.append(row)
            continue
        try:
            matched = _resolve_open_row(
                row,
                ezlynx_client=ezlynx_client,
                ascend_client=ascend_client,
                index_rows=index_rows,
                index_complete=index_complete,
                pause=sleeper,
                deadline=deadline,
                clock=ticks,
                persist=persist,
            )
        except PolicyBudgetExpired:
            stopped = True
            still_open.append(row)
            continue
        if not matched:
            still_open.append(row)
            continue
        row["ready_to_file"] = True
        row["notes_state"] = notes_state
        row["api_notes_live"] = notes_state == "live"
        if not persist:
            still_open.append(row)
            continue
        store = row.get("_store")
        key = str(row.get("event_key") or "").strip()
        if not isinstance(store, EventKeyStore) or not key:
            still_open.append(row)
            continue
        try:
            store.mark_ready_to_file(key, moment)
        except Exception as exc:  # noqa: BLE001 - keep the row on the email
            logger.warning("digest recheck could not mark %s ready: %s", key, type(exc).__name__)
            still_open.append(row)
            continue
        row["ready_at"] = moment
        ready += 1
        still_open.append(row)
    return still_open, ready


def notice_words(event_type: str) -> str:
    key = str(event_type or "").strip().lower()
    if key in _NOTICE_WORDS:
        return _NOTICE_WORDS[key]
    heading = _NOTICE_HEADINGS.get(key)
    if heading:
        return str(heading).lower()
    return "notice"


def _policy_phrase(numbers: list[str]) -> str:
    cleaned = [str(number).strip() for number in numbers if str(number or "").strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return f"policy {cleaned[0]}"
    if len(cleaned) == 2:
        return f"policies {cleaned[0]} and {cleaned[1]}"
    return "policies " + ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def item_line(item: dict[str, Any]) -> str:
    """One plain sentence. No field names and no code labels."""
    name = str(item.get("insured_name") or "").strip() or "An insured"
    parts = [name, notice_words(str(item.get("notice_type") or ""))]
    money = cents_to_money(item.get("amount_cents"))
    if money:
        parts.append(money)
    phrase = _policy_phrase(list(item.get("policy_numbers") or []))
    if phrase:
        parts.append(phrase)
    if str(item.get("reason") or "") == CATCHUP_REVIEW:
        return ", ".join(parts) + ". " + _REASON_WORDS[CATCHUP_REVIEW]
    if int(item.get("file_attempts") or 0) >= FILE_ATTEMPT_LIMIT:
        reason = str(item.get("file_failure") or "").strip() or "The note was not filed."
        return ", ".join(parts) + ". " + _HANDED_BACK + " " + reason
    if item.get("ready_to_file") or str(item.get("ready_at") or "").strip():
        state = str(item.get("notes_state") or "")
        if not state:
            state = "live" if item.get("api_notes_live") else "absent"
        if state == "live":
            waiting = _READY_WHEN_NOTES_ON
        elif state == "dry":
            waiting = _READY_WHEN_NOTES_OFF
        else:
            waiting = _READY_WHEN_NOTES_ABSENT
        return ", ".join(parts) + ". " + waiting
    reason = _REASON_WORDS.get(
        str(item.get("reason") or ""),
        "Robie could not match this notice to one client.",
    )
    sentence = ", ".join(parts) + ". " + reason
    client = str(item.get("suggestion_client") or "").strip()
    policy = str(item.get("suggestion_policy") or "").strip()
    if client and policy:
        sentence += f" Did you mean {client} (policy {policy})?"
    return sentence


def digest_subject(count: int, when: datetime) -> str:
    """Subject includes the ET date so the 24-hour same-subject guard can send tomorrow."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(EASTERN)
    stamp = f"{local.strftime('%B')} {local.day}, {local.year}"
    return f"Ascend notices Robie couldn't match ({count}) for {stamp}"


def render_digest(
    current: list[dict[str, Any]],
    aged: list[dict[str, Any]],
    *,
    when: datetime | None = None,
    policy_rechecked: bool = False,
) -> tuple[str, str] | None:
    """Subject and body, or None when there is nothing to send."""
    if not current and not aged:
        return None
    count = len(current) if current else len(aged)
    subject = digest_subject(count, when or _now())
    lines = [item_line(item) for item in current]
    if aged:
        bits = []
        for item in aged:
            name = str(item.get("insured_name") or "").strip() or "An insured"
            bits.append(f"{name}, {notice_words(str(item.get('notice_type') or ''))}")
        lines.append("Still unmatched after 14 days: " + "; ".join(bits) + ".")
    lines.append(_FIX_LINE_CHECKED if policy_rechecked else _FIX_LINE_AS_SHOWN)
    return subject, "\n".join(lines)


def split_window(
    rows: list[dict[str, Any]],
    now: datetime,
    *,
    window: timedelta = WINDOW,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Open items first seen within 14 days, and newly aged-out items."""
    cutoff = now - window
    current: list[dict[str, Any]] = []
    aged: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("resolved_at") or "").strip():
            continue
        seen = parse_time(row.get("first_seen"))
        if seen is None or seen >= cutoff:
            current.append(row)
        elif not str(row.get("aged_out_notified_at") or "").strip():
            aged.append(row)
    current.sort(key=lambda item: (str(item.get("insured_name") or ""), str(item.get("event_key") or "")))
    aged.sort(key=lambda item: (str(item.get("insured_name") or ""), str(item.get("event_key") or "")))
    return current, aged


def _publish_state_file(path: Path) -> None:
    """Mode 0644 on the state file, and 0755 only on ``ascend-api``."""
    try:
        if path.is_file():
            os.chmod(path, STORE_FILE_MODE)
        parent = path.parent
        if parent.name == STORE_DIR_NAME and parent.is_dir():
            os.chmod(parent, STORE_DIR_MODE)
    except OSError as exc:
        logger.warning("could not publish digest state permissions: %s", type(exc).__name__)


def write_last_run(payload: dict[str, Any], path: Path | None = None) -> None:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _publish_state_file(target)


def digest_store_paths() -> list[Path]:
    """The poller's store, plus the other API file when it already exists.

    ``ASCEND_UNMATCHED_DIGEST_DB`` pins one file. Otherwise the dry-run file
    is opened (that is where the poller writes until it is live) and the
    live file is included only when it is already on disk. This does not
    set ``ASCEND_API_SOURCE_LIVE`` and does not create the live file.
    """
    override = str(os.environ.get(DB_ENV) or "").strip()
    if override:
        return [Path(override)]
    primary = db_path()
    paths = [primary]
    for extra in (live_db_path(), dry_run_db_path()):
        if extra not in paths and extra.is_file():
            paths.append(extra)
    return paths


def open_digest_stores() -> list[EventKeyStore]:
    return [EventKeyStore(path) for path in digest_store_paths()]


def collect_open(stores: list[EventKeyStore]) -> list[dict[str, Any]]:
    """Open rows across stores. A resolved copy of the same key wins."""
    by_key: dict[str, dict[str, Any]] = {}
    for store in stores:
        for row in store.list_unmatched():
            key = str(row.get("event_key") or "")
            if not key:
                continue
            row["_store"] = store
            current = by_key.get(key)
            if current is None:
                by_key[key] = row
                continue
            current_resolved = bool(str(current.get("resolved_at") or "").strip())
            row_resolved = bool(str(row.get("resolved_at") or "").strip())
            if row_resolved and not current_resolved:
                by_key[key] = row
            elif current_resolved and not row_resolved:
                continue
            elif str(row.get("last_seen") or "") >= str(current.get("last_seen") or ""):
                by_key[key] = row
    return [row for row in by_key.values() if not str(row.get("resolved_at") or "").strip()]


def best_policy_index(
    stores: list[EventKeyStore],
) -> tuple[list[dict[str, Any]], dict[str, Any], EventKeyStore | None]:
    chosen_rows: list[dict[str, Any]] = []
    chosen_meta: dict[str, Any] = {}
    chosen_store: EventKeyStore | None = None
    for store in stores:
        rows, meta = store.policy_index()
        if int(meta.get("complete") or 0) == 1 and len(rows) >= len(chosen_rows):
            chosen_rows, chosen_meta, chosen_store = rows, meta, store
    return chosen_rows, chosen_meta, chosen_store


def index_is_fresh(meta: dict[str, Any], now: datetime) -> bool:
    if int(meta.get("complete") or 0) != 1:
        return False
    built = parse_time(meta.get("built_at"))
    if built is None:
        return False
    return now - built < INDEX_STALE


def rows_from_policy_page(
    payload: Any,
) -> tuple[list[dict[str, str]], int, int | None]:
    """Usable rows, the raw row count, and totalSize.

    A row with no policy number or no client id is not usable. It still
    counts in the raw total so a dropped row cannot keep the index incomplete.
    """
    total = _page_total(payload)
    raw = list(_policy_rows(payload))
    found: list[dict[str, str]] = []
    for row in raw:
        number = _row_policy_number(row)
        applicant_id = _first_present(row, _APPLICANT_ID_KEYS)
        name = _first_present(row, _IDENTITY_NAME_KEYS)
        key = policy_compare_key(number)
        if not key or not applicant_id:
            continue
        found.append(
            {
                "policy_key": key,
                "policy_number": number,
                "applicant_name": name,
                "applicant_id": applicant_id,
            }
        )
    return found, len(raw), total


def page_signature(payload: Any) -> tuple[tuple[str, str], ...]:
    """Identity of every raw row, including ones that cannot be suggested."""
    signature: list[tuple[str, str]] = []
    for row in _policy_rows(payload):
        number = policy_compare_key(_row_policy_number(row))
        applicant_id = str(_first_present(row, _APPLICANT_ID_KEYS) or "").strip()
        signature.append((number, applicant_id))
    return tuple(signature)


def call_with_policy_backoff(
    client: Any,
    call: Callable[[], Any],
    *,
    pause: Callable[[float], None],
    deadline: float | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[Any, int]:
    """One PolicyApi call. Back off on 429/5xx. Re-grant once on 401.

    Index paging and the digest re-check both use this. No policy is written.
    ``clear_cached_token`` is the only other client method. A passed
    ``deadline`` (monotonic) stops the wait instead of running long.
    """
    ticks = clock or time.monotonic
    reauthed = False
    backoff_used = 0
    attempts = 0
    while True:
        if deadline is not None and ticks() >= deadline:
            raise PolicyBudgetExpired()
        attempts += 1
        try:
            return call(), attempts
        except PolicyBudgetExpired:
            raise
        except Exception as exc:
            status = getattr(exc, "status", None)
            retryable = bool(getattr(exc, "retryable", False)) or status == 429 or (
                isinstance(status, int) and status >= 500
            )
            if status == 401 and not reauthed and _clear_cached_token(client):
                reauthed = True
                continue
            if retryable and status != 401 and backoff_used < len(PAGE_BACKOFF_S):
                wait = PAGE_BACKOFF_S[backoff_used]
                backoff_used += 1
                if deadline is not None and ticks() + wait > deadline:
                    raise PolicyBudgetExpired() from exc
                pause(wait)
                continue
            raise


def _clear_cached_token(client: Any) -> bool:
    method = getattr(client, "clear_cached_token", None)
    if not callable(method):
        return False
    method()
    return True


def fetch_policy_page(
    client: Any,
    page_index: int,
    page_size: int,
    *,
    pause: Callable[[float], None],
    deadline: float | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[Any, int]:
    """One PolicyApi page and how many GETs it took.

    Back off on 429/5xx. Re-grant once on 401. The only client methods used
    are ``search_policy_page`` and ``clear_cached_token``. No policy is written.
    """
    search = getattr(client, "search_policy_page", None)
    if search is None:
        raise RuntimeError("no_page_search")
    return call_with_policy_backoff(
        client,
        lambda: search(page_index, page_size),
        pause=pause,
        deadline=deadline,
        clock=clock,
    )


def refresh_policy_index(
    client: Any,
    stores: list[EventKeyStore],
    *,
    now: datetime | None = None,
    page_size: int = INDEX_PAGE_SIZE,
    max_pages: int = INDEX_MAX_PAGES,
    pause_s: float = INDEX_PAUSE_S,
    sleep: Callable[[float], None] | None = None,
    budget_s: float = INDEX_TIME_BUDGET_S,
    clock: Callable[[], float] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Page the policy book read-only. Save it only when the read is complete.

    The only method called is ``search_policy_page``. A repeated page means
    paging was ignored: the old index is kept and suggestions stay off until
    a complete read exists. Completeness uses the raw row count, not the
    usable subset. A short page without ``totalSize`` is not a complete book.
    """
    moment = now or _now()
    pause = sleep or time.sleep
    ticks = clock or time.monotonic
    started = ticks()
    limit = deadline if deadline is not None else started + budget_s
    calls = 0
    collected: list[dict[str, str]] = []
    raw_seen = 0
    previous: tuple[tuple[str, str], ...] | None = None
    total_size: int | None = None
    complete = False
    stopped = "page_cap"
    pages = 0
    search = getattr(client, "search_policy_page", None)
    if search is None:
        return {
            "calls": 0,
            "complete": False,
            "saved": False,
            "rows": 0,
            "raw_rows": 0,
            "total_size": None,
            "stopped": "no_page_search",
            "pages": 0,
        }
    while pages < max_pages:
        if ticks() >= limit:
            stopped = "time_budget"
            complete = False
            break
        pages += 1
        if pages > 1 and pause_s > 0:
            pause(pause_s)
        try:
            payload, attempts = fetch_policy_page(
                client, pages, page_size, pause=pause, deadline=limit, clock=ticks
            )
        except PolicyBudgetExpired:
            stopped = "time_budget"
            complete = False
            break
        calls += attempts
        rows, raw_count, total = rows_from_policy_page(payload)
        if total is not None:
            total_size = total
        signature = page_signature(payload)
        if previous is not None and signature == previous and signature:
            stopped = "paging_ignored"
            complete = False
            break
        previous = signature
        collected.extend(rows)
        raw_seen += raw_count
        if raw_count == 0:
            if total_size is not None and raw_seen < total_size:
                stopped = "short_of_total"
                complete = False
            else:
                stopped = "empty_page"
                complete = True
            break
        if total_size is not None and raw_seen >= total_size:
            stopped = "covered_total"
            complete = True
            break
    else:
        complete = total_size is not None and raw_seen >= total_size
        stopped = "covered_total" if complete else "page_cap"
    saved = False
    if complete and stores:
        built_at = _iso(moment)
        unique: dict[tuple[str, str], dict[str, str]] = {}
        for row in collected:
            unique[(row["policy_key"], row["applicant_id"])] = row
        saved_rows = list(unique.values())
        for store in stores:
            store.save_policy_index(
                saved_rows,
                built_at=built_at,
                calls=calls,
                total_size=total_size,
                pages=pages,
            )
        saved = True
    report = {
        "calls": calls,
        "complete": complete,
        "saved": saved,
        "rows": len(collected),
        "raw_rows": raw_seen,
        "total_size": total_size,
        "stopped": stopped,
        "pages": pages,
    }
    logger.info(
        "policy index refresh calls=%s complete=%s rows=%s total_size=%s stopped=%s",
        calls,
        complete,
        len(collected),
        total_size if total_size is not None else "unknown",
        stopped,
    )
    return report


def apply_suggestions(
    rows: list[dict[str, Any]],
    index_rows: list[dict[str, Any]],
    *,
    index_complete: bool,
) -> None:
    """Fill suggestion text from the local index. No EZLynx call and no filing."""
    if not index_complete:
        return
    for row in rows:
        if row.get("ready_to_file") or str(row.get("ready_at") or "").strip():
            continue
        if str(row.get("reason") or "") != POLICY_OUTCOME_NOT_IN_EZLYNX:
            row["suggestion_client"] = ""
            row["suggestion_policy"] = ""
            store = row.get("_store")
            if isinstance(store, EventKeyStore):
                store.set_unmatched_suggestion(str(row.get("event_key") or ""), "", "")
            continue
        match = suggest_near_match(
            index_rows,
            list(row.get("policy_numbers") or []),
            str(row.get("insured_name") or ""),
        )
        row["suggestion_client"] = match.client_name if match else ""
        row["suggestion_policy"] = match.policy_number if match else ""
        store = row.get("_store")
        if isinstance(store, EventKeyStore):
            store.set_unmatched_suggestion(
                str(row.get("event_key") or ""),
                row["suggestion_client"],
                row["suggestion_policy"],
            )


def _mark_aged(aged: list[dict[str, Any]], moment: str) -> None:
    by_store: dict[int, tuple[EventKeyStore, list[str]]] = {}
    for row in aged:
        store = row.get("_store")
        if not isinstance(store, EventKeyStore):
            continue
        bucket = by_store.setdefault(id(store), (store, []))
        bucket[1].append(str(row.get("event_key") or ""))
    for store, keys in by_store.values():
        store.mark_aged_out_notified(keys, moment)


def print_digest(subject: str, body: str) -> None:
    """The journal copy. Live sends do not print the body."""
    print(f"Subject: {subject}\n\n{body}\n", flush=True)


def default_mailer(*, to: list[str], subject: str, text_body: str) -> dict[str, Any]:
    """The existing internal mail path. No new credential is read here."""
    from .verification_mailer import send_verification_email

    return send_verification_email(
        to=to,
        cc=[],
        subject=subject,
        text_body=text_body,
        plain_only=True,
    )


def run_digest(
    *,
    stores: list[EventKeyStore] | None = None,
    now: datetime | None = None,
    live: bool | None = None,
    mailer: Callable[..., dict[str, Any]] | None = None,
    ezlynx_client: Any | None = None,
    ascend_client: Any | None = None,
    refresh: bool = True,
    pause_s: float = INDEX_PAUSE_S,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    deadline_s: float | None = None,
) -> dict[str, Any]:
    """Compose the accounting email. Send only when live and the list is non-empty."""
    moment = now or _now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    sending = live_enabled() if live is None else live
    ticks = clock or time.monotonic
    deadline = ticks() + (RUN_DEADLINE_S if deadline_s is None else deadline_s)
    pause = sleep or time.sleep
    opened = stores if stores is not None else open_digest_stores()
    notes_state = notes_state_from_polls(opened, moment)
    index_report: dict[str, Any] | None = None
    if refresh and ezlynx_client is not None and opened:
        _rows, meta, _store = best_policy_index(opened)
        if not index_is_fresh(meta, moment):
            try:
                index_report = refresh_policy_index(
                    ezlynx_client,
                    opened,
                    now=moment,
                    pause_s=pause_s,
                    sleep=pause,
                    clock=ticks,
                    deadline=deadline,
                )
            except Exception as exc:  # noqa: BLE001 - the email still goes out
                logger.warning("policy index refresh failed: %s", type(exc).__name__)
                index_report = {
                    "calls": 0,
                    "complete": False,
                    "saved": False,
                    "stopped": type(exc).__name__,
                }
    index_rows, meta, _store = best_policy_index(opened)
    index_complete = int(meta.get("complete") or 0) == 1
    open_rows = collect_open(opened)
    open_rows, ready_count = recheck_open_unmatched(
        open_rows,
        ezlynx_client=ezlynx_client,
        index_rows=index_rows,
        index_complete=index_complete,
        now=moment,
        persist=sending,
        notes_state=notes_state,
        pause=pause,
        deadline=deadline,
        clock=ticks,
        ascend_client=ascend_client,
    )
    for row in open_rows:
        row["notes_state"] = notes_state
        if row.get("ready_to_file") or str(row.get("ready_at") or "").strip():
            row["ready_to_file"] = True
            row["api_notes_live"] = notes_state == "live"
    apply_suggestions(
        open_rows,
        index_rows,
        index_complete=index_complete,
    )
    current, aged = split_window(open_rows, moment)
    emailed = [*current, *aged]
    policy_rechecked = bool(emailed) and all(row.get("_policy_rechecked") for row in emailed)
    rendered = render_digest(
        current, aged, when=moment, policy_rechecked=policy_rechecked
    )
    result: dict[str, Any] = {
        "dry_run": not sending,
        "live": sending,
        "empty": rendered is None,
        "sent": False,
        "printed": False,
        "open_count": len(current),
        "aged_count": len(aged),
        "ready_count": ready_count,
        "resolved_count": 0,
        "index": index_report,
        "subject": "",
        "body": "",
        "error": "",
    }
    if rendered is None:
        logger.info("no unmatched Ascend notices; no email")
        return result
    subject, body = rendered
    result["subject"] = subject
    result["body"] = body
    if not sending:
        print_digest(subject, body)
        result["printed"] = True
        logger.info(
            "dry-run: printed unmatched digest with %s notice(s)",
            len(current),
        )
        return result
    sender = mailer or default_mailer
    try:
        sender(to=[ACCOUNTING_TO], subject=subject, text_body=body)
    except Exception as exc:  # noqa: BLE001 - refused and failed sends both alert
        result["error"] = type(exc).__name__
        result["sent"] = False
        logger.warning("unmatched digest send failed: %s", type(exc).__name__)
        return result
    result["sent"] = True
    _mark_aged(aged, _iso(moment))
    logger.info("sent unmatched digest with %s notice(s)", len(current))
    return result


def evaluate_digest_health(
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Alert when an installed digest failed or skipped a weekday after 9:30 AM ET.

    Quiet unless the weekday timer is enabled or the installer marker
    exists. A missing state file on a host that never installed the digest,
    after --dry-run-once, or after rollback, does not page. Once installed,
    a recorded failure alerts any day. A run that is still inside the
    45-minute unit timeout stays quiet. A missing state file alerts on a
    weekday after 9:30 AM ET. Weekends and the hour before 9:30 do not
    require a run.
    """
    if not digest_watch_installed():
        return {
            "ok": True,
            "installed": False,
            "detail": "unmatched digest is not installed",
        }
    moment = now or _now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    target = path or state_path()
    local = moment.astimezone(EASTERN)
    deadline = local.replace(
        hour=DEADLINE_HOUR, minute=DEADLINE_MINUTE, second=0, microsecond=0
    )
    if not target.is_file():
        if local.weekday() < 5 and local >= deadline:
            return {
                "ok": False,
                "installed": True,
                "detail": "the Ascend unmatched-notice digest did not run today",
            }
        return {
            "ok": True,
            "installed": True,
            "detail": "unmatched digest has not run yet",
        }
    try:
        parsed = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "ok": False,
            "installed": True,
            "detail": "the Ascend unmatched-notice digest record is unreadable",
        }
    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "installed": True,
            "detail": "the Ascend unmatched-notice digest record is unreadable",
        }
    exit_code = parsed.get("exit_code")
    run_at = parse_time(parsed.get("run_at"))
    if exit_code != 0:
        still_running = (
            str(parsed.get("error") or "") == "started"
            and run_at is not None
            and moment - run_at < timedelta(minutes=50)
        )
        if not still_running:
            return {
                "ok": False,
                "installed": True,
                "detail": "the Ascend unmatched-notice digest failed",
                "run_at": parsed.get("run_at") or "",
            }
    if local.weekday() >= 5:
        return {
            "ok": True,
            "installed": True,
            "detail": "weekend; no digest expected",
            "run_at": parsed.get("run_at") or "",
        }
    if local < deadline:
        return {
            "ok": True,
            "installed": True,
            "detail": "before the morning digest deadline",
            "run_at": parsed.get("run_at") or "",
        }
    if run_at is None or run_at.astimezone(EASTERN).date() != local.date():
        return {
            "ok": False,
            "installed": True,
            "detail": "the Ascend unmatched-notice digest did not run today",
            "run_at": parsed.get("run_at") or "",
        }
    return {
        "ok": True,
        "installed": True,
        "detail": "unmatched digest ran today",
        "run_at": parsed.get("run_at") or "",
    }


def sample_digest() -> tuple[str, str]:
    """A journal-shaped email built from synthetic notices. Nothing is sent."""
    current = [
        {
            "insured_name": "Fixture Hauling LLC",
            "notice_type": "late_payment",
            "amount_cents": 41210,
            "policy_numbers": ["HO-998877"],
            "reason": POLICY_OUTCOME_NOT_IN_EZLYNX,
            "suggestion_client": "Fixture Hauling",
            "suggestion_policy": "HO-998878",
        },
        {
            "insured_name": "Northwind Trucking Inc",
            "notice_type": "cancellation",
            "amount_cents": 120000,
            "policy_numbers": ["CA-100200"],
            "reason": POLICY_OUTCOME_MULTIPLE,
            "suggestion_client": "",
            "suggestion_policy": "",
        },
        {
            "insured_name": "Bare Insured",
            "notice_type": "intent_to_cancel",
            "amount_cents": None,
            "policy_numbers": [],
            "reason": POLICY_OUTCOME_NO_NUMBER,
            "suggestion_client": "",
            "suggestion_policy": "",
        },
    ]
    aged = [
        {
            "insured_name": "Old Mill LLC",
            "notice_type": "payment_confirmation",
        }
    ]
    rendered = render_digest(
        current,
        aged,
        when=datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc),
    )
    if rendered is None:
        return "", ""
    return rendered


def _last_run_payload(result: dict[str, Any], *, exit_code: int, run_at: str) -> dict[str, Any]:
    return {
        "run_at": run_at,
        "exit_code": exit_code,
        "dry_run": bool(result.get("dry_run", True)),
        "sent": bool(result.get("sent")),
        "open_count": int(result.get("open_count") or 0),
        "error": str(result.get("error") or ""),
    }


class _AscendProgramRead:
    """GET one program through the poll's read client.

    This is the same client ``robie-ascend-sync`` and the notice poll use.
    It cannot create programs, billables, or insureds.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def get_program(self, program_id: str) -> Any:
        from .ascend_api_notice_source import FEED_PROGRAMS

        program = str(program_id or "").strip()
        if not program:
            raise ValueError("program id is required")
        return self._client.get(f"{FEED_PROGRAMS}/{program}")

    def get_billables(self, program_id: str) -> Any:
        """GET /v1/billables?program_id= the way the poll does. No cache."""
        from .ascend_api_notice_source import FEED_BILLABLES, PAGE_SIZE

        program = str(program_id or "").strip()
        if not program:
            raise ValueError("program id is required")
        return self._client.get(
            FEED_BILLABLES,
            {"program_id": program, "page_size": PAGE_SIZE},
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, force=True)
    parser = argparse.ArgumentParser(
        description=(
            "Email accounting the open unmatched Ascend notices. "
            "Dry-run unless ASCEND_UNMATCHED_DIGEST_LIVE=1."
        )
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Do not page EZLynx. Use the saved policy index.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Print a synthetic digest and do not read the store or send.",
    )
    args = parser.parse_args(argv)
    if args.sample:
        subject, body = sample_digest()
        print_digest(subject, body)
        return 0
    moment = _now()
    # Written before PolicyApi paging so a killed run still leaves a record.
    # A finished run replaces this. Health treats a fresh "started" row as
    # still running and an old one as a failure.
    write_last_run(
        {
            "run_at": _iso(moment),
            "exit_code": 1,
            "dry_run": not live_enabled(),
            "sent": False,
            "open_count": 0,
            "error": "started",
        }
    )
    try:
        client = None
        if not args.no_refresh:
            try:
                from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config

                client = EzlynxApiClient(load_ezlynx_api_config())
            except Exception as exc:  # noqa: BLE001 - email still runs without an index
                logger.warning("policy index client unavailable: %s", type(exc).__name__)
                client = None
        ascend_client = None
        try:
            from .ascend_api_notice_source import build_client

            ascend_client = _AscendProgramRead(build_client())
        except Exception as exc:  # noqa: BLE001 - the saved policy number still works
            logger.warning("Ascend program read unavailable: %s", type(exc).__name__)
            ascend_client = None
        result = run_digest(
            now=moment,
            ezlynx_client=client,
            ascend_client=ascend_client,
            refresh=client is not None,
        )
    except Exception as exc:  # noqa: BLE001 - the health file is the alert signal
        logger.warning("unmatched digest failed: %s", type(exc).__name__)
        write_last_run(
            {
                "run_at": _iso(moment),
                "exit_code": 1,
                "dry_run": not live_enabled(),
                "sent": False,
                "open_count": 0,
                "error": type(exc).__name__,
            }
        )
        return 1
    exit_code = 1 if result.get("error") else 0
    write_last_run(_last_run_payload(result, exit_code=exit_code, run_at=_iso(moment)))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
