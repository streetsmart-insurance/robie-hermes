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
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .ascend_api_notice_source import (
    EventKeyStore,
    _iso,
    _now,
    cents_to_money,
    db_path,
    dry_run_db_path,
    live_db_path,
    parse_time,
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
    _row_policy_number,
    insured_name_cores,
    insured_names_match,
)
from .ascend_notice_triage import _NOTICE_HEADINGS

logger = logging.getLogger(__name__)

LIVE_ENV = "ASCEND_UNMATCHED_DIGEST_LIVE"
STATE_ENV = "ASCEND_UNMATCHED_DIGEST_STATE"
DB_ENV = "ASCEND_UNMATCHED_DIGEST_DB"
ACCOUNTING_TO = "accounting@streetsmart.insurance"
EASTERN = ZoneInfo("America/New_York")
WINDOW = timedelta(days=14)
INDEX_STALE = timedelta(hours=20)
INDEX_PAGE_SIZE = 100
# 38k policies at 100 per page is 380 calls. A smaller honored page (30)
# is about 1,300 calls. 2,000 leaves headroom and still stops a runaway.
INDEX_MAX_PAGES = 2000
INDEX_PAUSE_S = 0.25
# Stay well under the unit TimeoutStartSec of 45 minutes so the state
# file is written even when PolicyApi is slow.
INDEX_TIME_BUDGET_S = 20 * 60
# One 401 re-grant, then these waits on 429 and 5xx.
PAGE_BACKOFF_S = (1.0, 2.0, 4.0)
DEADLINE_HOUR = 9
DEADLINE_MINUTE = 30
DEFAULT_STATE_PATH = Path(
    "/opt/streetsmart-hermes/robie-job-engine/data/ascend-api/"
    "unmatched-digest-last-run.json"
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
    }
)

_REASON_WORDS = {
    POLICY_OUTCOME_NO_NUMBER: "Ascend did not include a policy number.",
    POLICY_OUTCOME_NOT_IN_EZLYNX: "That policy number is not in EZLynx.",
    POLICY_OUTCOME_MULTIPLE: "More than one EZLynx client matched.",
    POLICY_OUTCOME_INCOMPLETE: "The EZLynx search did not finish, so Robie did not guess.",
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
_FIX_LINE = (
    "Correct the policy number in EZLynx or Ascend, and Robie files it "
    "automatically on its next run."
)


def live_enabled() -> bool:
    """True only for the exact env value ``1``."""
    return str(os.environ.get(LIVE_ENV) or "").strip() == "1"


def state_path() -> Path:
    override = str(os.environ.get(STATE_ENV) or "").strip()
    if override:
        return Path(override)
    return DEFAULT_STATE_PATH


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
    """One client when both the policy is one character off and the name matches.

    Zero or two candidates return nothing. The caller must pass a complete
    index. This function does not call EZLynx.
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
        client_name = str(row.get("applicant_name") or "").strip()
        applicant_id = str(row.get("applicant_id") or "").strip()
        if not applicant_id or not insured_names_match(insured_name, client_name):
            continue
        found[(applicant_id, key)] = NearMatch(
            client_name=client_name,
            policy_number=str(row.get("policy_number") or "").strip(),
            applicant_id=applicant_id,
        )
    if len(found) != 1:
        return None
    return next(iter(found.values()))


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


def rows_matching_filed_email(
    rows: list[dict[str, Any]],
    *,
    notice_type: str,
    program_id: str,
    policy_numbers: list[str],
    insured_name: str,
) -> list[dict[str, Any]]:
    """Open rows the email driver just filed.

    Policy numbers that overlap win. When the number was corrected and no
    longer overlaps, exactly one same-name row is closed. Two candidates
    with no shared policy number stay open.
    """
    kind = str(notice_type or "").strip().lower()
    program = str(program_id or "").strip().lower()
    wanted = {policy_compare_key(number) for number in policy_numbers}
    wanted.discard("")
    open_rows = [
        row
        for row in rows
        if not str(row.get("resolved_at") or "").strip()
        and str(row.get("notice_type") or "").strip().lower() == kind
    ]
    if program:
        open_rows = [
            row
            for row in open_rows
            if not str(row.get("program_id") or "").strip()
            or str(row.get("program_id") or "").strip().lower() == program
        ]

    def name_ok(row: dict[str, Any]) -> bool:
        row_name = str(row.get("insured_name") or "").strip()
        if not row_name or not str(insured_name or "").strip():
            return True
        return insured_names_match(insured_name, row_name)

    named = [row for row in open_rows if name_ok(row)]
    if wanted:
        overlapped = []
        for row in named:
            keys = {policy_compare_key(number) for number in row.get("policy_numbers") or []}
            keys.discard("")
            if keys & wanted:
                overlapped.append(row)
        if overlapped:
            return overlapped
        if len(named) == 1:
            return named
        return []
    if len(named) == 1:
        return named
    return []


def resolve_unmatched_filed_by_email(
    *,
    notice_type: str,
    program_id: str,
    policy_numbers: list[str],
    insured_name: str,
    seen_at: str,
    stores: list[EventKeyStore] | None = None,
) -> int:
    """Close open rows after the email driver files the notice.

    Only store files that already exist are opened. A missing digest unit
    and a missing database are left alone. Nothing is written to EZLynx.
    """
    opened = stores
    if opened is None:
        opened = []
        for path in digest_store_paths():
            if path.is_file():
                opened.append(EventKeyStore(path))
    closed = 0
    for store in opened:
        try:
            matches = rows_matching_filed_email(
                store.list_unmatched(),
                notice_type=notice_type,
                program_id=program_id,
                policy_numbers=policy_numbers,
                insured_name=insured_name,
            )
            for row in matches:
                store.resolve_unmatched(str(row.get("event_key") or ""), seen_at)
                closed += 1
        except Exception as exc:  # noqa: BLE001 - filing already happened
            logger.warning(
                "unmatched resolve after email file failed: %s", type(exc).__name__
            )
    return closed


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
    lines.append(_FIX_LINE)
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
) -> tuple[Any, int]:
    """One PolicyApi page and how many GETs it took.

    Back off on 429/5xx. Re-grant once on 401. The only client methods used
    are ``search_policy_page`` and ``clear_cached_token``. No policy is written.
    """
    search = getattr(client, "search_policy_page", None)
    if search is None:
        raise RuntimeError("no_page_search")
    reauthed = False
    backoff_used = 0
    attempts = 0
    while True:
        attempts += 1
        try:
            return search(page_index, page_size), attempts
        except Exception as exc:
            status = getattr(exc, "status", None)
            retryable = bool(getattr(exc, "retryable", False)) or status == 429 or (
                isinstance(status, int) and status >= 500
            )
            if status == 401 and not reauthed and _clear_cached_token(client):
                reauthed = True
                continue
            if retryable and status != 401 and backoff_used < len(PAGE_BACKOFF_S):
                pause(PAGE_BACKOFF_S[backoff_used])
                backoff_used += 1
                continue
            raise


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
        if ticks() - started >= budget_s:
            stopped = "time_budget"
            complete = False
            break
        pages += 1
        if pages > 1 and pause_s > 0:
            pause(pause_s)
        payload, attempts = fetch_policy_page(client, pages, page_size, pause=pause)
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
    refresh: bool = True,
    pause_s: float = INDEX_PAUSE_S,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Compose the accounting email. Send only when live and the list is non-empty."""
    moment = now or _now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    sending = live_enabled() if live is None else live
    opened = stores if stores is not None else open_digest_stores()
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
                    sleep=sleep,
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
    open_rows = collect_open(opened)
    apply_suggestions(
        open_rows,
        index_rows,
        index_complete=int(meta.get("complete") or 0) == 1,
    )
    current, aged = split_window(open_rows, moment)
    rendered = render_digest(current, aged, when=moment)
    result: dict[str, Any] = {
        "dry_run": not sending,
        "live": sending,
        "empty": rendered is None,
        "sent": False,
        "printed": False,
        "open_count": len(current),
        "aged_count": len(aged),
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
    """Alert when the digest failed or skipped a weekday after 9:30 AM ET.

    A recorded failure alerts any day. A run that is still inside the
    45-minute unit timeout stays quiet. A missing state file alerts on a
    weekday after 9:30 AM ET. Weekends and the hour before 9:30 do not
    require a run.
    """
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
                "installed": False,
                "detail": "the Ascend unmatched-notice digest did not run today",
            }
        return {
            "ok": True,
            "installed": False,
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
        result = run_digest(now=moment, ezlynx_client=client, refresh=client is not None)
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
