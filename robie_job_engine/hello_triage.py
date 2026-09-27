#!/usr/bin/env python3
"""Hello@ Phase 1 — read-only triage dry run.

Builds an evidence pack from unread hello@ mail: classifies each message,
extracts carrier-notice fields, resolves the EZLynx applicant via the
Policy API (fail closed — never guessed), names the existing discussion the
note would file under, and drafts the one-line note.

Phase 1 is evidence only. Nothing is labeled, moved, marked read, or
written to EZLynx. A live filing step would be a separate, explicitly
approved Phase 2.

Design authority: ~/workspace/robie-ops/hello-certificates-phase1-spec.md

Collaborator interfaces (inject fakes in tests, real clients in the dry run):
  gmail            .search_meta(query, max_results) -> [{id,from,to,subject,date,snippet,labels}]
                   .get_body(msg_id) -> str | None
  policy_search    .search_policy_by_number(number) -> {"status":..,"data": [rows]}
  discussion_lister.get_discussions(applicant_id) -> [discussion records]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from typing import Any

from .ezlynx_discussions import (
    DiscussionSelectionError,
    discussion_id_of,
    discussion_title_of,
    reject_phone_numbers,
    select_discussion_for_note,
)

MAILBOX_DEFAULT = "hello@streetsmart.insurance"

CARRIER_NOTICE = "carrier_notice"
VENDOR = "vendor"
SURVEY = "survey"
JUNK = "junk"
NEEDS_REVIEW = "needs_review"

# ---------------------------------------------------------------------------
# Classification signals (starting rules; the evidence pack records the reason
# for every decision so a human can verify and the rules can be tuned).
# ---------------------------------------------------------------------------

_INTERNAL_DOMAINS = ("streetsmart.insurance",)

# Bulk-mail platforms: almost always marketing when they hit hello@.
_BULK_MAIL_DOMAINS = (
    "ccsend.com",
    "constantcontact.com",
    "mailchimp.com",
    "mailchi.mp",
    "sendgrid.net",
    "hubspot.com",
    "marketo.com",
)

_JUNK_RE = re.compile(
    r"mailer-daemon|delivery status notification|delivery failure|"
    r"undeliverable|postmaster@|auto-reply|automatic reply|out of office",
    re.IGNORECASE,
)

_SURVEY_RE = re.compile(
    r"\bsurvey\b|feedback|how did we do|rate your|review us on|"
    r"nps|net promoter",
    re.IGNORECASE,
)

# Marketing-shaped mail, even when it comes from a carrier domain.
_VENDOR_RE = re.compile(
    r"webinar|you'?re invited|last chance.*invit|quoted:|newsletter|"
    r"\bpromo\b|promotion|marketing|unsubscribe|coverage solutions|"
    r"find .*quotes fast|commission when|sponsor|looking for .*company\?",
    re.IGNORECASE,
)

_NOTICE_KEYWORDS = (
    "cancellation",
    "cancel notice",
    "non-renewal",
    "nonrenewal",
    "reinstatement",
    "reinstated",
    "audit",
    "endorsement",
    "loss run",
    "expiration",
    "expiring",
    "binder",
    "policy change",
    "renewal",
)
_NOTICE_RE = re.compile("|".join(re.escape(k) for k in _NOTICE_KEYWORDS), re.IGNORECASE)

# Sender domains that can emit real policy notices (carriers + MGAs/brokers).
_CARRIER_DOMAINS = (
    "progressive.com",
    "geico.com",
    "travelers.com",
    "thehartford.com",
    "usli.com",
    "nationwide.com",
    "libertymutual.com",
    "safeco.com",
    "coverwhale.com",
    "rpsins.com",
    "tuscano.com",
    "fmiweb.com",
    "guard.com",
    "amtrustgroup.com",
    "selective.com",
    "chubb.com",
    "erieinsurance.com",
    "auto-owners.com",
    "hanover.com",
    "philadelphiainsurance.com",
    "gaig.com",
    "greatamericaninsurance.com",
    "archinsurance.com",
    "alliedworldinsurance.com",
    "merchantsinsurance.com",
    "keyrisk.com",
    "encova.com",
    "pie-insurance.com",
    "cerity.com",
    "employers.com",
    "nextinsurance.com",
    "biBERK.com",
    "hiscox.com",
)

_CARRIER_PRETTY = {
    "progressive.com": "Progressive",
    "geico.com": "GEICO",
    "travelers.com": "Travelers",
    "thehartford.com": "The Hartford",
    "usli.com": "USLI",
    "nationwide.com": "Nationwide",
    "libertymutual.com": "Liberty Mutual",
    "safeco.com": "Safeco",
    "coverwhale.com": "Cover Whale",
    "rpsins.com": "RPS",
    "tuscano.com": "Tuscano",
    "fmiweb.com": "Franklin Mutual",
    "guard.com": "GUARD",
    "hanover.com": "Hanover",
    "gaig.com": "Great American",
    "hiscox.com": "Hiscox",
}

_POLICY_RE = re.compile(
    r"(?i)pol(?:icy)?(?:\s*(?:no|number|#))?\s*[:#]?\s*"
    # Never capture the words "number"/"no" themselves (backtracking can land
    # on them when the real number sits on the next line), and always require
    # at least one digit — real policy numbers are never all letters.
    r"(?!number\b|no\b)(?=[A-Z0-9\-\./]*\d)"
    r"([A-Z0-9][A-Z0-9\-\./]{4,30})(?![A-Z0-9\-\./])"
)

# Insured-name heuristics (subject line only; body is read for policy numbers).
# Some carriers (e.g. Franklin Mutual) use "<policy#> <INSURED NAME>" subjects.
_BARE_POLICY_SUBJECT_RE = re.compile(r"^(\d{6,10})\s+([A-Z][A-Z .,'-]{2,60})$")

_INSURED_PATTERNS = (
    re.compile(r"(?i)loss runs?\s+for\s+(.+?)\s+(?:pol#|policy\b)"),
    re.compile(r"(?i)loss runs?\s+request\s+(.+?)\s+(?:pol#|policy\b)"),
    re.compile(r"(?i)^(.+?)\s+(?:homeowners|dwelling fire|auto|general liability|gl|wc|workers comp)\s+policy\b"),
    re.compile(r"(?i)customer,\s*(.+?),\s*is on the way"),
    re.compile(r"(?i)refund to your customer,\s*(.+?)(?:,|\s+is|\s*$)"),
    re.compile(r"^\s*\d+\s+([A-Z][A-Z .&'\-]{3,60}?)\s*$"),
)

_NOTICE_TYPE_RULES = (
    ("cancellation", ("cancellation", "cancel notice", "cancelled",
                     "pending cancel", "pending cancellation")),
    ("non-renewal", ("non-renewal", "nonrenewal")),
    ("reinstatement", ("reinstatement", "reinstated")),
    ("audit", ("audit",)),
    ("endorsement", ("endorsement",)),
    ("loss_runs", ("loss run",)),
    ("renewal", ("renewal",)),
    ("expiration", ("expiration", "expiring")),
)

_NOTICE_TYPE_LABEL = {
    "cancellation": "Cancellation",
    "non-renewal": "Non-renewal",
    "reinstatement": "Reinstatement",
    "audit": "Audit",
    "endorsement": "Endorsement",
    "loss_runs": "Loss runs",
    "renewal": "Renewal",
    "expiration": "Expiration",
    "policy_activity": "Policy activity",
}

_APPLICANT_ID_KEYS = ("applicantId", "applicant_id", "accountId", "account_id")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _domain_in(domain: str, candidates: tuple[str, ...]) -> bool:
    domain = (domain or "").lower()
    return any(domain == c.lower() or domain.endswith("." + c.lower())
               for c in candidates)


def _sender_domain(from_header: str) -> str:
    m = re.search(r"@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", from_header or "")
    return m.group(1).lower() if m else ""


def classify_message(meta: dict[str, Any]) -> tuple[str, str]:
    """Return (category, human-readable reason). Fail closed to NEEDS_REVIEW."""
    frm = str(meta.get("from") or "")
    subject = str(meta.get("subject") or "")
    snippet = str(meta.get("snippet") or "")
    domain = _sender_domain(frm)
    text = f"{subject} {snippet}"

    if _JUNK_RE.search(f"{frm} {subject}"):
        return JUNK, "bounce/auto-reply/system-noise signal in sender or subject"
    if _SURVEY_RE.search(subject):
        return SURVEY, "survey/feedback signal in subject"
    if _domain_in(domain, _INTERNAL_DOMAINS):
        return NEEDS_REVIEW, "internal staff mail — not a carrier notice; human decides"
    if _domain_in(domain, _BULK_MAIL_DOMAINS):
        return VENDOR, "bulk-mail platform sender — marketing unless proven otherwise"
    if _VENDOR_RE.search(text):
        return VENDOR, "marketing-shaped signal (webinar/invite/quote blast/newsletter)"
    if _NOTICE_RE.search(subject) or (
        _domain_in(domain, _CARRIER_DOMAINS) and _NOTICE_RE.search(snippet)
    ):
        return CARRIER_NOTICE, "policy-notice signal from a carrier/MGA sender"
    if _domain_in(domain, _CARRIER_DOMAINS) and _POLICY_RE.search(text):
        return CARRIER_NOTICE, "carrier/MGA sender with a policy number in subject/snippet"
    if _domain_in(domain, _CARRIER_DOMAINS) and _BARE_POLICY_SUBJECT_RE.match(
        subject.strip()
    ):
        return CARRIER_NOTICE, "bare policy-number subject from a carrier/MGA sender"
    return NEEDS_REVIEW, "no confident signal — flagged for human review, not guessed"


# ---------------------------------------------------------------------------
# Carrier-notice extraction
# ---------------------------------------------------------------------------

def notice_type_of(subject: str) -> str:
    lowered = (subject or "").lower()
    for ntype, keywords in _NOTICE_TYPE_RULES:
        if any(k in lowered for k in keywords):
            return ntype
    return "policy_activity"


def extract_notice_fields(
    meta: dict[str, Any], body: str | None = None
) -> dict[str, Any]:
    """Extract carrier/insured/policy/notice-type from a carrier notice.

    Missing fields stay None — the caller fails closed on them.
    """
    frm = str(meta.get("from") or "")
    subject = str(meta.get("subject") or "")
    snippet = str(meta.get("snippet") or "")
    domain = _sender_domain(frm)
    carrier = next((_CARRIER_PRETTY[c] for c in _CARRIER_PRETTY if _domain_in(domain, (c,))), domain or None)

    insured = None
    for pat in _INSURED_PATTERNS:
        m = pat.search(subject)
        if m:
            insured = re.sub(r"\s+", " ", m.group(1)).strip(" ,.-")
            break

    policy_number = None
    bare = _BARE_POLICY_SUBJECT_RE.match(subject.strip())
    if bare:
        policy_number, insured = bare.group(1), bare.group(2).strip()
    for text in (subject, snippet, body or ""):
        if policy_number:
            break
        m = _POLICY_RE.search(text or "")
        if m:
            policy_number = re.sub(r"\s+", "", m.group(1)).strip(".,;:")

    return {
        "carrier": carrier,
        "insured": insured or None,
        "policy_number": policy_number,
        "notice_type": notice_type_of(subject),
        "email_date": meta.get("date"),
    }


# ---------------------------------------------------------------------------
# Applicant resolution (fail closed)
# ---------------------------------------------------------------------------

def resolve_applicant_id(policy_search: Any, policy_number: str | None) -> tuple[str | None, str | None]:
    """Return (applicant_id, skip_reason). Never guesses.

    Resolved only when the policy search returns rows carrying exactly one
    distinct applicant/account id. Zero rows, missing ids, or disagreement
    across rows -> (None, reason).
    """
    number = str(policy_number or "").strip()
    if not number:
        return None, "no policy number extracted — cannot resolve applicant without guessing"
    if policy_search is None:
        return None, "no policy-search client configured — applicant left unresolved"
    try:
        result = policy_search.search_policy_by_number(number)
    except Exception as exc:  # noqa: BLE001 - fail closed on any API error
        return None, f"policy search failed ({type(exc).__name__}): refusing to guess"
    rows = result.get("data") if isinstance(result, dict) else None
    if isinstance(rows, dict):
        # Paged responses wrap rows under "results".
        inner = rows.get("results")
        rows = inner if isinstance(inner, list) else [rows]
    if not rows:
        return None, f"policy {number} not found in EZLynx — applicant unresolved"
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in _APPLICANT_ID_KEYS:
            val = str(row.get(key) or "").strip()
            if val and val != "0":
                ids.add(val)
                break
    if len(ids) == 1:
        return next(iter(ids)), None
    if not ids:
        return None, f"policy {number} matched but rows carry no applicant id — unresolved"
    return None, f"policy {number} matched {len(ids)} different applicants — refusing to guess"


# ---------------------------------------------------------------------------
# Discussion naming (fail closed)
# ---------------------------------------------------------------------------

def name_discussion(
    discussion_lister: Any, applicant_id: str | None, notice_type: str
) -> tuple[str | None, str | None, str | None]:
    """Return (discussion_id, discussion_title, skip_reason). Never creates."""
    if not applicant_id:
        return None, None, "no applicant id — no discussion to name"
    if discussion_lister is None:
        return None, None, "no discussion client configured"
    try:
        discussions = discussion_lister.get_discussions(applicant_id)
    except Exception as exc:  # noqa: BLE001 - fail closed on any API error
        return None, None, f"discussion listing failed ({type(exc).__name__})"
    try:
        chosen = select_discussion_for_note(
            discussions, title_hint=_NOTICE_TYPE_LABEL.get(notice_type, "")
        )
    except DiscussionSelectionError as exc:
        return None, None, f"discussion ambiguous or missing ({exc})"
    return discussion_id_of(chosen), discussion_title_of(chosen), None


# ---------------------------------------------------------------------------
# Note drafting (plain English, nontechnical, no phone numbers)
# ---------------------------------------------------------------------------

def draft_note(fields: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Draft the one-line note. Returns (note, flags)."""
    label = _NOTICE_TYPE_LABEL.get(fields.get("notice_type") or "", "Policy activity")
    carrier = fields.get("carrier") or "Carrier"
    insured = fields.get("insured") or "insured not identified"
    policy = fields.get("policy_number") or "policy number not shown"
    date = fields.get("email_date") or "date not shown"
    flags: list[str] = []
    if fields.get("notice_type") == "cancellation":
        flags.append("followup_task")
    note = (
        f"{label} notice {date}: {carrier} — {insured} "
        f"(policy {policy}). CSR to review."
    )
    if "followup_task" in flags:
        note += " Flag: follow-up task per cancellation rule."
    try:
        reject_phone_numbers(note)
    except Exception:  # noqa: BLE001 - phone-like value; redact policy, keep pack building
        note = (
            f"{label} notice {date}: {carrier} — {insured}. "
            f"CSR to review (policy number withheld: looked phone-like)."
        )
        flags.append("policy_withheld_phone_guard")
    return note, flags


# ---------------------------------------------------------------------------
# Triage run
# ---------------------------------------------------------------------------

class HelloTriage:
    def __init__(
        self,
        gmail: Any,
        policy_search: Any = None,
        discussion_lister: Any = None,
    ) -> None:
        self._gmail = gmail
        self._policy_search = policy_search
        self._discussion_lister = discussion_lister

    def run(
        self,
        mailbox: str = MAILBOX_DEFAULT,
        days: int = 7,
        max_items: int = 25,
    ) -> dict[str, Any]:
        query = f"is:unread newer_than:{int(days)}d"
        metas = self._gmail.search_meta(query, max_results=max_items) or []
        items: list[dict[str, Any]] = []
        for meta in metas[:max_items]:
            items.append(self._triage_one(meta))
        counts: dict[str, int] = {}
        skip_reasons: dict[str, int] = {}
        unmatched: list[str] = []
        resolved = 0
        for it in items:
            counts[it["classification"]] = counts.get(it["classification"], 0) + 1
            if it.get("skip_reason"):
                skip_reasons[it["skip_reason"]] = skip_reasons.get(it["skip_reason"], 0) + 1
            if it["classification"] == CARRIER_NOTICE:
                if it.get("applicant_id"):
                    resolved += 1
                else:
                    unmatched.append(it["gmail_id"])
        notices = sum(1 for it in items if it["classification"] == CARRIER_NOTICE)
        return {
            "generated": datetime.now(timezone.utc).isoformat(),
            "mailbox": mailbox,
            "scope": query,
            "phase": "phase1-dryrun-readonly",
            "n": len(items),
            "items": items,
            "unmatched_gmail_ids": unmatched,
            "summary": {
                "classification_counts": counts,
                "carrier_notices": notices,
                "applicant_match_rate": (resolved / notices) if notices else 0.0,
                "skip_reasons": skip_reasons,
            },
        }

    def _triage_one(self, meta: dict[str, Any]) -> dict[str, Any]:
        gmail_id = str(meta.get("id") or "")
        category, reason = classify_message(meta)
        item: dict[str, Any] = {
            "gmail_id": gmail_id,
            "from": meta.get("from"),
            "subject": meta.get("subject"),
            "date": meta.get("date"),
            "snippet": meta.get("snippet"),
            "classification": category,
            "classification_reason": reason,
            "carrier": None,
            "insured": None,
            "policy_number": None,
            "notice_type": None,
            "applicant_id": None,
            "discussion_id": None,
            "discussion_title": None,
            "draft_note": None,
            "flags": [],
            "skip_reason": None,
        }
        if category != CARRIER_NOTICE:
            if category == NEEDS_REVIEW:
                item["skip_reason"] = f"needs human review: {reason}"
            return item
        body = None
        try:
            body = self._gmail.get_body(gmail_id)
        except Exception:  # noqa: BLE001 - body read is best-effort; subject still triaged
            body = None
        fields = extract_notice_fields(meta, body)
        item.update(
            carrier=fields["carrier"],
            insured=fields["insured"],
            policy_number=fields["policy_number"],
            notice_type=fields["notice_type"],
        )
        applicant_id, skip = resolve_applicant_id(self._policy_search, fields["policy_number"])
        if skip:
            item["skip_reason"] = skip
            return item
        item["applicant_id"] = applicant_id
        disc_id, disc_title, dskip = name_discussion(
            self._discussion_lister, applicant_id, fields["notice_type"]
        )
        if dskip:
            item["skip_reason"] = dskip
            return item
        item["discussion_id"] = disc_id
        item["discussion_title"] = disc_title
        note, flags = draft_note(fields)
        item["draft_note"] = note
        item["flags"] = flags
        return item


def main() -> int:
    parser = argparse.ArgumentParser(description="Hello@ Phase 1 read-only triage dry run")
    parser.add_argument("--mailbox", default=MAILBOX_DEFAULT)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--max", type=int, default=25, dest="max_items")
    parser.add_argument("--results-out", default="")
    parser.add_argument(
        "--gmail-shim",
        default="",
        help="Import path of a no-arg callable returning a gmail client (for wiring).",
    )
    args = parser.parse_args()

    gmail = None
    if args.gmail_shim:
        import importlib

        mod_name, _, attr = args.gmail_shim.rpartition(":")
        if not mod_name:
            raise SystemExit("--gmail-shim must look like package.module:factory")
        gmail = getattr(importlib.import_module(mod_name), attr)()

    if gmail is None:
        raise SystemExit(
            "no gmail client wired: pass --gmail-shim. "
            "Phase 1 never touches a mailbox without an explicit client."
        )

    pack = HelloTriage(gmail).run(
        mailbox=args.mailbox, days=args.days, max_items=args.max_items
    )
    text = json.dumps(pack, indent=1, default=str)
    if args.results_out:
        with open(args.results_out, "w") as fh:
            fh.write(text)
        print(f"wrote {args.results_out} ({pack['n']} items)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
