"""Prior-term ↔ renewal-term policy number aliases.

Carriers sometimes issue a new policy number for the renewal term
(e.g. Safe Man: R2WC681352 in renewals.db vs R2WC771037 on the portal/offer).
Both numbers belong to the same account / outreach thread.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from src.database.models import PolicyNumberAlias, PolicyRenewal

logger = logging.getLogger("policy_aliases")

# Token that looks like a carrier policy number, excluding [RENEWAL-REQ-###].
_POLICY_TOKEN_RE = re.compile(r"\b[A-Z0-9][A-Z0-9._/-]{5,40}\b", re.IGNORECASE)
_SKIP_TOKEN_RE = re.compile(
    r"^(renewal-req-\d+|https?|www|message-id|gmail_)",
    re.IGNORECASE,
)


def normalize_policy_number(value: Optional[str]) -> str:
    """Alphanumeric fold used for equality (dashes/spaces ignored)."""
    if not value:
        return ""
    return re.sub(r"[^a-zA-Z0-9]", "", str(value)).lower()


def numbers_equivalent(left: Optional[str], right: Optional[str]) -> bool:
    a, b = normalize_policy_number(left), normalize_policy_number(right)
    return bool(a) and a == b


def collect_policy_numbers(policy: Optional[PolicyRenewal]) -> List[str]:
    """Canonical policy_number plus every stored alias, de-duplicated."""
    if policy is None:
        return []
    seen = set()
    out: List[str] = []
    for raw in [policy.policy_number] + [
        a.alias_number for a in (getattr(policy, "number_aliases", None) or [])
    ]:
        key = normalize_policy_number(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(str(raw).strip())
    return out


def policy_match_payload(policy: PolicyRenewal) -> Dict[str, Any]:
    """Dict consumed by GmailRenewalClient._matches_any_policy (includes aliases)."""
    return {
        "id": policy.id,
        "policy_number": policy.policy_number,
        "policy_numbers": collect_policy_numbers(policy),
        "insured_name": policy.insured_name,
    }


def find_policy_by_any_number(db: Session, raw_number: Optional[str]) -> Optional[PolicyRenewal]:
    """Resolve a prior-term or renewal-term number to the PolicyRenewal row."""
    needle = normalize_policy_number(raw_number)
    if not needle or not raw_number:
        return None

    exact = (
        db.query(PolicyRenewal)
        .filter(PolicyRenewal.policy_number == raw_number.strip())
        .first()
    )
    if exact:
        return exact

    alias = (
        db.query(PolicyNumberAlias)
        .filter(PolicyNumberAlias.alias_number == raw_number.strip())
        .first()
    )
    if alias and alias.policy:
        return alias.policy

    # Folded compare for dashed / spaced variants
    for pol in db.query(PolicyRenewal).all():
        if any(normalize_policy_number(n) == needle for n in collect_policy_numbers(pol)):
            return pol
    return None


def register_policy_alias(
    db: Session,
    policy: PolicyRenewal,
    alias_number: Optional[str],
    alias_kind: str = "renewal_term",
) -> Optional[PolicyNumberAlias]:
    """Store prior→current (or current→prior) alias. No-op if same as canonical or empty."""
    if policy is None or not alias_number:
        return None
    cleaned = str(alias_number).strip()
    if not cleaned or numbers_equivalent(cleaned, policy.policy_number):
        return None

    existing_for_number = find_policy_by_any_number(db, cleaned)
    if existing_for_number and existing_for_number.id != policy.id:
        logger.info(
            f"Not aliasing {cleaned} onto policy {policy.id}: already bound to policy {existing_for_number.id}"
        )
        return None

    for alias in policy.number_aliases or []:
        if numbers_equivalent(alias.alias_number, cleaned):
            return alias

    row = PolicyNumberAlias(
        policy_id=policy.id,
        alias_number=cleaned,
        alias_kind=alias_kind or "renewal_term",
    )
    db.add(row)
    db.flush()
    logger.info(
        f"Aliased {cleaned} ({alias_kind}) → Pol #{policy.policy_number} (id={policy.id})"
    )
    return row


def harvest_policy_numbers(*texts: Optional[str]) -> List[str]:
    """Pull policy-number-like tokens from email/PDF text (skips RENEWAL-REQ tags)."""
    found: List[str] = []
    seen = set()
    for text in texts:
        if not text:
            continue
        for token in _POLICY_TOKEN_RE.findall(text):
            if _SKIP_TOKEN_RE.match(token):
                continue
            key = normalize_policy_number(token)
            if len(key) < 6 or key in seen:
                continue
            if not re.search(r"[A-Za-z]", key) or not re.search(r"\d", key):
                continue
            seen.add(key)
            found.append(token.strip())
    return found


def register_aliases_from_texts(
    db: Session,
    policy: PolicyRenewal,
    texts: Sequence[Optional[str]],
    alias_kind: str = "renewal_term",
) -> List[str]:
    """If a message/PDF cites a different policy number, bind it as an alias."""
    added: List[str] = []
    known = {normalize_policy_number(n) for n in collect_policy_numbers(policy)}
    for token in harvest_policy_numbers(*texts):
        if normalize_policy_number(token) in known:
            continue
        row = register_policy_alias(db, policy, token, alias_kind=alias_kind)
        if row:
            added.append(row.alias_number)
            known.add(normalize_policy_number(row.alias_number))
    return added


def association_policy_number(
    policy: PolicyRenewal,
    discussion: Optional[Dict[str, Any]] = None,
) -> str:
    """Number to send to EZLynx document/policy association (prefer card / renewal-term)."""
    if discussion:
        note = discussion.get("discussionNote")
        if isinstance(note, dict) and note.get("policyNumber"):
            return str(note["policyNumber"])
        title = discussion.get("title") or ""
        for n in collect_policy_numbers(policy):
            if normalize_policy_number(n) and normalize_policy_number(n) in normalize_policy_number(title):
                return n
    for alias in getattr(policy, "number_aliases", None) or []:
        if alias.alias_kind in ("renewal_term", "portal"):
            return alias.alias_number
    return policy.policy_number


def sibling_policy_for_intake(
    db: Session,
    *,
    applicant_id: Optional[str],
    expiration_date,
    line_of_business: Optional[str],
    policy_number: Optional[str],
) -> Optional[PolicyRenewal]:
    """Same applicant + expiration + LOB with a different number = term flip, not a new row."""
    if not applicant_id or not expiration_date or not policy_number:
        return None
    q = db.query(PolicyRenewal).filter(
        PolicyRenewal.applicant_id == str(applicant_id),
        PolicyRenewal.expiration_date == expiration_date,
    )
    if line_of_business:
        q = q.filter(PolicyRenewal.line_of_business == line_of_business)
    for pol in q.all():
        if numbers_equivalent(pol.policy_number, policy_number):
            continue
        return pol
    return None
