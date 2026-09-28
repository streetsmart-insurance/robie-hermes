"""Phase 2 of the 4359 overdue policy-change worker: reply tracking,
confirmation checking, and the 14-day escalation report to Carlo.

Phase 1 (weekly Tuesday nag) emails each CSR about their overdue open
change requests. This worker (daily) closes the loop:

  1. Reply tracking: read robie@streetsmart.insurance Gmail, match CSR
     replies to the nag threads phase 1 recorded (thread IDs in the sent
     store), and classify each reply as in_progress / blocked /
     docs_claimed / no_signal. A real signal ("working on it") suppresses
     further Tuesday nags for that change.
  2. Confirmation checking: for docs_claimed changes, look for the
     endorsement / revised dec page via DocumentApi (fuzzy name match +
     policy digits). Document metadata carries no machine-readable
     insured/effective-date/premium fields, so the only machine
     confirmation available is the change dropping off the open 4359
     queue (EZLynx request no longer Open). Anything else is
     needs_human (summarize both sides, never guess) or discrepancy
     (exact field mismatches, e.g. the reply names a different policy).
  3. 14-day escalation: any change whose first nag is more than 14 days
     ago and whose status is not confirmed goes into a digest email to
     Carlo. No duplicates: escalation is recorded per change.

All state lives in the same data dir as phase 1
(overdue_policy_change_reports/): sent.json (phase 1's, extended with
thread/message IDs), followup.json (this worker's per-change status +
history). Dry-run mode: no sends, no state mutation.

The worker never writes to EZLynx. The only writes are the outbound
escalation email (live mode only) and the state files.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .overdue_policy_change_reports import (
    AGENCY_EMAIL_SUFFIX,
    JOB_TYPE as PHASE1_JOB_TYPE,
    REPORT_KEY,
    REPORT_MAILBOX,
    REQUIRED_COLUMNS,
    RESOURCE_ID,
    SENDER,
    PolicyChangeReportContractError,
    _name_key,
    _utc_now,
    default_queue_reader,
    normalize_policy_number,
    notification_key,
    parse_4359_rows,
    row_created_date,
)

logger = logging.getLogger(__name__)

JOB_TYPE = "ezlynx.policy_change_followup"
FOLLOWUP_SENDER = SENDER
ESCALATION_TO = "carlo@streetsmart.insurance"
ESCALATION_SUBJECT = "Policy changes unconfirmed after 14 days"
ESCALATION_AFTER_DAYS = 14
FOLLOWUP_STORE_FILENAME = "followup.json"
DEFAULT_FOLLOWUP_STORE = "~/.robie/overdue_policy_change_reports/followup.json"


def default_followup_store_path() -> str:
    override = os.environ.get("ROBIE_4359_FOLLOWUP_STORE", "").strip()
    if override:
        return override
    return str(Path(DEFAULT_FOLLOWUP_STORE).expanduser())

# -- follow-up statuses --------------------------------------------------------
STATUS_NO_SIGNAL = "no_signal"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"
STATUS_DOCS_CLAIMED = "docs_claimed"
STATUS_CONFIRMED = "confirmed"
STATUS_DISCREPANCY = "discrepancy"
STATUS_NEEDS_HUMAN = "needs_human"
FOLLOWUP_STATUSES = (
    STATUS_NO_SIGNAL, STATUS_IN_PROGRESS, STATUS_BLOCKED,
    STATUS_DOCS_CLAIMED, STATUS_CONFIRMED, STATUS_DISCREPANCY,
    STATUS_NEEDS_HUMAN,
)
# A live reply signal in any of these means the CSR is engaged: phase 1's
# Tuesday run must not nag the change again.
ACTIVE_REPLY_STATUSES = frozenset(
    {STATUS_IN_PROGRESS, STATUS_BLOCKED, STATUS_DOCS_CLAIMED}
)


# -- reply classification ------------------------------------------------------

# Checked before any keyword: automated/out-of-office noise is never a
# status signal.
OOO_HINTS = (
    "out of office",
    "automatic reply",
    "auto-reply",
    "autoreply",
    "on vacation",
    "will be away",
    "currently away",
    "do not reply",
    "noreply",
    "limited access to email",
)
DOCS_CLAIMED_HINTS = (
    "endorsement received",
    "endorsement is in",
    "got the endorsement",
    "received the endorsement",
    "endorsement issued",
    "endorsement attached",
    "endorsement processed",
    "revised dec",
    "dec page",
    "declarations page",
    "change is complete",
    "change completed",
    "change is done",
    "change has been completed",
    "processed the change",
    "bound the endorsement",
    "all set",
    "taken care of",
)
BLOCKED_HINTS = (
    "blocked",
    "waiting on",
    "still waiting",
    "haven't heard",
    "have not heard",
    "no response from",
    "carrier hasn't",
    "carrier has not",
    "can't get",
    "cannot get",
    "unable to",
    "pending carrier",
    "stuck",
    "no word from",
)
IN_PROGRESS_HINTS = (
    "working on it",
    "in progress",
    "following up",
    "followed up",
    "reached out",
    "submitted to",
    "sent to the carrier",
    "requested from",
    "on it",
    "looking into",
    "chasing",
    "in touch with",
    "have this done",
    "get this done",
    "will complete",
)


def classify_reply(text: Any) -> str:
    """Classify a CSR reply body into a follow-up status signal.

    Conservative by design: anything ambiguous, empty, or automated
    returns no_signal — never a false status. docs_claimed outranks
    blocked outranks in_progress when a reply mixes signals. Matching is
    word-boundary based so "on it" doesn't fire inside "on item".
    """
    body = str(text or "").casefold()
    if not body.strip():
        return STATUS_NO_SIGNAL
    if _hint_match(OOO_HINTS, body):
        return STATUS_NO_SIGNAL
    if _hint_match(DOCS_CLAIMED_HINTS, body):
        return STATUS_DOCS_CLAIMED
    if _hint_match(BLOCKED_HINTS, body):
        return STATUS_BLOCKED
    if _hint_match(IN_PROGRESS_HINTS, body):
        return STATUS_IN_PROGRESS
    return STATUS_NO_SIGNAL


def _hint_match(hints: tuple[str, ...], body: str) -> bool:
    pattern = r"\b(?:" + "|".join(re.escape(h) for h in hints) + r")\b"
    return re.search(pattern, body) is not None


def extract_policy_digits(text: Any) -> str:
    """All digit runs in a text joined — a coarse policy identity signal.

    Prefer _policy_like_token_digits for attribution decisions: this
    coarse join also catches timeframes ("2-3 days") and filename dates,
    which must never move a change's status.
    """
    return "".join(re.findall(r"\d", str(text or "")))


def _policy_like_token_digits(text: Any) -> set[str]:
    """Digit strings of policy-number-like tokens in free text.

    A policy-like token is alphanumeric (letters/digits/dashes), at
    least 4 characters, with at least 3 digits: "13WECCE5FJ6",
    "S2391821", "BDG-312624001". Timeframes ("2-3 days"), years
    ("2026"), and filename dates ("09-2026") are too short or
    digit-poor to count — they must never poison attribution.
    """
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]{3,}", str(text or "")):
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 3:
            tokens.add(digits)
    return tokens


def _name_mentions_policy_digits(name: str, digits: str) -> bool:
    """The change's digit string appears as a standalone run in the name.

    Boundary-aware: change "2391821" matches "S2391821 09-2026" but not
    "12391821".
    """
    if not digits:
        return False
    return re.search(r"(?<!\d)" + re.escape(digits) + r"(?!\d)", name) is not None


def policy_digits(policy_number: Any) -> str:
    return re.sub(r"\D", "", normalize_policy_number(policy_number))


# -- follow-up state store -----------------------------------------------------


def _default_record() -> dict[str, Any]:
    return {
        "status": STATUS_NO_SIGNAL,
        "history": [],
        "first_seen": None,
        "last_reply_date": None,
        "last_reply_excerpt": "",
        "last_processed_message_id": None,
        "escalated_date": None,
        "confirmation": None,
        # Change metadata (insured/policy/CSR/applicant/created) so a
        # change that drops off the open queue keeps its identity for
        # escalation rendering and reply attribution.
        "context": {},
    }


class FollowupStore:
    """Per-change reply/confirmation state, keyed by phase 1's notification key.

    Atomic JSON save (tmp + replace), same pattern as NotificationStore.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (ValueError, OSError):
            data = {}
        self._records: dict[str, dict[str, Any]] = {}
        for key, raw in (data or {}).items():
            record = _default_record()
            if isinstance(raw, dict):
                record.update({k: v for k, v in raw.items() if k in record})
                if record["status"] not in FOLLOWUP_STATUSES:
                    record["status"] = STATUS_NO_SIGNAL
                if not isinstance(record["history"], list):
                    record["history"] = []
            self._records[str(key)] = record

    def get(self, key: str) -> dict[str, Any]:
        return self._records.setdefault(str(key), _default_record())

    def keys(self) -> list[str]:
        return list(self._records)

    def record_event(self, key: str, event: str, detail: str, today: date) -> None:
        record = self.get(key)
        record["history"].append({
            "date": today.isoformat(),
            "event": str(event),
            "detail": str(detail or "")[:500],
        })
        if record["first_seen"] is None:
            record["first_seen"] = today.isoformat()

    def set_status(self, key: str, status: str, today: date, detail: str = "") -> None:
        if status not in FOLLOWUP_STATUSES:
            raise PolicyChangeReportContractError(
                f"refusing to record unknown follow-up status: {status!r}"
            )
        record = self.get(key)
        old = record["status"]
        # confirmed is terminal: a late "working on it" must never reopen it.
        if old == STATUS_CONFIRMED and status != STATUS_CONFIRMED:
            self.record_event(
                key, "reply_after_confirmed",
                f"late reply ignored, status stays confirmed: {detail}", today,
            )
            return
        record["status"] = status
        self.record_event(
            key,
            "status_change" if old != status else "status_reaffirmed",
            f"{old} -> {status}" + (f": {detail}" if detail else ""),
            today,
        )

    def active_keys(self) -> set[str]:
        """Keys whose CSR is engaged — phase 1 must not re-nag these."""
        return {
            key for key, record in self._records.items()
            if record.get("status") in ACTIVE_REPLY_STATUSES
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._records, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


class DryRunFollowupStore(FollowupStore):
    """A FollowupStore that never persists (dry-run: state untouched)."""

    def save(self) -> None:  # noqa: D102
        logger.info(
            "dry-run: follow-up store NOT saved (would persist %d records)",
            len(self._records),
        )


# -- reply ingestion -----------------------------------------------------------


def _is_own_message(message: Mapping[str, Any]) -> bool:
    sender = str(message.get("from") or message.get("sender") or "").casefold()
    return "robie@" in sender or FOLLOWUP_SENDER.casefold() in sender


def _reply_date(message: Mapping[str, Any]) -> str:
    """Best-effort ISO date from a Gmail Date header. "" when unparseable."""
    raw = str(message.get("date") or "").strip()
    if not raw:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(raw)
        return parsed.date().isoformat()
    except (TypeError, ValueError):
        return ""


def _context_from_row(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    policy_number = normalize_policy_number(row.get("Policy Number"))
    return {
        "account_name": str(row.get("Account Name") or "").strip(),
        "policy_number": policy_number,
        "policy_digits": policy_digits(policy_number),
        "csr": str(row.get("CSR") or "").strip(),
        "applicant_id": str(row.get("Applicant ID") or "").strip(),
        "created_date": str(row.get("created_date") or "").strip(),
    }


def ingest_replies(
    *,
    sent_store: Any,
    followup: FollowupStore,
    read_thread: Callable[[str], list[dict[str, Any]]],
    key_context: Mapping[str, Mapping[str, Any]],
    csr_is_active: Callable[[str], bool] | None,
    today: date,
) -> dict[str, Any]:
    """Match CSR replies to nag threads and update per-change status.

    key_context maps notification key -> {"policy_digits", "csr",
    "applicant_id"}. read_thread(thread_id) returns messages oldest-first
    as {"id", "from", "date", "text"}. Only messages after the last
    processed one are considered; Robie's own sends are skipped.

    A reply naming a different policy's digits is NEVER attributed to the
    change — it is logged as misattributed and the status is untouched.
    A reply from a CSR who is no longer an active employee flips the
    change to needs_human (the reply can't be trusted as an owner update).
    Duplicate processing is idempotent via last_processed_message_id.
    """
    thread_to_key: dict[str, str] = {}
    # Thread IDs live in the sent store (phase 1 records them per key).
    thread_map = sent_store.thread_to_key() if hasattr(sent_store, "thread_to_key") else {}
    ingested = 0
    misattributed = 0
    unmatched_threads = 0
    for thread_id, key in thread_map.items():
        context = key_context.get(key)
        if context is None:
            unmatched_threads += 1
            continue
        record = followup.get(key)
        try:
            messages = read_thread(thread_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reply read failed for thread %s: %s", thread_id, exc)
            followup.record_event(key, "reply_read_failed", f"{type(exc).__name__}: {exc}", today)
            continue
        if not isinstance(messages, list):
            continue
        last_seen = record.get("last_processed_message_id")
        new_messages = []
        started = last_seen is None
        for message in messages:
            if not isinstance(message, dict):
                continue
            if not started:
                if str(message.get("id") or "") == str(last_seen):
                    started = True
                continue
            new_messages.append(message)
        for message in new_messages:
            message_id = str(message.get("id") or "")
            if _is_own_message(message):
                record["last_processed_message_id"] = message_id
                continue
            text = str(message.get("text") or message.get("snippet") or "")
            token_digits = _policy_like_token_digits(text)
            change_digits = str(context.get("policy_digits") or "")
            if token_digits and change_digits and change_digits not in token_digits:
                # The reply names a DIFFERENT policy (CSR pasted the wrong
                # thread or answered two nags at once). Never let it move
                # this change's status.
                misattributed += 1
                followup.record_event(
                    key, "misattributed_reply",
                    f"reply names policy digits {sorted(token_digits)}; change is "
                    f"{change_digits}; status untouched",
                    today,
                )
                record["last_processed_message_id"] = message_id
                continue
            signal = classify_reply(text)
            excerpt = " ".join(text.split())[:200]
            record["last_reply_date"] = _reply_date(message) or today.isoformat()
            record["last_reply_excerpt"] = excerpt
            record["last_processed_message_id"] = message_id
            ingested += 1
            if signal == STATUS_NO_SIGNAL:
                followup.record_event(key, "reply_no_signal",
                                      f"ambiguous/automated reply kept as no_signal: {excerpt}", today)
                continue
            csr = str(context.get("csr") or "")
            if csr_is_active is not None and csr and not csr_is_active(csr):
                followup.set_status(
                    key, STATUS_NEEDS_HUMAN, today,
                    f"reply from {csr}, who is no longer an active employee: {excerpt}",
                )
                continue
            followup.set_status(key, signal, today, f"CSR reply: {excerpt}")
    return {
        "threads_checked": len(thread_map),
        "replies_ingested": ingested,
        "misattributed_replies": misattributed,
        "unmatched_threads": unmatched_threads,
    }


# -- confirmation checking -----------------------------------------------------

ENDORSEMENT_NAME_HINTS = (
    "endorsement",
    "endo ",
    "endo-",
    "revised dec",
    "dec page",
    "declarations",
    "amendment",
    "rider",
    "policy change",
)


def find_endorsement_documents(
    documents: list[Mapping[str, Any]], policy_number: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split applicant documents into endorsement candidates vs ignored.

    A candidate must name the change's policy (its digit string appears
    as a standalone run in the document name) AND carry an
    endorsement-type hint. Documents naming a DIFFERENT policy are
    returned as ignored — they must never confirm this change. An
    endorsement-type name with no attributable policy token is neither:
    it carries no signal either way.
    (DocumentApi search only returns id + name; there are no
    machine-readable insured/effective-date/premium fields on the doc.)
    """
    digits = policy_digits(policy_number)
    candidates: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        name = str(doc.get("name") or "")
        lowered = name.casefold()
        if not any(hint in lowered for hint in ENDORSEMENT_NAME_HINTS):
            continue
        entry = {"id": str(doc.get("id") or ""), "name": name}
        if _name_mentions_policy_digits(name, digits):
            candidates.append(entry)
            continue
        other = [d for d in _policy_like_token_digits(name) if d != digits]
        if other:
            ignored.append(entry)
    return candidates, ignored


def evaluate_confirmation(
    *,
    item: Mapping[str, Any],
    still_on_open_queue: bool,
    documents: list[Mapping[str, Any]],
    reply_text: str = "",
    reply_digits: str = "",
) -> dict[str, Any]:
    """Verdict for one docs_claimed (or escalated) change. Fail-closed.

    confirmed: the change is no longer on the open 4359 queue — the EZLynx
        request itself closed, which is the authoritative signal. Nothing
        else can confirm: DocumentApi metadata has no machine-readable
        insured/effective-date/premium fields to compare.
    discrepancy: exact mismatches found (e.g. the CSR's reply names a
        different policy number than the change).
    needs_human: endorsement doc found but fields need a human comparison,
        or no endorsement doc found — both sides summarized, never guessed.
    """
    policy_number = normalize_policy_number(item.get("Policy Number"))
    digits = policy_digits(policy_number)
    account = str(item.get("Account Name") or "").strip()
    if not still_on_open_queue:
        return {
            "verdict": STATUS_CONFIRMED,
            "detail": (
                f"{account} / {policy_number}: change request is no longer "
                "on the open 4359 queue — EZLynx shows it closed."
            ),
        }
    claimed_digits = str(reply_digits or "").strip()
    token_digits = {claimed_digits} if claimed_digits else _policy_like_token_digits(reply_text)
    if digits and token_digits and digits not in token_digits:
        offending = ", ".join(sorted(token_digits - {digits}))
        return {
            "verdict": STATUS_DISCREPANCY,
            "detail": (
                f"{account} / {policy_number}: the CSR's reply names policy "
                f"digits {offending}, which do not match this change's "
                f"{digits}. Exact mismatch — human must untangle."
            ),
        }
    candidates, ignored = find_endorsement_documents(documents, policy_number)
    ignored_note = ""
    if ignored:
        ignored_note = (
            " Ignored (never confirming on these): "
            + "; ".join(f"{doc['name']} (doc {doc['id']})" for doc in ignored[:5])
            + "."
        )
    if candidates:
        best = candidates[0]
        return {
            "verdict": STATUS_NEEDS_HUMAN,
            "detail": (
                f"{account} / {policy_number}: found endorsement-type document "
                f"\"{best['name']}\" (doc {best['id']}). Document metadata has "
                "no machine-readable named-insured / effective-date / premium "
                "fields, so a human must compare the endorsement against the "
                f"original request (opened {item.get('created_date') or 'unknown date'})."
                + ignored_note
            ),
        }
    return {
        "verdict": STATUS_NEEDS_HUMAN,
        "detail": (
            f"{account} / {policy_number}: no endorsement-type document found "
            f"on the applicant record. Request opened "
            f"{item.get('created_date') or 'unknown date'}; CSR signal so far: "
            f"{(reply_text[:120] + '...') if reply_text else 'none'}."
            + ignored_note
        ),
    }


# -- 14-day escalation ---------------------------------------------------------


def escalation_due(
    record: Mapping[str, Any], first_nag_date: date | None, today: date
) -> bool:
    """A change escalates when its first nag is MORE than 14 days ago and
    it is not confirmed and has not been escalated before.

    Boundary: 13 days -> no, 14 days -> no, 15 days -> yes.
    """
    if record.get("status") == STATUS_CONFIRMED:
        return False
    if record.get("escalated_date"):
        return False
    if first_nag_date is None:
        return False
    return (today - first_nag_date).days > ESCALATION_AFTER_DAYS


def _missing_summary(record: Mapping[str, Any], item: Mapping[str, Any]) -> str:
    status = str(record.get("status") or STATUS_NO_SIGNAL)
    excerpt = str(record.get("last_reply_excerpt") or "").strip()
    confirmation = record.get("confirmation") or {}
    if status == STATUS_CONFIRMED:
        return "confirmed — nothing missing"
    if status == STATUS_DOCS_CLAIMED:
        return (
            "CSR says the endorsement is in; "
            + str(confirmation.get("detail") or "awaiting document comparison")
        )
    if status == STATUS_BLOCKED:
        return "CSR reports blocked" + (f": {excerpt}" if excerpt else " (no detail yet)")
    if status == STATUS_IN_PROGRESS:
        return "CSR is working on it" + (f": {excerpt}" if excerpt else " (no update yet)")
    if status == STATUS_DISCREPANCY:
        return str(confirmation.get("detail") or "field mismatch needs untangling")
    if status == STATUS_NEEDS_HUMAN:
        return str(confirmation.get("detail") or "needs a human comparison")
    return "no reply from the CSR yet"


def build_escalation_email(
    changes: list[dict[str, Any]], today: date
) -> tuple[str, str, str]:
    """(subject, text_body, html_body) for Carlo's 14-day digest.

    Each change: insured name, policy number, age in days, assigned CSR,
    last reply/status, and what's missing. Plain English, concise.
    """
    lines = [
        "Hi Carlo,",
        "",
        "These policy change requests still aren't confirmed more than "
        f"{ESCALATION_AFTER_DAYS} days after the first nag:",
        "",
    ]
    html_items = []
    for change in sorted(changes, key=lambda c: int(c.get("age_days") or 0), reverse=True):
        account = str(change.get("account_name") or "Unknown account").strip()
        policy = str(change.get("policy_number") or "").strip()
        age = int(change.get("age_days") or 0)
        csr = str(change.get("csr") or "").strip()
        status = str(change.get("status") or STATUS_NO_SIGNAL).replace("_", " ")
        last_reply = str(change.get("last_reply_date") or "no reply").strip()
        missing = str(change.get("missing") or "").strip()
        bullet = (
            f"{account} — policy {policy}, {age} days since the request opened. "
            f"CSR: {csr}. Status: {status} (last reply: {last_reply}). "
            f"What's missing: {missing}"
        )
        lines.append(f"- {bullet}")
        html_items.append(f"<li>{html.escape(bullet)}</li>")
    lines += [
        "",
        "Nothing here was auto-closed — every item above needs a human "
        "decision or a nudge to the CSR.",
        "",
        "-Robie",
    ]
    text_body = "\n".join(lines)
    html_body = "\n".join([
        "<div>",
        "<p>Hi Carlo,</p>",
        f"<p>These policy change requests still aren&apos;t confirmed more than "
        f"{ESCALATION_AFTER_DAYS} days after the first nag:</p>",
        f"<ul>{''.join(html_items)}</ul>",
        "<p>Nothing here was auto-closed — every item above needs a human "
        "decision or a nudge to the CSR.</p>",
        "<p>-Robie</p>",
        "</div>",
    ])
    return ESCALATION_SUBJECT, text_body, html_body


# -- default injected dependencies (all replaceable in tests) ------------------


def default_reply_reader(mailbox: str = REPORT_MAILBOX) -> Callable[[str], list[dict[str, Any]]]:
    """Read Gmail threads in Robie's mailbox (read-only, keyless delegation).

    Returns read_thread(thread_id) -> messages oldest-first as
    {"id", "from", "date", "text"}. Only the threads phase 1 recorded are
    ever fetched.
    """
    from .ringcentral_email_sync import build_keyless_report_mailbox_service

    service_account_email = os.environ.get(
        "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
    if not service_account_email:
        raise PolicyChangeReportContractError(
            "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT is not configured"
        )
    service = build_keyless_report_mailbox_service(service_account_email, mailbox)

    def _part_text(payload: Mapping[str, Any]) -> str:
        texts: list[str] = []
        mime = str(payload.get("mimeType") or "")
        body = payload.get("body") or {}
        data = body.get("data") if isinstance(body, dict) else None
        if data and mime.startswith("text/plain"):
            import base64
            try:
                texts.append(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
        for part in payload.get("parts") or []:
            if isinstance(part, dict):
                texts.append(_part_text(part))
        return "\n".join(texts)

    def _headers(payload: Mapping[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for header in payload.get("headers") or []:
            if isinstance(header, dict) and header.get("name"):
                out[str(header["name"]).casefold()] = str(header.get("value") or "")
        return out

    def read_thread(thread_id: str) -> list[dict[str, Any]]:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full").execute()
        messages: list[dict[str, Any]] = []
        for raw in thread.get("messages", []) or []:
            if not isinstance(raw, dict):
                continue
            payload = raw.get("payload") or {}
            headers = _headers(payload)
            messages.append({
                "id": str(raw.get("id") or ""),
                "from": headers.get("from", ""),
                "date": headers.get("date", ""),
                "text": _part_text(payload) or str(raw.get("snippet") or ""),
            })
        return messages

    return read_thread


def default_document_search(applicant_id: str) -> list[dict[str, Any]]:
    """DocumentApi search for one applicant (read-only). Returns [{id, name}]."""
    from .ezlynx_api import EzlynxApiClient, extract_document_api_results, load_ezlynx_api_config

    applicant = str(applicant_id or "").strip()
    if not applicant:
        raise PolicyChangeReportContractError("applicant id is required for document search")
    try:
        client = EzlynxApiClient(load_ezlynx_api_config())
        payload = client.search_applicant_documents(applicant)
    except Exception as exc:  # noqa: BLE001
        raise PolicyChangeReportContractError(
            f"DocumentApi search failed for applicant {applicant}: {type(exc).__name__}: {exc}"
        ) from exc
    return [{"id": row["id"], "name": row["name"]}
            for row in extract_document_api_results(payload)]


def default_escalation_mailer(*, to: list[str], subject: str,
                              text_body: str, html_body: str) -> dict[str, Any]:
    """Send the escalation digest from robie@ via keyless-delegated Gmail."""
    from .overdue_policy_change_reports import default_mailer

    return default_mailer(
        to=to, cc=[], subject=subject, text_body=text_body, html_body=html_body,
    )


def open_queue_rows(queue_reader: Callable[[Mapping[str, Any]], list[dict[str, str]]],
                     payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """All Open 4359 rows (any age) — the authoritative still-open set."""
    rows = queue_reader(payload)
    return [
        row for row in rows
        if str(row.get("Request Status") or "").strip().casefold() == "open"
    ]


class PolicyChangeFollowupWorker:
    """Daily reply tracking + confirmation checking + Tuesday escalation.

    All external access is injected for tests; defaults are read-only and
    fail closed. Never writes to EZLynx. The only writes are the
    escalation email (live mode) and the state files.
    """

    def __init__(
        self,
        *,
        queue_reader: Callable[[Mapping[str, Any]], list[dict[str, str]]] | None = None,
        reply_reader: Callable[[str], list[dict[str, Any]]] | None = None,
        document_search: Callable[[str], list[dict[str, Any]]] | None = None,
        escalation_mailer: Callable[..., dict[str, Any]] | None = None,
        sent_store: Any | None = None,
        followup_store: FollowupStore | None = None,
        csr_is_active: Callable[[str], bool] | None = None,
    ) -> None:
        self.queue_reader = queue_reader or default_queue_reader
        self.reply_reader = reply_reader  # built per-run (needs mailbox); None = default
        self.document_search = document_search or default_document_search
        self.escalation_mailer = escalation_mailer or default_escalation_mailer
        self.sent_store = sent_store
        self.followup_store = followup_store
        self.csr_is_active = csr_is_active

    def perform(self, job: dict[str, Any], *, dry_run: bool = True,
                today: date | None = None) -> dict[str, Any]:
        """Run one daily cycle. Returns the evidence dict (also the summary).

        dry_run: fake the escalation send and never persist state.
        today: injectable clock (tests); defaults to the real date.
        """
        from .overdue_policy_change_reports import NotificationStore

        payload = dict(job.get("payload") or {})
        today = today or date.today()
        sent_store = self.sent_store or NotificationStore(
            payload.get("sent_store_path")
            or Path("~/.robie/overdue_policy_change_reports/sent.json").expanduser()
        )
        followup_path = (
            payload.get("followup_store_path")
            or Path("~/.robie/overdue_policy_change_reports/followup.json").expanduser()
        )
        followup: FollowupStore = (
            self.followup_store
            or (DryRunFollowupStore(followup_path) if dry_run else FollowupStore(followup_path))
        )

        evidence: dict[str, Any] = {
            "job_type": JOB_TYPE,
            "ran_at": _utc_now(),
            "mode": "dry-run" if dry_run else "live",
            "succeeded": False,
            "error": None,
        }
        try:
            rows = open_queue_rows(self.queue_reader, payload)
            # Notification keys MUST match phase 1's: phase 1 builds them
            # from the normalized ISO created_date (row_created_date), not
            # the raw CSV text (which may be MM/DD/YYYY). A mismatch here
            # would orphan every thread -> key join.
            open_keys: dict[str, dict[str, str]] = {}
            for row in rows:
                created_iso = row_created_date(row).isoformat()
                key = notification_key(
                    str(row.get("CSR") or ""),
                    str(row.get("Policy Number") or ""),
                    created_iso,
                )
                open_keys[key] = {**row, "created_date": created_iso}
            # key_context for reply attribution (only nags we actually sent).
            # Context is persisted on the followup record so a change that
            # drops off the queue keeps its identity.
            key_context: dict[str, dict[str, Any]] = {}
            first_nag: dict[str, date | None] = {}
            thread_map = (
                sent_store.thread_to_key()
                if hasattr(sent_store, "thread_to_key") else {}
            )
            for key in set(thread_map.values()):
                row = open_keys.get(key)
                stored = followup.get(key).get("context") or {}
                context = _context_from_row(row) if row else dict(stored)
                if context:
                    record = followup.get(key)
                    merged = dict(record.get("context") or {})
                    merged.update({k: v for k, v in context.items() if v})
                    record["context"] = merged
                    key_context[key] = merged
                else:
                    # Legacy key with no stored context and no open row:
                    # replies can't be attributed by policy digits.
                    key_context[key] = {
                        "policy_digits": "", "csr": "", "applicant_id": "",
                    }
                first_nag[key] = sent_store.first_sent_date(key) if hasattr(
                    sent_store, "first_sent_date") else None

            if thread_map:
                reader = self.reply_reader or default_reply_reader(
                    str(payload.get("report_mailbox") or REPORT_MAILBOX).strip()
                )
                ingestion = ingest_replies(
                    sent_store=sent_store,
                    followup=followup,
                    read_thread=reader,
                    key_context=key_context,
                    csr_is_active=self.csr_is_active,
                    today=today,
                )
            else:
                ingestion = {
                    "threads_checked": 0,
                    "replies_ingested": 0,
                    "misattributed_replies": 0,
                    "unmatched_threads": 0,
                }

            # Confirmation checking for docs_claimed changes.
            confirmations: list[dict[str, Any]] = []
            for key in followup.keys():
                record = followup.get(key)
                if record.get("status") != STATUS_DOCS_CLAIMED:
                    continue
                row = open_keys.get(key)
                if row is None:
                    # Dropped off the open queue entirely (Request Status no
                    # longer Open): the authoritative close signal.
                    context = record.get("context") or {}
                    verdict = evaluate_confirmation(
                        item={
                            "Policy Number": context.get("policy_number", ""),
                            "Account Name": context.get("account_name", ""),
                            "created_date": context.get("created_date", ""),
                        },
                        still_on_open_queue=False,
                        documents=[],
                        reply_text=record.get("last_reply_excerpt") or "",
                    )
                else:
                    try:
                        documents = self.document_search(
                            str(row.get("Applicant ID") or "").strip()
                        )
                    except PolicyChangeReportContractError as exc:
                        followup.record_event(
                            key, "confirmation_skipped",
                            f"DocumentApi unavailable: {exc}", today)
                        continue
                    verdict = evaluate_confirmation(
                        item={**row, "created_date": str(
                            row.get("created_date") or "").strip()},
                        still_on_open_queue=True,
                        documents=documents if isinstance(documents, list) else [],
                        reply_text=record.get("last_reply_excerpt") or "",
                    )
                record["confirmation"] = {
                    "verdict": verdict["verdict"],
                    "detail": verdict["detail"],
                    "checked": today.isoformat(),
                }
                followup.record_event(
                    key, "confirmation_" + verdict["verdict"], verdict["detail"], today)
                if verdict["verdict"] == STATUS_CONFIRMED:
                    followup.set_status(key, STATUS_CONFIRMED, today, verdict["detail"])
                elif verdict["verdict"] == STATUS_DISCREPANCY:
                    followup.set_status(key, STATUS_DISCREPANCY, today, verdict["detail"])
                confirmations.append({"key": key, **verdict})
            # A change that left the open queue is closed — the request is
            # no longer Open in 4359, which is the authoritative signal.
            # Confirm it quietly regardless of prior reply status (a
            # discrepancy/needs_human that later closed is done, not stuck).
            for key in list(followup.keys()):
                if key in open_keys:
                    continue
                record = followup.get(key)
                if record.get("status") == STATUS_CONFIRMED:
                    continue
                context = record.get("context") or {}
                verdict = evaluate_confirmation(
                    item={
                        "Policy Number": context.get("policy_number", ""),
                        "Account Name": context.get("account_name", ""),
                        "created_date": context.get("created_date", ""),
                    },
                    still_on_open_queue=False,
                    documents=[],
                )
                record["confirmation"] = {
                    "verdict": verdict["verdict"],
                    "detail": verdict["detail"] + " (was: " + str(record.get("status")) + ")",
                    "checked": today.isoformat(),
                }
                followup.set_status(key, STATUS_CONFIRMED, today, verdict["detail"])

            # Tuesday escalation digest to Carlo.
            escalation: dict[str, Any] = {"emitted": False, "changes": []}
            if today.weekday() == 1:  # Tuesday — same weekly rhythm as the nag run
                due_changes: list[dict[str, Any]] = []
                for key in followup.keys():
                    record = followup.get(key)
                    nag_date = first_nag.get(key)
                    if not escalation_due(record, nag_date, today):
                        continue
                    row = open_keys.get(key)
                    created_raw = str((row or {}).get("created_date") or "").strip()
                    context = record.get("context") or {}
                    try:
                        created = datetime.strptime(created_raw, "%Y-%m-%d").date()
                        age = (today - created).days
                    except ValueError:
                        age = -1
                    due_changes.append({
                        "account_name": (
                            str((row or {}).get("Account Name") or "").strip()
                            or str(context.get("account_name") or "").strip()
                        ),
                        "policy_number": (
                            normalize_policy_number((row or {}).get("Policy Number"))
                            or str(context.get("policy_number") or "").strip()
                        ),
                        "age_days": age,
                        "csr": (
                            str((row or {}).get("CSR") or "").strip()
                            or str(context.get("csr") or "").strip()
                        ),
                        "status": record.get("status"),
                        "last_reply_date": record.get("last_reply_date"),
                        "missing": _missing_summary(record, row or {}),
                    })
                if due_changes:
                    subject, text_body, html_body = build_escalation_email(due_changes, today)
                    if dry_run:
                        receipt = {
                            "kind": "dry-run",
                            "destination": [ESCALATION_TO],
                            "subject": subject,
                            "text_body": text_body,
                            "html_body": html_body,
                            "message_id": "dry-run-escalation",
                            "sender": FOLLOWUP_SENDER,
                        }
                    else:
                        receipt = self.escalation_mailer(
                            to=[ESCALATION_TO], subject=subject,
                            text_body=text_body, html_body=html_body,
                        )
                    for key in followup.keys():
                        record = followup.get(key)
                        if escalation_due(record, first_nag.get(key), today):
                            record["escalated_date"] = today.isoformat()
                            followup.record_event(
                                key, "escalated",
                                f"included in {ESCALATION_AFTER_DAYS}-day digest to {ESCALATION_TO}",
                                today,
                            )
                    escalation = {
                        "emitted": True,
                        "changes": due_changes,
                        "receipt": receipt,
                    }

            if not dry_run:
                followup.save()
            else:
                DryRunFollowupStore(followup.path).save()  # logs the NOT-saved line

            evidence.update({
                "succeeded": True,
                "open_queue_rows": len(open_keys),
                "tracked_changes": len(followup.keys()),
                "reply_ingestion": ingestion,
                "confirmations": confirmations,
                "escalation": escalation,
                "status_counts": _status_counts(followup),
            })
        except PolicyChangeReportContractError as exc:
            evidence["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            evidence["error"] = f"{type(exc).__name__}: {exc}"
        return evidence


def _status_counts(followup: FollowupStore) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in followup.keys():
        status = str(followup.get(key).get("status") or STATUS_NO_SIGNAL)
        counts[status] = counts.get(status, 0) + 1
    return counts
