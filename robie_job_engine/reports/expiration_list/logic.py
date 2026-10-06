"""Pure business logic for the Weekly Expiration List report.

No network, no Sheets calls here — everything operates on parsed payloads
so it is fully unit-testable. Sandeep Yadav's E1-E15 answers are LOCKED
(2026-10-05); remaining assumptions are marked with their question numbers
(see ASSUMPTIONS.md).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config
from .portal import ExpirationPortalClient


def today_et() -> date:
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


_MS_DATE_RE = re.compile(r"/Date\((\d+)([+-]\d{4})?\)/")


def parse_ezlynx_date(value: object) -> date | None:
    """Parse /Date(ms)/ or ISO date strings. Returns None when unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = _MS_DATE_RE.fullmatch(text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, ZoneInfo(config.TIMEZONE)).date()
    # (format, width of the date text it parses)
    for fmt, width in (
        ("%Y-%m-%dT%H:%M:%S.%f", 26),
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d", 10),
    ):
        try:
            return datetime.strptime(text[:width], fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Retention Center rows
# ---------------------------------------------------------------------------


@dataclass
class RetentionRow:
    applicant_id: int
    days_to_expiration: int
    earliest_expiration: date | None
    first_name: str = ""
    last_name: str = ""
    business_name: str = ""
    policies_count: int = 0
    # E9 fallback step 2: Retention Center "Renewal Manager" (field name
    # INFERRED — unverified against the live list shape).
    renewal_manager: str = ""


def parse_retention_row(raw: dict) -> RetentionRow | None:
    try:
        applicant_id = int(raw.get("ID"))
        days = int(raw.get("DaysToExpiration"))
    except (TypeError, ValueError):
        return None
    return RetentionRow(
        applicant_id=applicant_id,
        days_to_expiration=days,
        earliest_expiration=parse_ezlynx_date(raw.get("EarliestExpirationDate")),
        first_name=str(raw.get("FirstName") or ""),
        last_name=str(raw.get("LastName") or ""),
        business_name=str(raw.get("BusinessName") or ""),
        policies_count=int(raw.get("PoliciesCount") or 0),
        renewal_manager=str(
            raw.get("RenewalManager") or raw.get("Renewal Manager") or ""
        ).strip(),
    )


def fetch_retention_rows(
    client: ExpirationPortalClient,
    *,
    window_days: int = config.WINDOW_DAYS,
    page_size: int = config.RETENTION_PAGE_SIZE,
    max_pages: int = 50,
) -> list[RetentionRow]:
    """Paginate GetExpirationList, keep 0 <= days <= window_days.

    E1/E2 (LOCKED): 30-day window; DaysToExpiration == 0 (expires today)
    is included; already-expired (negative) rows are excluded.
    E3 (LOCKED): pages are walked in daysToExpiration ascending order;
    continue to the next page IFF the LAST row of the current page has
    DaysToExpiration <= window_days AND another page exists.
    """
    rows: list[RetentionRow] = []
    for page_index in range(1, max_pages + 1):
        payload = client.retention_expiration_list(
            page_size=page_size, page_index=page_index,
            sort_column="daysToExpiration", sort_order="asc",
        )
        raw_rows = payload.get("results") or []
        if not raw_rows:
            break
        last_days: int | None = None
        for raw in raw_rows:
            row = parse_retention_row(raw)
            if row is None:
                continue
            last_days = row.days_to_expiration
            if 0 <= row.days_to_expiration <= window_days:
                rows.append(row)
        # E3 stop condition: no more pages worth reading when the last
        # row already exceeds the window (or the page was all malformed).
        if last_days is None or last_days > window_days:
            break
    return rows


# ---------------------------------------------------------------------------
# Per-account reads
# ---------------------------------------------------------------------------


def account_display_name(sidebar: dict) -> str:
    """Full name incl. co-insured — docx says use this, not the truncated
    Retention Center name."""
    applicant = sidebar.get("applicant") or {}
    return str(applicant.get("accountName") or applicant.get("name") or "").strip()


def assigned_producer(sidebar: dict, *, fallback: str = "") -> str:
    """Grouping owner (E6 LOCKED): sidebar account-level
    assignment.assignedTo. E9 fallback chain: sidebar assignedTo ->
    Retention Center "Renewal Manager" (passed as fallback) -> "" (the
    caller routes empty to the "Unassigned" section)."""
    applicant = sidebar.get("applicant") or {}
    assignment = applicant.get("assignment") or {}
    return str(assignment.get("assignedTo") or "").strip() or fallback.strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def cycle_days_for_lob(lob: str) -> tuple[int, bool]:
    """E5 (LOCKED): 90 days for personal lines, 120 for commercial/trucking.

    Returns (cycle_days, mapped_ok). Missing or unmapped LOB falls back
    to 120 and mapped_ok=False so the policy is flagged in the run
    summary. The personal-lines marker list is INFERRED (see ASSUMPTIONS
    E5); Sandeep did not provide the full LOB vocabulary.
    """
    norm = _norm(lob)
    if not norm:
        return config.DEFAULT_CYCLE_DAYS, False
    if any(marker in norm for marker in config.PERSONAL_LOB_MARKERS):
        return config.PERSONAL_LINES_CYCLE_DAYS, True
    if any(marker in norm for marker in config.COMMERCIAL_LOB_MARKERS):
        return config.DEFAULT_CYCLE_DAYS, True
    return config.DEFAULT_CYCLE_DAYS, False


@dataclass
class PolicyLine:
    lob: str
    policy_number: str
    expiration: date | None
    cycle_days: int = config.DEFAULT_CYCLE_DAYS
    pending_type: str = ""
    lob_flag: bool = False  # True when LOB missing/unmapped (120-day fallback used)


def _clean_policy_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def select_expiring_policies(
    cards: list[dict], *, today: date | None = None,
    window_days: int = config.POLICY_WINDOW_DAYS,
) -> list[PolicyLine]:
    """Docx Step 2: policyStatusViewModelID==1 and expirationDate between
    today and today+31 days. Format '{lob} | {policyNumber}'.

    E8 (LOCKED): policies with pendingType="Renewal" (already renewed)
    are EXCLUDED from the list — each run rebuilds from scratch and
    renewed policies drop off. There is no green highlight or carryover.
    """
    today = today or today_et()
    out: list[PolicyLine] = []
    for card in cards:
        try:
            active = int(card.get("policyStatusViewModelID")) == 1
        except (TypeError, ValueError):
            active = False
        exp = parse_ezlynx_date(card.get("expirationDate"))
        if not active or exp is None:
            continue
        if not (today <= exp <= today + timedelta(days=window_days)):
            continue
        pending_type = str(card.get("pendingType") or "")
        if bool(card.get("hasPending")) and pending_type.lower() == "renewal":
            continue  # E8: renewed — drops off the list entirely
        lob = _clean_policy_text(card.get("lob"))
        cycle_days, mapped_ok = cycle_days_for_lob(lob)
        out.append(
            PolicyLine(
                lob=lob,
                policy_number=_clean_policy_text(card.get("policyNumber")),
                expiration=exp,
                cycle_days=cycle_days,
                pending_type=pending_type,
                lob_flag=not mapped_ok,
            )
        )
    out.sort(key=lambda p: (p.expiration or date.max, p.policy_number))
    return out


# ---------------------------------------------------------------------------
# Renewal discussion matching + latest staff note
# ---------------------------------------------------------------------------


def _contains_any(haystack: str, needles: tuple[str, ...]) -> bool:
    h = _norm(haystack)
    return any(n in h for n in needles)


def _contains_word(haystack: str, words: tuple[str, ...]) -> bool:
    tokens = set(re.findall(r"[a-z0-9]+", _norm(haystack)))
    return any(w in tokens for w in words)


def is_renewal_discussion(title: str) -> bool:
    """E4 (LOCKED): reject markers are checked FIRST (case-insensitive
    substring), then include patterns (renew / renwal / non-renew /
    "mortgage verification" substring, whole-word NR)."""
    if _contains_any(title, config.RENEWAL_TITLE_REJECT):
        return False
    return _contains_any(title, config.RENEWAL_TITLE_PATTERNS) or _contains_word(
        title, config.RENEWAL_TITLE_WORD_PATTERNS
    )


def is_nonrenewal_text(text: str) -> bool:
    return _contains_any(text, config.NONRENEWAL_PATTERNS) or _contains_word(
        text, config.NONRENEWAL_WORD_PATTERNS
    )


def match_renewal_discussions(
    discussions: list[dict],
    *,
    cycle_start: date,
) -> list[dict]:
    """E4 (LOCKED): renewal discussion candidates — title include/exclude
    matching only, modified since the cycle start. Most recent first.
    The policy-number tie is checked separately (title OR notes) by
    discussion_tied_to_policy after fetching detail."""
    matched: list[dict] = []
    for disc in discussions:
        title = str(disc.get("title") or "")
        if not is_renewal_discussion(title):
            continue
        modified = parse_ezlynx_date(disc.get("lastModified"))
        if modified is not None and modified < cycle_start:
            continue
        matched.append(disc)
    matched.sort(
        key=lambda d: parse_ezlynx_date(d.get("lastModified")) or date.min,
        reverse=True,
    )
    return matched


def discussion_tied_to_policy(
    title: str, note_texts: list[str], policy_numbers: list[str]
) -> bool:
    """E4 (LOCKED): the discussion must be tied to the expiring policy —
    policy number in the title OR in the notes. No policy number anywhere
    -> no tie (Notes = "No notes")."""
    numbers = {_clean_policy_text(n).casefold() for n in policy_numbers if n}
    if not numbers:
        return False
    if any(n in _norm(title) for n in numbers):
        return True
    return any(
        any(n in _norm(body) for n in numbers) for body in note_texts
    )


def is_bot_note(note_text: str, author: str) -> bool:
    """Step 3 skip-bots heuristic (UNVERIFIED, finding 5 / E4)."""
    if _contains_any(note_text, config.BOT_TEXT_MARKERS):
        return True
    author_norm = _norm(author)
    if any(m in author_norm for m in config.BOT_AUTHOR_MARKERS):
        return True
    # Activity-only entries: no note body, just a due-date/activity comment.
    if not _norm(note_text) and not _norm(str("")).strip():
        return True
    return False


def author_label(author: str) -> str:
    """E11 (LOCKED): "Last Activity by" = first name + last initial,
    exact format "First L" (no period). Single-token names stay as-is."""
    tokens = [t for t in _norm(author).split(" ") if t]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0].capitalize()
    return f"{tokens[0].capitalize()} {tokens[1][0].upper()}"


def clean_note_text(note_text: str, *, cap: int = config.NOTE_CHAR_CAP) -> str:
    """Strip HTML, collapse whitespace, cap at ~450 chars.

    E12 (LOCKED): truncate at the last sentence boundary (". "/"! "/"? ",
    punctuation included) at or before char 450; if none, word boundary;
    append "…".
    """
    text = re.sub(r"<[^>]+>", " ", note_text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > cap:
        window = text[:cap]
        cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
        if cut > 0:
            text = window[: cut + 1] + "…"
        else:
            word_cut = text.rfind(" ", 0, cap - 1)
            text = (text[:word_cut] if word_cut > 0 else text[: cap - 1]).rstrip() + "…"
    return text


def latest_staff_note(notes: list[dict]) -> tuple[str, str]:
    """Return (notes_text, last_activity_by_label).

    Most recent note written by staff; bots/automation and activity-only
    entries skipped. E11 (LOCKED): label is "First L". E4: bare
    placeholder 'Upcoming renewal add notes here' counts as no staff
    note.
    """
    ordered = sorted(
        notes,
        key=lambda n: str(n.get("created") or ""),
        reverse=True,
    )
    for note in ordered:
        body = str(note.get("note") or "")
        author = str(note.get("createdByName") or "")
        if is_bot_note(body, author):
            continue
        if _norm(body) in ("", config.PLACEHOLDER_NOTE_TEXT):
            continue
        return clean_note_text(body), author_label(author)
    return config.NO_NOTES_TEXT, ""


def collect_note_bodies(detail: dict) -> list[dict]:
    """Flatten discussion.notes[] plus nested notes[].task.taskNotes[]."""
    discussion = detail.get("discussion") or {}
    out: list[dict] = []
    for note in discussion.get("notes") or []:
        out.append(note)
        task = note.get("task") or {}
        for tnote in task.get("taskNotes") or []:
            if isinstance(tnote, dict):
                out.append(tnote)
    return out


# ---------------------------------------------------------------------------
# Account classification + row assembly
# ---------------------------------------------------------------------------


@dataclass
class AccountRow:
    applicant_id: int
    account_name: str
    producer: str
    days_to_expiration: int
    policy_lines: list[str] = field(default_factory=list)
    notes: str = ""
    last_activity_by: str = ""
    nonrenewal: bool = False          # non-renewal activity found (Step 2b)
    cancellation_pending: bool = False  # E15: pendingType="Cancellation" on an expiring policy
    lob_flags: list[str] = field(default_factory=list)  # E5: policy numbers whose LOB was missing/unmapped


def build_account_row(
    *,
    retention_row: RetentionRow,
    sidebar: dict,
    policies: list[PolicyLine],
    discussions: list[dict],
    matched_discussion: dict | None,
    discussion_detail: dict,
    cycle_start: date,
    today: date | None = None,
) -> AccountRow:
    """Assemble one account row per docx Steps 2-3.

    E6 (LOCKED): one row per account; sidebar account-level assignedTo
    wins (E9 fallback chain applied by the caller via assigned_producer).
    E8 (LOCKED): renewed policies never reach here (excluded upstream).
    E15 (LOCKED): pendingType="Cancellation" -> "CANCELLATION PENDING – "
    prefix + red-fill formatting intent. pendingType="Endorsement" ->
    no effect on membership.
    """
    today = today or today_et()
    producer = assigned_producer(sidebar, fallback=retention_row.renewal_manager)

    # Non-renewal activity anywhere in discussions since the cycle start.
    nonrenewal = False
    for disc in discussions:
        modified = parse_ezlynx_date(disc.get("lastModified"))
        if modified is not None and modified < cycle_start:
            continue
        if is_nonrenewal_text(str(disc.get("title") or "")):
            nonrenewal = True
            break

    notes_text, last_by = config.NO_NOTES_TEXT, ""
    if matched_discussion is not None:
        bodies = collect_note_bodies(discussion_detail)
        # Also consider note-level non-renewal markers.
        if not nonrenewal:
            nonrenewal = any(
                is_nonrenewal_text(str(n.get("note") or "")) for n in bodies
            )
        notes_text, last_by = latest_staff_note(bodies)

    cancellation = any(p.pending_type.lower() == "cancellation" for p in policies)

    row = AccountRow(
        applicant_id=retention_row.applicant_id,
        account_name=account_display_name(sidebar),
        producer=producer,
        days_to_expiration=retention_row.days_to_expiration,
        policy_lines=[
            f"{p.lob} | {p.policy_number}" if p.lob else p.policy_number
            for p in policies
        ],
        notes=notes_text,
        last_activity_by=last_by,
        nonrenewal=nonrenewal,
        cancellation_pending=cancellation,
        lob_flags=[p.policy_number for p in policies if p.lob_flag],
    )
    if nonrenewal:
        row.notes = config.NONRENEWAL_NOTE_PREFIX + row.notes
    if cancellation:
        row.notes = config.CANCELLATION_NOTE_PREFIX + row.notes
    return row


def producer_section(producer: str) -> tuple[str, int] | None:
    """Return (section_name, producer_index) for a producer, else None."""
    for section, names in config.SECTIONS:
        if producer in names:
            return section, names.index(producer)
    return None


def producer_label(producer: str) -> str:
    return config.PRODUCER_LABELS.get(producer, producer)


def group_rows(rows: list[AccountRow]) -> list[tuple[str, str, list[AccountRow]]]:
    """Group rows into (section, producer, rows) in docx Step 5 order;
    accounts in days-to-expiration order; E9/E10: unknown/empty producers
    go to a trailing 'Unassigned' section for Sandeep's review."""
    by_producer: dict[str, list[AccountRow]] = {}
    unassigned: list[AccountRow] = []
    for row in rows:
        if producer_section(row.producer) is None:
            unassigned.append(row)
        else:
            by_producer.setdefault(row.producer, []).append(row)
    ordered: list[tuple[str, str, list[AccountRow]]] = []
    for section, names in config.SECTIONS:
        for producer in names:
            group = by_producer.get(producer, [])
            if not group:
                continue
            group.sort(key=lambda r: (r.days_to_expiration, r.account_name))
            ordered.append((section, producer, group))
    if unassigned:
        unassigned.sort(key=lambda r: (r.days_to_expiration, r.account_name))
        ordered.append(("Unassigned", "", unassigned))
    return ordered


def run_account(
    client: ExpirationPortalClient,
    retention_row: RetentionRow,
    *,
    today: date | None = None,
) -> AccountRow | None:
    """Full per-account read path (Steps 2-3). Fail-closed on any read.

    Returns None when the account has no expiring policies left after
    E8 filtering (all renewed -> account drops off the list).
    """
    today = today or today_et()
    sidebar = client.sidebar(retention_row.applicant_id)
    cards = client.policies(retention_row.applicant_id)
    policies = select_expiring_policies(cards, today=today)
    if not policies:
        return None  # E8: all expiring policies renewed — drop the account
    # E5: per-policy cycle days; the account-level cycle start uses the
    # longest window so no policy's renewal discussion is dropped early.
    cycle_start = today - timedelta(days=max(p.cycle_days for p in policies))
    policy_numbers = [p.policy_number for p in policies]
    paged = client.paged_discussions(retention_row.applicant_id, page_number=1, page_size=60)
    discussions = paged.get("discussions") or []
    matched = match_renewal_discussions(discussions, cycle_start=cycle_start)
    # E4: pick the first title-matched discussion that is also tied to
    # the expiring policy (policy number in title OR in its notes).
    selected: dict | None = None
    selected_detail: dict = {}
    for disc in matched[:5]:
        did = int(disc.get("discussionId") or 0)
        if not did:
            continue
        detail = client.discussion_detail(did, retention_row.applicant_id)
        note_texts = [str(n.get("note") or "") for n in collect_note_bodies(detail)]
        if discussion_tied_to_policy(
            str(disc.get("title") or ""), note_texts, policy_numbers
        ):
            selected, selected_detail = disc, detail
            break
    return build_account_row(
        retention_row=retention_row,
        sidebar=sidebar,
        policies=policies,
        discussions=discussions,
        matched_discussion=selected,
        discussion_detail=selected_detail,
        cycle_start=cycle_start,
        today=today,
    )
