"""Match a robie@ email to exactly one EZLynx client, or say why not.

Order (Carlo 2026-10-08):

1. A policy number in the subject or body -> the client that carries it.
   The book index answers first; the EZLynx PolicyApi confirms or fills in.
2. A sender, To, or Cc address that is a client's primary or business
   email.
3. The same Gmail thread as an email already filed for one client.
4. Anything else, or more than one client -> Needs review. Nothing filed.

Skipped before matching: Applied Reporting reports, staff-only mail
(every address @streetsmart.insurance), out-of-office auto replies, and
Zapier alerts. A forward from Carlo is matched on the original sender.

The index is the full-book CSV on the host. It is client data and is
never committed; tests use synthetic rows.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr
from typing import Any, Callable, Iterable

from .cert_applicant_index import normalize_email, normalize_policy_number

STAFF_DOMAIN = "@streetsmart.insurance"
FORWARDER = "carlo@streetsmart.insurance"
SKIP_SENDERS = frozenset({"donotreply@appliedsystems.com"})
SKIP_SENDER_DOMAINS = ("mail.zapier.com",)
AUTO_REPLY_SUBJECT = re.compile(r"^\s*(?:automatic reply|auto(?:matic)?[- ]?reply|out of (?:the )?office)\b", re.I)

MATCHED = "MATCHED"
SKIPPED = "SKIPPED"
REVIEW = "NEEDS_REVIEW"

_POLICY_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{1,5}[ -]?\d{5,12}(?:[ -]\d{1,4})?|\d{2,4}[ -]?[A-Z]{1,4}[ -]?\d{4,10}|\d{7,12})(?![A-Za-z0-9])"
)
_PHONE = re.compile(r"^\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}$")
_STATE_ZIP = re.compile(r"^[A-Z]{2}[ -]\d{5}(?:-\d{4})?$")
_FORWARDED_FROM = re.compile(r"^\s*>?\s*From:\s*(.+)$", re.I | re.M)
MAX_POLICY_CANDIDATES = 6


@dataclass
class BookIndex:
    by_policy: dict[str, set[str]] = field(default_factory=dict)
    by_email: dict[str, set[str]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def name(self, applicant_id: str) -> str:
        return self.names.get(str(applicant_id), "")


def load_book_index(path: str) -> BookIndex:
    """Read the full-book CSV. Several clients sharing an email stay ambiguous."""

    index = BookIndex()
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            applicant = str(row.get("applicant_id") or "").strip()
            if not applicant.isdigit():
                continue
            index.names[applicant] = str(row.get("account_name") or "").strip()
            for column in ("email_primary", "email_business"):
                for raw in re.split(r"[;,\s]+", str(row.get(column) or "")):
                    email = normalize_email(raw)
                    if "@" in email and not email.endswith(STAFF_DOMAIN):
                        index.by_email.setdefault(email, set()).add(applicant)
            for raw in str(row.get("policy_numbers") or "").split(";"):
                key = _policy_key(raw)
                if key:
                    index.by_policy.setdefault(key, set()).add(applicant)
    return index


def _policy_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_policy_number(value))


@dataclass
class MatchDecision:
    outcome: str
    reason: str = ""
    applicant_id: str = ""
    account_name: str = ""
    how: str = ""
    policy_number: str = ""
    policy_master_id: str = ""
    candidates: list[str] = field(default_factory=list)


def _addresses(message: dict[str, Any], header: Callable[[dict[str, Any], str], str]) -> list[str]:
    values = [header(message, name) for name in ("From", "To", "Cc")]
    return [normalize_email(addr) for _, addr in getaddresses(values) if addr]


def policy_candidates(text: str) -> list[str]:
    found: list[str] = []
    for raw in _POLICY_CANDIDATE.findall(text or ""):
        value = raw.strip()
        if _PHONE.match(value) or _STATE_ZIP.match(value) or value in found:
            continue
        found.append(value)
        if len(found) >= MAX_POLICY_CANDIDATES:
            break
    return found


def skip_reason(message: dict[str, Any], header: Callable[[dict[str, Any], str], str], body: str) -> tuple[str, list[str]]:
    """('' , external addresses) to match, or (reason, []) to skip."""

    sender = normalize_email(parseaddr(header(message, "From"))[1])
    if sender in SKIP_SENDERS:
        return "Applied Reporting report", []
    if sender.endswith(SKIP_SENDER_DOMAINS):
        return "automated alert", []
    if AUTO_REPLY_SUBJECT.search(header(message, "Subject")) or header(message, "Auto-Submitted").lower().startswith("auto-"):
        return "auto reply", []
    external = [a for a in _addresses(message, header) if not a.endswith(STAFF_DOMAIN)]
    if external:
        return "", external
    if sender == FORWARDER:
        originals = []
        for line in _FORWARDED_FROM.findall(body or ""):
            addr = normalize_email(parseaddr(line)[1])
            if "@" in addr and not addr.endswith(STAFF_DOMAIN) and not addr.endswith(SKIP_SENDER_DOMAINS):
                originals.append(addr)
        if originals:
            return "", originals
    return "staff-only", []


PolicyLookup = Callable[[str], list[tuple[str, str]]]


def match_message(
    message: dict[str, Any],
    *,
    header: Callable[[dict[str, Any], str], str],
    body: str,
    index: BookIndex,
    policy_lookup: PolicyLookup | None,
    thread_applicants: Iterable[str] = (),
) -> MatchDecision:
    reason, external = skip_reason(message, header, body)
    if reason:
        return MatchDecision(SKIPPED, reason)

    text = header(message, "Subject") + "\n" + (body or "")
    candidates = policy_candidates(text)
    by_policy: dict[str, tuple[str, str]] = {}
    for candidate in candidates:
        key = _policy_key(candidate)
        hits = {aid: "" for aid in index.by_policy.get(key, set())}
        if policy_lookup is not None and (len(hits) <= 1):
            for aid, master in policy_lookup(candidate):
                if aid and (not hits or aid in hits):
                    hits[aid] = master or hits.get(aid, "")
        for aid, master in hits.items():
            by_policy.setdefault(aid, (candidate, master))
    if len(by_policy) == 1:
        aid, (number, master) = next(iter(by_policy.items()))
        return MatchDecision(MATCHED, applicant_id=aid, account_name=index.name(aid), how="policy #",
                             policy_number=number, policy_master_id=master)
    if len(by_policy) > 1:
        return MatchDecision(REVIEW, f"policy numbers point to {len(by_policy)} clients",
                             candidates=sorted(by_policy))

    by_email: dict[str, str] = {}
    for address in external:
        for aid in index.by_email.get(address, set()):
            by_email.setdefault(aid, address)
    if len(by_email) == 1:
        aid, address = next(iter(by_email.items()))
        return MatchDecision(MATCHED, applicant_id=aid, account_name=index.name(aid), how=f"contact {address}")
    if len(by_email) > 1:
        return MatchDecision(REVIEW, f"email addresses match {len(by_email)} clients", candidates=sorted(by_email))

    threaded = sorted(set(thread_applicants))
    if len(threaded) == 1:
        return MatchDecision(MATCHED, applicant_id=threaded[0], account_name=index.name(threaded[0]), how="same thread")
    if len(threaded) > 1:
        return MatchDecision(REVIEW, f"thread already filed to {len(threaded)} clients", candidates=threaded)
    return MatchDecision(REVIEW, "no policy number, client email, or filed thread", candidates=[])
