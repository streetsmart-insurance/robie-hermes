"""Durable human-clearable queue for genuine hello@ requests the worker
could not match to an applicant.

Same pattern as the certificate unmatched queue (PR #610): when the
hello intake worker holds a GENUINE request for identity reasons (no
applicant matched — ambiguous name, no report hit, no policy anchor),
the request must not die in a silent hold. It goes here instead: a JSONL
queue a human clears daily, oldest first.

Each entry carries the classified request_type (new_business, renewal,
midterm, client_issue, carrier_notice, billing, endorsement, document,
general_question) and the hello routing role (originating_producer /
applicable_csr) so the human clearing it knows where the work goes.

Read-only with respect to EZLynx: this module never calls the EZLynx API,
never fires Zapier, never sends email. It appends to a local JSONL queue
file, renders a human review report, and — only on explicit human
resolution — appends a sender->applicant mapping to the local hello
sender-alias store that the intake worker reads.

Vendor/system/carrier senders (compliance platforms, lender servicers,
carrier producer-notice addresses, premium-finance collectors, agency
internal) represent many unrelated insureds or are not the client, and
must NEVER become fixed sender aliases: resolving a queue entry from
such a sender records the applicant for that message only and writes no
alias.

Self-contained: no dependency on the certificate branches (PR #609 /
PR #610). This module can merge and run independently of them.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "robie_job_engine", "data")


def default_queue_path() -> str:
    """Queue file location. Overridable with HELLO_UNMATCHED_QUEUE_PATH."""
    return os.environ.get(
        "HELLO_UNMATCHED_QUEUE_PATH",
        os.path.join(_DEFAULT_DATA_DIR, "hello_unmatched_queue.jsonl"),
    )


def default_alias_store_path() -> str:
    """Sender-alias store location. Overridable with HELLO_SENDER_ALIASES_PATH."""
    return os.environ.get(
        "HELLO_SENDER_ALIASES_PATH",
        os.path.join(_DEFAULT_DATA_DIR, "hello_sender_aliases.json"),
    )


# ---------------------------------------------------------------------------
# Vendor / system / carrier senders: NEVER become fixed sender aliases
# ---------------------------------------------------------------------------
# One address here covers many unrelated insureds (compliance platforms,
# lender servicers, carrier producer-notice mail, premium-finance
# collectors) or is agency-internal. Per-message insured extraction only;
# a queue resolution records the applicant for that message and writes no
# alias. Subdomains match too. This extends the certificate queue's guard
# with hello-observed senders (Ascend, PHLY producer notices,
# Brown & Joseph, BriteCore, SWYFFT).

VENDOR_SENDER_DOMAINS = frozenset({
    # Compliance / certificate-tracking platforms (same as cert queue)
    "certs.highway.com",
    "insurance.highway.com",
    "highway.com",
    "registrymonitoring.com",   # RMIS
    "truckstop.com",            # RMIS help desk
    "trustlayer.io",
    "mycoitracking.com",        # myCOI
    "certificial.ai",
    "operfi.com",
    # Lender / servicer insurance tracking
    "assurant.com",
    # Vendor credentialing / automated notices
    "realpage.com",
    "myinsuranceinfo.com",
    "plus1solutions.net",
    # Hello-observed: premium finance / carrier / agency-ops systems
    "useascend.com",            # Ascend accounting (return premium notices)
    "phly.com",                 # PHLY producer notices (carrier, not client)
    "brownandjoseph.net",       # premium-finance collections
    "britecore.com",            # agency-ops vendor billing
    "swyfft.com",               # carrier marketing
    # Agency internal — never alias our own people
    "streetsmart.insurance",
    "ssinj.com",
})


def _domain_of(email: str) -> str:
    parts = (email or "").strip().lower().split("@")
    return parts[-1] if len(parts) == 2 else ""


def sender_is_vendor(sender_email: str) -> bool:
    """True when the sender address must never become a fixed alias."""
    domain = _domain_of(sender_email)
    if not domain:
        return False
    return any(domain == v or domain.endswith("." + v)
               for v in VENDOR_SENDER_DOMAINS)


# ---------------------------------------------------------------------------
# Queue store (JSONL, one entry per line)
# ---------------------------------------------------------------------------

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

_ENTRY_ID_RE = re.compile(r"^hu-(\d{6})$")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_lines(queue_path: str) -> list[dict]:
    entries: list[dict] = []
    if not os.path.exists(queue_path):
        return entries
    with open(queue_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return entries


def _next_entry_id(entries: list[dict]) -> str:
    best = 0
    for e in entries:
        m = _ENTRY_ID_RE.match(str(e.get("entry_id", "")))
        if m:
            best = max(best, int(m.group(1)))
    return f"hu-{best + 1:06d}"


def load_entries(queue_path: str | None = None) -> list[dict]:
    """All queue entries, oldest first."""
    path = queue_path or default_queue_path()
    entries = _read_lines(path)
    entries.sort(key=lambda e: (e.get("created_at", ""), e.get("entry_id", "")))
    return entries


def open_entries(queue_path: str | None = None) -> list[dict]:
    """Unresolved entries, oldest first."""
    return [e for e in load_entries(queue_path)
            if e.get("status", STATUS_OPEN) == STATUS_OPEN]


def enqueue_unmatched(*, sender_email: str, subject: str,
                      hold_reason: str,
                      request_type: str | None = None,
                      route: str | None = None,
                      company_name: str | None = None,
                      sender_name: str | None = None,
                      policy_numbers: list[str] | None = None,
                      mc_numbers: list[str] | None = None,
                      strategies_tried: list[str] | None = None,
                      gmail_id: str | None = None,
                      message_id: str | None = None,
                      evidence: list[str] | None = None,
                      queue_path: str | None = None) -> dict:
    """Append a genuine-but-unmatched hello@ request to the queue.

    Fail-closed: sender_email and hold_reason are required. If an OPEN
    entry already exists for the same gmail_id (re-run / duplicate
    sweep), the existing entry is returned and nothing is appended.
    """
    sender_email = (sender_email or "").strip()
    hold_reason = (hold_reason or "").strip()
    if not sender_email:
        raise ValueError("enqueue_unmatched requires sender_email")
    if not hold_reason:
        raise ValueError("enqueue_unmatched requires hold_reason")

    path = queue_path or default_queue_path()
    entries = _read_lines(path)

    if gmail_id:
        for e in entries:
            if (e.get("status", STATUS_OPEN) == STATUS_OPEN
                    and e.get("gmail_id") == gmail_id):
                return e  # already queued — never double-queue

    entry = {
        "entry_id": _next_entry_id(entries),
        "created_at": _utcnow_iso(),
        "status": STATUS_OPEN,
        "sender_email": sender_email,
        "sender_name": (sender_name or "").strip() or None,
        "subject": (subject or "").strip(),
        "request_type": (request_type or "").strip() or None,
        "route": (route or "").strip() or None,
        "company_name": (company_name or "").strip() or None,
        "policy_numbers": list(policy_numbers or []),
        "mc_numbers": list(mc_numbers or []),
        "hold_reason": hold_reason,
        "strategies_tried": list(strategies_tried or []),
        "gmail_id": gmail_id,
        "message_id": message_id,
        "evidence": list(evidence or []),
        "resolved_at": None,
        "resolved_by": None,
        "resolution": None,
        "alias_written": False,
        "notes": [],
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = 140) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_markdown(entries: list[dict] | None = None,
                    queue_path: str | None = None) -> str:
    """Markdown table of open entries, oldest first — the daily human review."""
    if entries is None:
        entries = open_entries(queue_path)
    lines = [
        "# Hello unmatched queue — human review",
        "",
        f"Generated (UTC): {_utcnow_iso()} — open entries: {len(entries)}",
        "",
    ]
    if not entries:
        lines.append("Queue is clear. Nothing awaiting human matching.")
        return "\n".join(lines) + "\n"
    lines.append(
        "| ID | Date | Sender | Company | Type | Route | Policy / MC | "
        "Why held | Strategies tried | Evidence |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for e in entries:
        ids = ", ".join(list(e.get("policy_numbers") or [])
                        + list(e.get("mc_numbers") or [])) or "—"
        lines.append("| " + " | ".join([
            e.get("entry_id", ""),
            (e.get("created_at", "") or "")[:10],
            e.get("sender_email", ""),
            _truncate(e.get("company_name") or "—", 50),
            e.get("request_type") or "—",
            e.get("route") or "—",
            _truncate(ids, 36),
            _truncate(e.get("hold_reason", ""), 80),
            _truncate(", ".join(e.get("strategies_tried") or []), 50),
            _truncate("; ".join(e.get("evidence") or []), 110),
        ]) + " |")
    lines.append("")
    lines.append(
        "Resolve with: "
        "`hello_unmatched_cli.py resolve <ID> --applicant-id <N> --by <name>` "
        "or `--not-our-client`. Vendor/carrier/system senders never get aliases.")
    return "\n".join(lines) + "\n"


def render_json(entries: list[dict] | None = None,
                queue_path: str | None = None) -> dict:
    """Machine-readable companion to the markdown report."""
    if entries is None:
        entries = open_entries(queue_path)
    return {
        "generated_at": _utcnow_iso(),
        "open_count": len(entries),
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Sender-alias store (shared with the hello intake worker)
# ---------------------------------------------------------------------------

def _blank_alias_store() -> dict:
    return {
        "generated": _utcnow_iso()[:10],
        "scope": "hello intake sender aliases",
        "rule": ("alias saved only when the sender is the client themselves "
                 "and a human confirmed the match. Vendor, carrier, broker, "
                 "holder, lender, collector, and internal senders are NEVER "
                 "aliased."),
        "aliases": [],
    }


def load_alias_store(alias_store_path: str | None = None) -> dict:
    path = alias_store_path or default_alias_store_path()
    if not os.path.exists(path):
        return _blank_alias_store()
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if (not isinstance(data, dict)
            or not isinstance(data.get("aliases"), list)):
        raise ValueError(
            f"alias store at {path} has an unexpected schema — refusing "
            "to write rather than corrupt it")
    return data


def save_alias_store(data: dict,
                     alias_store_path: str | None = None) -> str:
    path = alias_store_path or default_alias_store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

class QueueError(RuntimeError):
    """Human-actionable queue failure (bad id, already resolved, bad input)."""


def _rewrite_entries(queue_path: str, entries: list[dict]) -> None:
    with open(queue_path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def resolve_entry(entry_id: str, *, applicant_id: int | str | None = None,
                  account_name: str | None = None,
                  request_type: str | None = None,
                  not_our_client: bool = False,
                  resolved_by: str = "",
                  no_alias: bool = False,
                  queue_path: str | None = None,
                  alias_store_path: str | None = None) -> dict:
    """Human resolution of one queue entry.

    Exactly one of applicant_id / not_our_client is required. On a real
    match the sender->applicant mapping is appended to the hello
    sender-alias store (confidence "strong": a human confirmed it) so
    future emails from that sender auto-resolve — UNLESS the sender is a
    vendor/system/carrier address or the human passed no_alias, in which
    case the entry resolves with no alias written.

    request_type may be corrected by the human (the classifier's guess is
    a starting point); it is recorded on the resolution for routing.
    """
    resolved_by = (resolved_by or "").strip()
    if not resolved_by:
        raise QueueError("resolve requires --by <name>: who made the call")
    if bool(applicant_id) == bool(not_our_client):
        raise QueueError(
            "resolve requires exactly one of --applicant-id or "
            "--not-our-client")

    applicant_int: int | None = None
    if applicant_id is not None:
        try:
            applicant_int = int(str(applicant_id).strip())
        except (TypeError, ValueError):
            raise QueueError(
                f"applicant id {applicant_id!r} is not a number — "
                "never guess an applicant")
        if applicant_int <= 0:
            raise QueueError(
                f"applicant id {applicant_int} is not valid — "
                "never guess an applicant")

    path = queue_path or default_queue_path()
    entries = _read_lines(path)
    target = next((e for e in entries
                   if e.get("entry_id") == entry_id), None)
    if target is None:
        raise QueueError(f"no queue entry {entry_id}")
    if target.get("status") != STATUS_OPEN:
        raise QueueError(f"entry {entry_id} is already "
                         f"{target.get('status')}")

    resolution: dict
    alias_written = False
    note: str | None = None

    if not_our_client:
        resolution = {"not_our_client": True}
    else:
        sender = target["sender_email"]
        final_type = (request_type or "").strip() or target.get("request_type")
        resolution = {
            "applicant_id": applicant_int,
            "account_name": (account_name or "").strip() or None,
            "request_type": final_type,
        }
        if sender_is_vendor(sender):
            # Vendor/system/carrier sender: per-message extraction only. The
            # applicant is recorded for THIS message; no alias is written.
            note = (f"vendor/system/carrier sender {sender} — resolved per "
                    "message, no fixed alias written (one address covers "
                    "many insureds)")
        elif no_alias:
            note = ("human chose no alias (broker/holder/lender sender) — "
                    "resolved per message only")
        else:
            store = load_alias_store(alias_store_path)
            aliases = store.setdefault("aliases", [])
            sender_key = sender.strip().lower()
            record = {
                "sender": sender_key,
                "applicant_id": applicant_int,
                "account_name": (account_name or "").strip() or None,
                "confidence": "strong",
                "evidence": (
                    f"human-resolved via hello unmatched queue {entry_id} by "
                    f"{resolved_by}; company per email "
                    f"{target.get('company_name') or '—'}; "
                    f"type {final_type or '—'}"),
            }
            replaced = False
            for i, a in enumerate(aliases):
                if str(a.get("sender", "")).strip().lower() == sender_key:
                    aliases[i] = record
                    replaced = True
                    break
            if not replaced:
                aliases.append(record)
            save_alias_store(store, alias_store_path)
            alias_written = True
            if replaced:
                note = f"alias for {sender_key} updated (was already present)"

    target["status"] = STATUS_RESOLVED
    target["resolved_at"] = _utcnow_iso()
    target["resolved_by"] = resolved_by
    target["resolution"] = resolution
    target["alias_written"] = alias_written
    if note:
        target.setdefault("notes", []).append(note)
    _rewrite_entries(path, entries)
    return target
