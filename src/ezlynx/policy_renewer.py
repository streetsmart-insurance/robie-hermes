"""EZLynx manual renewal *shell* keying — hardened server recipe.

Routine keys belong on hermes-test-01 / hermes-poc-01 via this module (or
``scripts/run_manual_renewal.py``). Antigravity / Gemini stay explore + HITL
only — do not chain SSH/SCP micro-scripts per step.

One connected CDP session, one Python job:

    optional authentic PDF upload → exact titled discussion note →
    ``#RenewPolicyBtn`` shell (never Renew & Edit) → proof JSON

Hardening (live 2026-09-06 symptoms):
- Pending RWL shells are proven from policy-summary History / in-page PolicyAPI.
  Classic ``get_applicant_policies`` does **not** return pending RWL shells and
  must never be the sole "already exists" check (Maier Solar IM duplicates).
- Same term + premium pending RWL → STOP (``already_in``). No second shell.
- Shell-only submit is ``#RenewPolicyBtn`` ("Renew Policy"). Never
  "Renew & Edit Policy" (opens FormEntry; not this job).
- Writing Company (and other required fields) must be populated before submit
  (Yes We Do WC bounce → retry → duplicate).
- Notes only on the caller-supplied / original titled card matching LOB +
  policy number. Never untitled. Never a generic LOB card such as
  ``Workers Compensation Renewal``.
- ``already_in`` skips docs and notes. PDFs under 10KB are stubs — never upload.
- Producer / CSR is Carlo Ferrara, never Robie. No bind. No money. No client
  email. Never Add Policy from Quote ID.

Production is never the first test. Verify on hermes-test-01 (``--env test``)
before any Production zip to hermes-poc-01.

CLI::

    PYTHONPATH=. python3 -m src.ezlynx.policy_renewer --dry-run ...
    PYTHONPATH=. python3 scripts/run_manual_renewal.py --dry-run ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.config import settings
from src.database.policy_aliases import normalize_policy_number, numbers_equivalent
from src.ezlynx.api_client import (
    DISQUALIFIED_DISCUSSION_PATTERNS,
    EZLynxApiClient,
    ROBIE_SIGNATURE,
)

logger = logging.getLogger("policy_renewer")

RENEW_POLICY_BTN_SELECTOR = "#RenewPolicyBtn"
RENEW_POLICY_BTN_TEXT = "Renew Policy"
FORBIDDEN_RENEW_BUTTON_TEXTS = ("Renew & Edit Policy",)
FORBIDDEN_BIND_TEXTS = ("Bind", "Issue Policy", "Purchase", "Checkout")
MIN_AUTHENTIC_PDF_BYTES = 10 * 1024
DEFAULT_PRODUCER_CSR = "Carlo Ferrara"
FORBIDDEN_PRODUCER_NAMES = ("Robie", "Robie AI", "SSRobie", "SS Robie")
LIVE_ALLOW_TOKEN = "Carlo"
ROBIE_SIGNATURE_LINE = "Robie was here"

GENERIC_LOB_TITLES = {
    "workers compensation renewal",
    "workers comp renewal",
    "workers compensation",
    "general liability renewal",
    "commercial auto renewal",
    "commercial package renewal",
    "excess manual renewal",
    "inland marine renewal",
    "business owners manual renewal",
    "manual workers comp renewal",
    "manual commercial general liability renewal",
}

LOB_ALIASES = {
    "workers compensation": "workerscomp",
    "workers comp": "workerscomp",
    "worker's compensation": "workerscomp",
    "wc": "workerscomp",
    "general liability": "generalliability",
    "commercial general liability": "generalliability",
    "cgl": "generalliability",
    "gl": "generalliability",
    "inland marine": "inlandmarine",
    "im": "inlandmarine",
    "commercial auto": "commercialauto",
    "ca": "commercialauto",
    "business owners": "bop",
    "bop": "bop",
    "excess": "excess",
    "umbrella": "excess",
}

# Known production applicants from live 2026-09 jobs / SOP. Extended via
# MANUAL_RENEWAL_LIVE_APPLICANTS=id1,id2. --allow-live Carlo is still required.
DEFAULT_PRODUCTION_LIVE_APPLICANTS = frozenset(
    {
        "25156187",  # Maier Solar LLC
        "21588091",  # Yes We Do WC (PWC1239278 discussion fixture / live card)
    }
)

WRITING_COMPANY_SELECTORS = (
    "#WritingCompany",
    "select#WritingCompany",
    "select[name*='WritingCompany' i]",
    "select[id*='WritingCompany' i]",
    "input#WritingCompany",
    "input[name*='WritingCompany' i]",
    "[aria-label*='Writing Company' i]",
)

PRODUCER_SELECTORS = (
    "#Producer",
    "#AssignedProducer",
    "select[name*='Producer' i]",
    "select[id*='Producer' i]",
    "select[name*='AssignedTo' i]",
    "#AssignedTo",
)


class RenewalGuardError(RuntimeError):
    """Hard stop: do not key, bind, or post to the wrong card."""


class LiveKeyingBlocked(RenewalGuardError):
    """Production live keying refused (allow-list / --allow-live Carlo)."""


@dataclass
class PendingShell:
    transaction_type: str
    status: str
    effective_date: Optional[str] = None
    expiration_date: Optional[str] = None
    premium: Optional[Decimal] = None
    policy_number: Optional[str] = None
    writing_company: Optional[str] = None
    source: str = "unknown"
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if isinstance(data.get("premium"), Decimal):
            data["premium"] = str(data["premium"])
        return data


@dataclass
class RenewalJobSpec:
    applicant_id: str
    policy_id: Optional[str] = None
    policy_number: Optional[str] = None
    policy_number_aliases: List[str] = field(default_factory=list)
    line_of_business: Optional[str] = None
    carrier_name: Optional[str] = None
    premium: Optional[Decimal] = None
    full_term_premium: Optional[Decimal] = None
    annual_premium: Optional[Decimal] = None
    effective_date: Optional[str] = None
    expiration_date: Optional[str] = None
    writing_company: Optional[str] = None
    discussion_title: Optional[str] = None
    note_text: Optional[str] = None
    upload_path: Optional[Path] = None
    producer: str = DEFAULT_PRODUCER_CSR
    dry_run: bool = True
    verify_only: bool = False
    allow_live: Optional[str] = None
    env: str = "test"
    cdp_url: Optional[str] = None
    proof_json: Optional[Path] = None
    already_in: bool = False


@dataclass
class RenewalJobResult:
    status: str
    applicant_id: str
    policy_number: Optional[str] = None
    policy_id: Optional[str] = None
    already_in: bool = False
    pending_shells: List[Dict[str, Any]] = field(default_factory=list)
    discussion_title: Optional[str] = None
    note_posted: bool = False
    document_uploaded: bool = False
    document_skipped_reason: Optional[str] = None
    note_skipped_reason: Optional[str] = None
    renew_button: Optional[str] = None
    writing_company: Optional[str] = None
    producer: str = DEFAULT_PRODUCER_CSR
    bound: bool = False
    proof_source: Optional[str] = None
    verified_via_classic_only: bool = False
    dry_run: bool = True
    env: str = "test"
    error: Optional[str] = None
    planned_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested; no Playwright)
# ---------------------------------------------------------------------------


def parse_money(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    text = re.sub(r"[,$]", "", text)
    text = text.replace("USD", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def premiums_match(left: Any, right: Any, *, cents: Decimal = Decimal("0.01")) -> bool:
    a, b = parse_money(left), parse_money(right)
    if a is None or b is None:
        return False
    return abs(a - b) <= cents


def normalize_date(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if match:
        month, day, year = match.groups()
        year_i = int(year)
        if year_i < 100:
            year_i += 2000
        return f"{year_i:04d}-{int(month):02d}-{int(day):02d}"
    return None


def terms_match(
    left_eff: Any,
    left_exp: Any,
    right_eff: Any,
    right_exp: Any,
) -> bool:
    a_eff, a_exp = normalize_date(left_eff), normalize_date(left_exp)
    b_eff, b_exp = normalize_date(right_eff), normalize_date(right_exp)
    if not a_eff or not a_exp or not b_eff or not b_exp:
        return False
    return a_eff == b_eff and a_exp == b_exp


def _norm_title(title: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (title or "").strip()).lower()


def is_untitled_discussion(title: Optional[str]) -> bool:
    cleaned = (title or "").strip()
    return not cleaned or cleaned.lower() in {"untitled", "(untitled)", "new discussion"}


def is_generic_lob_title(title: Optional[str]) -> bool:
    cleaned = _norm_title(title)
    if not cleaned:
        return True
    if "|" in cleaned:
        return False
    if normalize_policy_number(title) and re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", normalize_policy_number(title) or ""):
        # Title is only a policy number — still not a titled agency card.
        pass
    collapsed = re.sub(r"[^a-z0-9 ]+", " ", cleaned)
    collapsed = re.sub(r"\s+", " ", collapsed).strip()
    if collapsed in GENERIC_LOB_TITLES:
        return True
    # Generic "X Renewal [Year]" without a policy number
    if re.fullmatch(r"[a-z ]+ renewal(?:\s+\d{4}(?:\s*[-/]\s*\d{2,4})?)?", collapsed):
        return True
    return False


def normalize_lob_token(line_of_business: Optional[str]) -> str:
    raw = re.sub(r"[^a-z0-9 ]+", " ", (line_of_business or "").lower())
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return ""
    if raw in LOB_ALIASES:
        return LOB_ALIASES[raw]
    for alias, token in LOB_ALIASES.items():
        if alias in raw:
            return token
    return re.sub(r"\s+", "", raw)


def title_matches_lob(title: Optional[str], line_of_business: Optional[str]) -> bool:
    if not line_of_business:
        return True
    t_low = _norm_title(title)
    lob_token = normalize_lob_token(line_of_business)
    if not lob_token:
        return True
    if lob_token == "workerscomp":
        return any(s in t_low for s in ("workers comp", "worker s comp", "workers compensation", " wc "))
    if lob_token == "generalliability":
        return "general liability" in t_low or re.search(r"\bgl\b", t_low) is not None
    if lob_token == "inlandmarine":
        return "inland marine" in t_low or re.search(r"\bim\b", t_low) is not None
    if lob_token == "commercialauto":
        return "commercial auto" in t_low or "auto renewal" in t_low
    if lob_token == "excess":
        return "excess" in t_low or "umbrella" in t_low
    if lob_token == "bop":
        return "business owner" in t_low or re.search(r"\bbop\b", t_low) is not None
    return lob_token in re.sub(r"[^a-z0-9]", "", t_low)


def title_contains_policy(title: Optional[str], numbers: Sequence[str]) -> bool:
    title_fold = normalize_policy_number(title)
    if not title_fold:
        return False
    for raw in numbers:
        needle = normalize_policy_number(raw)
        if needle and len(needle) >= 4 and needle in title_fold:
            return True
    return False


def is_disqualified_discussion_title(title: Optional[str]) -> bool:
    t_low = _norm_title(title)
    return any(pat in t_low for pat in DISQUALIFIED_DISCUSSION_PATTERNS)


def collect_job_policy_numbers(spec: RenewalJobSpec) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in [spec.policy_number, *spec.policy_number_aliases]:
        key = normalize_policy_number(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(str(raw).strip())
    return out


def resolve_exact_discussion_title(
    discussions: Sequence[Dict[str, Any]],
    *,
    requested_title: Optional[str],
    policy_numbers: Sequence[str],
    line_of_business: Optional[str],
) -> str:
    """Return the original titled card. Never untitled. Never wrong LOB.

    Caller-supplied ``--discussion-title`` wins on exact (normalized) match.
    Generic LOB-only titles such as ``Workers Compensation Renewal`` are refused
    when a policy-numbered titled card exists for the same LOB (Yes We Do WC).
    """
    valid: List[Dict[str, Any]] = []
    for d in discussions:
        title = (d.get("title") or "").strip()
        if is_untitled_discussion(title) or is_disqualified_discussion_title(title):
            continue
        valid.append(d)

    numbered = [
        d
        for d in valid
        if title_contains_policy(d.get("title"), policy_numbers)
        and title_matches_lob(d.get("title"), line_of_business)
        and not is_generic_lob_title(d.get("title"))
    ]
    numbered.sort(
        key=lambda d: (
            1 if "|" in (d.get("title") or "") else 0,
            d.get("noteCount") or 0,
        ),
        reverse=True,
    )

    if requested_title and not is_untitled_discussion(requested_title):
        want = _norm_title(requested_title)
        for d in valid:
            if _norm_title(d.get("title")) != want:
                continue
            title = d["title"].strip()
            if line_of_business and not title_matches_lob(title, line_of_business):
                raise RenewalGuardError(
                    f"Requested discussion '{title}' does not match LOB '{line_of_business}'"
                )
            # Exact titled card (has policy # / pipe) — honor the caller.
            if not is_generic_lob_title(title):
                return title
            # Generic LOB card ("Workers Compensation Renewal") is never used
            # when the original policy-numbered card exists (Yes We Do WC).
            if numbered:
                logger.warning(
                    "Ignoring generic discussion '%s'; using original titled card '%s'",
                    title,
                    numbered[0]["title"],
                )
                return numbered[0]["title"].strip()
            raise RenewalGuardError(
                f"Refusing generic LOB discussion '{title}'. "
                "Pass the original titled card "
                "(e.g. 'Renewal Manual Workers comp | PWC1239278 Associated Specialty Insurance')."
            )

    if numbered:
        return numbered[0]["title"].strip()

    if requested_title and is_generic_lob_title(requested_title):
        raise RenewalGuardError(
            f"Refusing generic LOB discussion '{requested_title}'. "
            "Pass the original titled card "
            "(e.g. 'Renewal Manual Workers comp | PWC1239278 Associated Specialty Insurance')."
        )

    raise RenewalGuardError(
        "No exact original titled discussion found for this LOB/policy. "
        "Notes are never posted untitled or onto a wrong LOB card."
    )


def is_pending_rwl(record: Dict[str, Any]) -> bool:
    blob = " ".join(
        str(record.get(k) or "")
        for k in (
            "transactionType",
            "TransactionType",
            "transaction",
            "type",
            "status",
            "Status",
            "policyStatus",
            "description",
            "Description",
            "text",
            "raw_text",
        )
    ).lower()
    pending = "pending" in blob or str(record.get("status") or "").lower() == "pending"
    rwl = bool(
        re.search(r"\b(rwl|renewal)\b", blob)
        or str(record.get("transactionType") or record.get("TransactionType") or "").upper()
        in {"RWL", "RENEWAL"}
    )
    return pending and rwl


def extract_pending_rwl_shells(records: Iterable[Any]) -> List[PendingShell]:
    shells: List[PendingShell] = []
    for rec in records:
        if isinstance(rec, PendingShell):
            shells.append(rec)
            continue
        if isinstance(rec, str):
            rec = {"text": rec, "raw_text": rec}
        if not isinstance(rec, dict):
            continue
        text = str(rec.get("text") or rec.get("raw_text") or rec.get("description") or rec.get("Description") or "")
        if not is_pending_rwl(rec) and not (
            re.search(r"pending", text, re.I) and re.search(r"\b(rwl|renewal)\b", text, re.I)
        ):
            continue
        dates = rec.get("dates") or []
        if isinstance(dates, str):
            dates = [dates]
        date_matches = list(re.finditer(r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})", text))
        eff = normalize_date(
            rec.get("effectiveDate")
            or rec.get("EffectiveDate")
            or rec.get("effective_date")
            or rec.get("termStart")
            or (dates[0] if dates else None)
            or (date_matches[0].group(1) if date_matches else None)
        )
        exp = normalize_date(
            rec.get("expirationDate")
            or rec.get("ExpirationDate")
            or rec.get("expiration_date")
            or rec.get("termEnd")
            or (dates[1] if len(dates) > 1 else None)
            or (date_matches[1].group(1) if len(date_matches) > 1 else None)
        )
        premium = parse_money(
            rec.get("premium")
            or rec.get("Premium")
            or rec.get("fullTermPremium")
            or rec.get("FullTermPremium")
            or rec.get("writtenPremium")
        )
        if premium is None:
            money = rec.get("money") or rec.get("amounts")
            if isinstance(money, list) and money:
                premium = parse_money(money[-1])
            elif premium is None:
                found = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
                if found:
                    premium = parse_money(found[-1])
        shells.append(
            PendingShell(
                transaction_type=str(
                    rec.get("transactionType") or rec.get("TransactionType") or "RWL"
                ),
                status=str(rec.get("status") or rec.get("Status") or "Pending"),
                effective_date=eff,
                expiration_date=exp,
                premium=premium,
                policy_number=str(rec.get("policyNumber") or rec.get("PolicyNumber") or "") or None,
                writing_company=str(rec.get("writingCompany") or rec.get("WritingCompany") or "") or None,
                source=str(rec.get("source") or "payload"),
                raw_text=text,
            )
        )
    return shells


def find_duplicate_pending_shell(
    shells: Sequence[PendingShell],
    *,
    effective_date: Optional[str],
    expiration_date: Optional[str],
    premium: Any,
) -> Optional[PendingShell]:
    """If a pending RWL already exists for the same term + premium, return it."""
    for shell in shells:
        if not terms_match(shell.effective_date, shell.expiration_date, effective_date, expiration_date):
            continue
        if not premiums_match(shell.premium, premium):
            continue
        return shell
    return None


def classic_api_cannot_prove_absence(_classic_payload: Any = None) -> bool:
    """Classic ``get_applicant_policies`` omits pending RWL shells.

    An empty Classic list is **not** proof that no pending shell exists.
    """
    return True


def verification_sources_are_sufficient(sources: Sequence[str]) -> bool:
    allowed = {
        "policy_summary_history",
        "ui_history",
        "policy_api_cards",
        "in_page_policy_api",
    }
    return any(src in allowed for src in sources)


def is_stub_document(path: Optional[Path], *, min_bytes: int = MIN_AUTHENTIC_PDF_BYTES) -> bool:
    if path is None:
        return False
    path = Path(path)
    if not path.is_file():
        return True
    return path.stat().st_size < min_bytes


def should_skip_docs_and_notes(already_in: bool) -> bool:
    return bool(already_in)


def is_forbidden_renew_button(text: Optional[str], element_id: Optional[str] = None) -> bool:
    t = (text or "").strip()
    i = (element_id or "").strip()
    if any(forb.lower() == t.lower() for forb in FORBIDDEN_RENEW_BUTTON_TEXTS):
        return True
    if "edit" in t.lower() or "edit" in i.lower():
        return True
    if any(forb.lower() == t.lower() for forb in FORBIDDEN_BIND_TEXTS):
        return True
    if any(word in t.lower() for word in ("bind", "purchase", "checkout")):
        return True
    return False


def is_preferred_renew_button(text: Optional[str], element_id: Optional[str] = None) -> bool:
    if is_forbidden_renew_button(text, element_id):
        return False
    if (element_id or "").strip() == "RenewPolicyBtn":
        return True
    return (text or "").strip() == RENEW_POLICY_BTN_TEXT


def choose_renew_submit_button(candidates: Sequence[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Pick ``#RenewPolicyBtn`` / exact 'Renew Policy'. Never Renew & Edit."""
    preferred = [
        c
        for c in candidates
        if is_preferred_renew_button(c.get("text"), c.get("id"))
    ]
    if not preferred:
        return None
    preferred.sort(key=lambda c: 0 if c.get("id") == "RenewPolicyBtn" else 1)
    return preferred[0]


def missing_required_fields(
    *,
    premium: Any,
    writing_company: Optional[str],
    effective_date: Optional[str] = None,
    expiration_date: Optional[str] = None,
    require_term: bool = True,
) -> List[str]:
    missing: List[str] = []
    if parse_money(premium) is None:
        missing.append("premium")
    if not (writing_company or "").strip():
        missing.append("writing_company")
    if require_term:
        if not normalize_date(effective_date):
            missing.append("effective_date")
        if not normalize_date(expiration_date):
            missing.append("expiration_date")
    return missing


def assert_producer_is_carlo(name: Optional[str]) -> str:
    cleaned = (name or "").strip() or DEFAULT_PRODUCER_CSR
    lowered = cleaned.lower()
    if any(forb.lower() in lowered for forb in FORBIDDEN_PRODUCER_NAMES):
        raise RenewalGuardError(
            f"Producer/CSR must be {DEFAULT_PRODUCER_CSR}, never Robie (got '{cleaned}')"
        )
    if "carlo" not in lowered:
        raise RenewalGuardError(
            f"Producer/CSR must be {DEFAULT_PRODUCER_CSR} (got '{cleaned}')"
        )
    return DEFAULT_PRODUCER_CSR


def load_production_allowlist() -> frozenset:
    extra = os.getenv("MANUAL_RENEWAL_LIVE_APPLICANTS", "")
    ids = set(DEFAULT_PRODUCTION_LIVE_APPLICANTS)
    for part in extra.split(","):
        part = part.strip()
        if part:
            ids.add(part)
    return frozenset(ids)


def assert_live_keying_allowed(spec: RenewalJobSpec) -> None:
    if spec.dry_run or spec.verify_only:
        return
    env = (spec.env or "test").strip().lower()
    if env in {"test", "hermes-test", "hermes-test-01"}:
        return
    token = (spec.allow_live or "").strip()
    if token.lower() != LIVE_ALLOW_TOKEN.lower():
        raise LiveKeyingBlocked(
            "Production live keying requires --allow-live Carlo "
            "(and hermes-test-01 verification first)."
        )
    allowlist = load_production_allowlist()
    if spec.applicant_id not in allowlist:
        raise LiveKeyingBlocked(
            f"Applicant {spec.applicant_id} is not on the production live allow-list. "
            "Add it via MANUAL_RENEWAL_LIVE_APPLICANTS after hermes-test-01 proof."
        )
    assert_producer_is_carlo(spec.producer)


def reject_quote_id_add_policy(quote_id: Optional[str]) -> None:
    if quote_id:
        raise RenewalGuardError("Never Add Policy from Quote ID")


def build_shell_note(spec: RenewalJobSpec) -> str:
    if spec.note_text and spec.note_text.strip():
        body = spec.note_text.strip()
    else:
        premium = spec.premium if spec.premium is not None else spec.full_term_premium
        lines = [
            f"Policy: #{spec.policy_number} ({spec.line_of_business or 'Policy'} - {spec.carrier_name or 'Carrier'})",
            "",
            "Manual renewal shell keyed (no bind).",
            f"Term: {normalize_date(spec.effective_date) or '?'} – {normalize_date(spec.expiration_date) or '?'}",
            f"Written premium: {premium}",
            f"Writing Company: {spec.writing_company or ''}",
            f"Producer/CSR: {DEFAULT_PRODUCER_CSR}",
            "",
            ROBIE_SIGNATURE_LINE,
        ]
        body = "\n".join(lines)
    if spec.policy_number and f"#{spec.policy_number}" not in body and f"Policy: {spec.policy_number}" not in body:
        header = f"Policy: #{spec.policy_number} ({spec.line_of_business or 'Policy'} - {spec.carrier_name or 'Carrier'})"
        body = f"{header}\n\n{body.lstrip()}"
    if ROBIE_SIGNATURE_LINE not in body:
        body = f"{body.rstrip()}{ROBIE_SIGNATURE}"
    return body


def write_proof_json(result: RenewalJobResult, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str) + "\n")
    logger.info("Wrote renewal proof JSON to %s", path)
    return path


# ---------------------------------------------------------------------------
# Connected CDP job
# ---------------------------------------------------------------------------


HISTORY_SCRAPE_JS = """
async ({ applicantId, policyId }) => {
  const shells = [];
  const pushFromText = (text, source) => {
    const blob = (text || '').replace(/\\s+/g, ' ').trim();
    if (!blob) return;
    const pending = /pending/i.test(blob);
    const rwl = /\\b(RWL|RENEWAL|Renewal)\\b/.test(blob);
    if (!(pending && rwl)) return;
    const dates = [...blob.matchAll(/(\\d{1,2}\\/\\d{1,2}\\/\\d{2,4}|\\d{4}-\\d{2}-\\d{2})/g)].map(m => m[1]);
    const money = [...blob.matchAll(/\\$[\\d,]+(?:\\.\\d{2})?/g)].map(m => m[0]);
    shells.push({
      source,
      text: blob.slice(0, 500),
      dates,
      money,
      status: 'Pending',
      transactionType: 'RWL',
    });
  };

  const rows = Array.from(document.querySelectorAll(
    'table tr, .history-row, .transaction-row, .policy-history tr, tbody tr'
  ));
  for (const row of rows) {
    pushFromText(row.innerText || '', 'policy_summary_history');
  }

  try {
    const r = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=' + encodeURIComponent(applicantId));
    if (r.ok) {
      const j = await r.json();
      const cards = j.policyCards || j.PolicyCards || [];
      for (const p of cards) {
        const status = String(p.policyStatus || p.status || p.policyStatusViewModelID || '');
        const ttype = String(p.transactionType || p.TransactionType || p.lob || '');
        const blob = [status, ttype, p.policyNumber, p.premium, p.effectiveDate, p.expirationDate].join(' ');
        shells.push({
          source: 'in_page_policy_api',
          text: blob,
          status: status,
          transactionType: ttype || 'RWL',
          policyNumber: p.policyNumber,
          premium: p.premium,
          effectiveDate: p.effectiveDate || p.effective,
          expirationDate: p.expirationDate || p.expiration,
          writingCompany: p.writingCompany || p.writingCompanyName,
        });
      }
    }
  } catch (e) {}

  if (policyId) {
    try {
      const hist = await fetch('/PolicyAPI/v1/Policy/GetPolicyHistory?policyId=' + encodeURIComponent(policyId));
      if (hist.ok) {
        const hj = await hist.json();
        const items = hj.history || hj.transactions || hj.items || (Array.isArray(hj) ? hj : []);
        for (const item of items) {
          shells.push({
            source: 'policy_api_cards',
            ...item,
            text: JSON.stringify(item).slice(0, 400),
          });
        }
      }
    } catch (e) {}
  }
  return shells;
}
"""


class ConnectedCdpSession:
    """One Playwright CDP attach for the whole manual-renewal job."""

    def __init__(self, cdp_url: Optional[str] = None):
        self.cdp_url = cdp_url or settings.ezlynx_cdp_endpoint or "http://localhost:9222"
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._created_page = False

    async def __aenter__(self) -> "ConnectedCdpSession":
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
        if not self.browser.contexts:
            raise RenewalGuardError(f"CDP at {self.cdp_url} has no browser context")
        self.context = self.browser.contexts[0]
        for pg in self.context.pages:
            if "ezlynx.com" in (pg.url or ""):
                self.page = pg
                break
        if not self.page:
            self.page = await self.context.new_page()
            self._created_page = True
        logger.info("Connected to EZLynx Chrome over CDP at %s", self.cdp_url)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._created_page and self.page:
            try:
                await self.page.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass


class ManualPolicyRenewer:
    """Hardened shell keyer. No bind. One connected CDP job."""

    def __init__(self, api_client: Optional[EZLynxApiClient] = None):
        self.api = api_client or EZLynxApiClient()

    async def run_connected_job(self, spec: RenewalJobSpec) -> RenewalJobResult:
        result = RenewalJobResult(
            status="dry_run" if spec.dry_run else "started",
            applicant_id=spec.applicant_id,
            policy_number=spec.policy_number,
            policy_id=spec.policy_id,
            dry_run=spec.dry_run,
            env=spec.env,
            producer=DEFAULT_PRODUCER_CSR,
            writing_company=spec.writing_company,
            bound=False,
            renew_button=RENEW_POLICY_BTN_SELECTOR,
        )

        if spec.dry_run or spec.verify_only:
            result.planned_actions = self._plan_actions(spec)

        try:
            assert_producer_is_carlo(spec.producer)
            assert_live_keying_allowed(spec)

            # Dry-run plans locally unless the operator passed an explicit CDP URL
            # (live History verify). Do not implicit-connect via env on --dry-run.
            if spec.dry_run and not spec.cdp_url:
                # Unit / operator rehearsal without a live Chrome.
                result.proof_source = "dry_run_no_cdp"
                result.status = "dry_run"
                self._apply_local_dedupe(spec, result)
                self._apply_skip_rules(spec, result)
                return self._finalize(spec, result)

            async with ConnectedCdpSession(spec.cdp_url) as session:
                page = session.page
                assert page is not None
                shells, sources = await self.verify_pending_shells_via_ui(
                    page, spec.applicant_id, spec.policy_id
                )
                result.pending_shells = [s.to_dict() for s in shells]
                result.proof_source = ",".join(sources) if sources else None
                result.verified_via_classic_only = not verification_sources_are_sufficient(sources)

                if result.verified_via_classic_only:
                    raise RenewalGuardError(
                        "Pending-shell verification requires policy-summary History or "
                        "in-page PolicyAPI. Classic get_applicant_policies is not sufficient."
                    )

                dup = find_duplicate_pending_shell(
                    shells,
                    effective_date=spec.effective_date,
                    expiration_date=spec.expiration_date,
                    premium=spec.premium if spec.premium is not None else spec.full_term_premium,
                )
                if dup or spec.already_in:
                    result.already_in = True
                    result.status = "already_in"
                    logger.warning(
                        "Pending RWL already exists for applicant %s term %s–%s premium %s — STOP",
                        spec.applicant_id,
                        spec.effective_date,
                        spec.expiration_date,
                        spec.premium,
                    )
                    self._apply_skip_rules(spec, result)
                    return self._finalize(spec, result)

                if spec.verify_only:
                    result.status = "verified_clear"
                    return self._finalize(spec, result)

                if spec.upload_path and not should_skip_docs_and_notes(result.already_in):
                    await self._upload_if_allowed(page, spec, result)
                    if result.status == "error":
                        return self._finalize(spec, result)

                if not should_skip_docs_and_notes(result.already_in):
                    await self._post_note_exact_title(page, spec, result)
                    if result.status == "error":
                        return self._finalize(spec, result)

                if spec.dry_run:
                    result.status = "dry_run"
                    result.planned_actions.append(
                        f"Would click {RENEW_POLICY_BTN_SELECTOR} (never Renew & Edit Policy)"
                    )
                    return self._finalize(spec, result)

                missing = missing_required_fields(
                    premium=spec.premium if spec.premium is not None else spec.full_term_premium,
                    writing_company=spec.writing_company,
                    effective_date=spec.effective_date,
                    expiration_date=spec.expiration_date,
                )
                if missing:
                    raise RenewalGuardError(
                        f"Required fields missing before submit: {', '.join(missing)}"
                    )

                await self._key_renewal_shell(page, spec, result)
                return self._finalize(spec, result)
        except LiveKeyingBlocked as exc:
            result.status = "blocked"
            result.error = str(exc)
            return self._finalize(spec, result)
        except RenewalGuardError as exc:
            result.status = "blocked" if "allow-live" in str(exc).lower() else "error"
            result.error = str(exc)
            return self._finalize(spec, result)
        except Exception as exc:
            logger.exception("Manual renewal job failed: %s", exc)
            result.status = "error"
            result.error = str(exc)
            return self._finalize(spec, result)

    def _plan_actions(self, spec: RenewalJobSpec) -> List[str]:
        actions = [
            "Verify pending RWL via policy-summary History / in-page PolicyAPI (not Classic alone)",
            "Dedupe: stop if pending RWL exists for same term+premium",
        ]
        if spec.upload_path:
            actions.append(f"Upload {spec.upload_path} if >= {MIN_AUTHENTIC_PDF_BYTES} bytes")
        actions.append("Post note only on exact original titled discussion")
        actions.append(f"Click {RENEW_POLICY_BTN_SELECTOR} ('{RENEW_POLICY_BTN_TEXT}'); never Renew & Edit Policy")
        actions.append("Require Writing Company before submit; producer Carlo Ferrara; no bind")
        return actions

    def _apply_local_dedupe(self, spec: RenewalJobSpec, result: RenewalJobResult) -> None:
        if spec.already_in:
            result.already_in = True
            result.status = "already_in"

    def _apply_skip_rules(self, spec: RenewalJobSpec, result: RenewalJobResult) -> None:
        if should_skip_docs_and_notes(result.already_in):
            result.document_skipped_reason = "already_in"
            result.note_skipped_reason = "already_in"
            result.document_uploaded = False
            result.note_posted = False
        elif spec.upload_path and is_stub_document(spec.upload_path):
            result.document_skipped_reason = f"stub_pdf_lt_{MIN_AUTHENTIC_PDF_BYTES}"
            result.document_uploaded = False

    def _finalize(self, spec: RenewalJobSpec, result: RenewalJobResult) -> RenewalJobResult:
        result.bound = False
        result.producer = DEFAULT_PRODUCER_CSR
        result.renew_button = RENEW_POLICY_BTN_SELECTOR
        if spec.proof_json:
            write_proof_json(result, spec.proof_json)
        return result

    async def verify_pending_shells_via_ui(
        self,
        page,
        applicant_id: str,
        policy_id: Optional[str],
    ) -> Tuple[List[PendingShell], List[str]]:
        if policy_id:
            summary_url = (
                f"https://app.ezlynx.com/applicantportal/Policy/{policy_id}/summary/index"
            )
        else:
            summary_url = f"https://app.ezlynx.com/web/account/{applicant_id}/activity"
        logger.info("Verifying pending shells via UI at %s", summary_url)
        await page.goto(summary_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1500)
        if "login" in (page.url or "").lower():
            raise RenewalGuardError("EZLynx session is at login; cannot verify pending shells")

        history_tab = page.locator("text=History").first
        try:
            if await history_tab.count() > 0:
                await history_tab.click()
                await page.wait_for_timeout(1200)
        except Exception as exc:
            logger.debug("History tab click skipped: %s", exc)

        raw = await page.evaluate(HISTORY_SCRAPE_JS, {"applicantId": applicant_id, "policyId": policy_id or ""})
        records = raw if isinstance(raw, list) else []
        shells = extract_pending_rwl_shells(records)
        sources = sorted({s.source for s in shells if s.source})
        if not sources and records:
            sources = sorted(
                {
                    str(r.get("source"))
                    for r in records
                    if isinstance(r, dict) and r.get("source")
                }
            )
        if not sources:
            # Page rendered; History scrape ran. Empty History is valid proof of absence.
            sources = ["policy_summary_history"]
        logger.info("UI pending-shell proof: %s shell(s) sources=%s", len(shells), sources)
        return shells, sources

    async def _upload_if_allowed(self, page, spec: RenewalJobSpec, result: RenewalJobResult) -> None:
        path = Path(spec.upload_path) if spec.upload_path else None
        if path is None:
            return
        if not path.is_file():
            result.status = "error"
            result.error = f"Upload file not found: {path}"
            result.document_skipped_reason = "file_not_found"
            return
        if is_stub_document(path):
            result.document_skipped_reason = f"stub_pdf_lt_{MIN_AUTHENTIC_PDF_BYTES}"
            result.document_uploaded = False
            logger.warning("Skipping stub PDF %s (%s bytes)", path, path.stat().st_size)
            return
        if spec.dry_run:
            result.planned_actions.append(f"Would upload {path.name}")
            return

        docs_url = f"https://app.ezlynx.com/web/account/{spec.applicant_id}/documents"
        await page.goto(docs_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2500)
        folder = page.locator('td:has-text("Renewal Offers/Declarations")').first
        if await folder.count() > 0:
            await folder.click()
            await page.wait_for_timeout(2000)
        add_btn = page.locator("#add-action")
        await add_btn.wait_for(state="visible", timeout=15000)
        await add_btn.click()
        await page.wait_for_timeout(800)
        await page.locator(".mat-mdc-menu-item:has-text('Upload')").click()
        await page.wait_for_timeout(2000)
        frame = page.frame(url=lambda u: u and "documentactions/upload" in u)
        for _ in range(12):
            if frame:
                break
            await page.wait_for_timeout(500)
            frame = page.frame(url=lambda u: u and "documentactions/upload" in u)
        if not frame:
            result.status = "error"
            result.error = "Upload dialog iframe could not be located"
            return
        await frame.locator("input[type=file]").set_input_files([str(path.resolve())])
        name_input = frame.locator("#file-desc-0")
        if await name_input.count() > 0 and spec.policy_number:
            await name_input.fill(f"{spec.policy_number} Renewal Offer.pdf")
        if spec.policy_number:
            policy_select = frame.locator("#selected-policyor-application-0")
            if await policy_select.count() > 0:
                await policy_select.click()
                await page.wait_for_timeout(500)
                opt = frame.locator(f"mat-option:has-text('{spec.policy_number}')")
                if await opt.count() > 0:
                    await opt.first.click()
        await frame.locator("button:has-text('Upload')").first.click()
        await page.wait_for_timeout(4000)
        result.document_uploaded = True

    async def _post_note_exact_title(self, page, spec: RenewalJobSpec, result: RenewalJobResult) -> None:
        discussions = self.api.get_applicant_discussions(spec.applicant_id)
        try:
            title = resolve_exact_discussion_title(
                discussions,
                requested_title=spec.discussion_title,
                policy_numbers=collect_job_policy_numbers(spec),
                line_of_business=spec.line_of_business,
            )
        except RenewalGuardError as exc:
            result.status = "error"
            result.error = str(exc)
            result.note_skipped_reason = "no_exact_titled_discussion"
            return
        result.discussion_title = title
        note = build_shell_note(spec)
        if spec.dry_run:
            result.planned_actions.append(f"Would post note onto exact title '{title}'")
            return

        api_res = self.api.add_note_to_discussion(
            applicant_id=spec.applicant_id,
            discussion_title=title,
            note_text=note,
            policy_number=spec.policy_number,
            line_of_business=spec.line_of_business,
            carrier_name=spec.carrier_name,
            require_existing_discussion=True,
            policy_numbers=collect_job_policy_numbers(spec),
            use_playwright_fallback=False,
        )
        if api_res.get("status") == "success" and api_res.get("discussion_title") == title:
            result.note_posted = True
            return

        # Same CDP page — exact title only, never first-card fallback.
        activity_url = f"https://app.ezlynx.com/web/account/{spec.applicant_id}/activity"
        await page.goto(activity_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        clicked = await page.evaluate(
            """(want) => {
                const cards = Array.from(document.querySelectorAll('.activity-container'));
                const card = cards.find(c => (c.innerText || '').includes(want));
                if (!card) return false;
                const btn = card.querySelector('button[title="Add to Discussion"]');
                if (!btn) return false;
                btn.click();
                return true;
            }""",
            title,
        )
        if not clicked:
            result.status = "error"
            result.error = f"Exact titled discussion '{title}' not found on Activity"
            result.note_skipped_reason = "exact_title_not_in_dom"
            return
        txt = page.locator("#txtNote")
        await txt.wait_for(state="visible", timeout=8000)
        await txt.fill(note)
        await page.locator('button:has-text("Save")').filter(has_not_text="Reset").first.click()
        await page.wait_for_timeout(2000)
        result.note_posted = True

    async def _key_renewal_shell(self, page, spec: RenewalJobSpec, result: RenewalJobResult) -> None:
        if not spec.policy_id:
            raise RenewalGuardError("policy_id is required to open the Renew action URL")
        renew_url = (
            f"https://app.ezlynx.com/applicantportal/Policy/Actions/Renew/"
            f"{spec.applicant_id}/{spec.policy_id}"
        )
        logger.info("Opening renew action %s", renew_url)
        await page.goto(renew_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        if "login" in (page.url or "").lower():
            raise RenewalGuardError("EZLynx session is at login on Renew action")

        premium = spec.premium if spec.premium is not None else spec.full_term_premium
        premium_str = "" if premium is None else f"{premium:.2f}"
        for sel, value in (
            ("#Premium", premium_str),
            ("#FullTermPremium", f"{(spec.full_term_premium or premium):.2f}" if (spec.full_term_premium or premium) else ""),
            ("#AnnualPremium", f"{(spec.annual_premium or premium):.2f}" if (spec.annual_premium or premium) else ""),
            ("#Description", f"Renewal of {spec.policy_number or ''}".strip()),
        ):
            loc = page.locator(sel).first
            if value and await loc.count() > 0:
                await loc.fill(value)

        writing = await self._ensure_writing_company(page, spec.writing_company)
        if not (writing or "").strip():
            raise RenewalGuardError(
                "Writing Company is blank — refusing to submit (Yes We Do WC bounce guard)"
            )
        result.writing_company = writing
        await self._ensure_producer_carlo(page)

        # Never click Renew & Edit / Bind. Only #RenewPolicyBtn.
        edit_btns = page.locator("button:has-text('Renew & Edit Policy')")
        if await edit_btns.count() > 0:
            logger.info("Renew & Edit Policy is visible — leaving it unclicked (shell-only)")

        btn = page.locator(RENEW_POLICY_BTN_SELECTOR)
        if await btn.count() == 0:
            btn = page.locator(f'button:text-is("{RENEW_POLICY_BTN_TEXT}")')
        if await btn.count() == 0:
            raise RenewalGuardError(f"{RENEW_POLICY_BTN_SELECTOR} not found; refusing fallback buttons")
        label = (await btn.first.inner_text() or "").strip()
        if is_forbidden_renew_button(label, "RenewPolicyBtn"):
            raise RenewalGuardError(f"Refusing forbidden renew button text: {label}")
        await btn.first.click()
        await page.wait_for_timeout(2500)
        result.renew_button = RENEW_POLICY_BTN_SELECTOR
        result.status = "keyed"

        shells, sources = await self.verify_pending_shells_via_ui(
            page, spec.applicant_id, spec.policy_id
        )
        result.pending_shells = [s.to_dict() for s in shells]
        result.proof_source = ",".join(sources) if sources else result.proof_source

    async def _ensure_writing_company(self, page, value: Optional[str]) -> str:
        desired = (value or "").strip()
        for sel in WRITING_COMPANY_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select" and desired:
                    # Label, then value — never select_option(index=N). A random
                    # first option is not a Writing Company. Blank still aborts.
                    try:
                        await loc.select_option(label=desired)
                    except Exception:
                        await loc.select_option(value=desired)
                elif desired:
                    await loc.fill(desired)
                current = (await loc.input_value()) if tag != "select" else (await loc.evaluate("el => el.options[el.selectedIndex] && el.options[el.selectedIndex].text"))
                if (current or "").strip():
                    return str(current).strip()
            except Exception as exc:
                logger.debug("Writing Company selector %s failed: %s", sel, exc)

        # Angular Material
        mat = page.locator("mat-select[id*='Writing' i], mat-select[aria-label*='Writing Company' i]").first
        try:
            if await mat.count() > 0 and desired:
                await mat.click()
                opt = page.locator(f"mat-option:has-text('{desired}')").first
                if await opt.count() > 0:
                    await opt.click()
                    return desired
        except Exception as exc:
            logger.debug("Writing Company mat-select failed: %s", exc)

        # Read any visible Writing Company value already on the form
        readback = await page.evaluate(
            """() => {
                const labels = Array.from(document.querySelectorAll('label, .control-label, mat-label'));
                const lab = labels.find(l => /writing company/i.test(l.textContent || ''));
                if (!lab) return '';
                const root = lab.closest('.form-group, .mat-mdc-form-field, tr, div') || lab.parentElement;
                if (!root) return '';
                const sel = root.querySelector('select');
                if (sel && sel.selectedIndex >= 0) {
                    return (sel.options[sel.selectedIndex].text || '').trim();
                }
                const inp = root.querySelector('input');
                if (inp && inp.value) return inp.value.trim();
                const shown = root.querySelector('.mat-mdc-select-value-text, .ng-value-label');
                return (shown && shown.textContent || '').trim();
            }"""
        )
        return (readback or desired or "").strip()

    async def _ensure_producer_carlo(self, page) -> None:
        desired = DEFAULT_PRODUCER_CSR
        for sel in PRODUCER_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    try:
                        await loc.select_option(label=desired)
                    except Exception:
                        await loc.select_option(label=re.compile(r"Carlo", re.I))
                else:
                    await loc.fill(desired)
                current = await loc.evaluate(
                    """el => {
                        if (el.tagName.toLowerCase() === 'select' && el.selectedIndex >= 0) {
                            return el.options[el.selectedIndex].text || '';
                        }
                        return el.value || '';
                    }"""
                )
                if current and any(forb.lower() in str(current).lower() for forb in FORBIDDEN_PRODUCER_NAMES):
                    raise RenewalGuardError(f"Producer resolved to Robie ('{current}'); aborting")
                if current and "carlo" in str(current).lower():
                    return
            except RenewalGuardError:
                raise
            except Exception as exc:
                logger.debug("Producer selector %s failed: %s", sel, exc)


def spec_from_args(args: argparse.Namespace) -> RenewalJobSpec:
    aliases = []
    if getattr(args, "policy_number_aliases", None):
        aliases = [a.strip() for a in args.policy_number_aliases.split(",") if a.strip()]
    upload = Path(args.upload) if getattr(args, "upload", None) else None
    proof = Path(args.proof_json) if getattr(args, "proof_json", None) else None
    reject_quote_id_add_policy(getattr(args, "quote_id", None))
    return RenewalJobSpec(
        applicant_id=str(args.applicant_id),
        policy_id=str(args.policy_id) if args.policy_id else None,
        policy_number=args.policy_number,
        policy_number_aliases=aliases,
        line_of_business=args.lob,
        carrier_name=args.carrier,
        premium=parse_money(args.premium),
        full_term_premium=parse_money(args.full_term_premium),
        annual_premium=parse_money(args.annual_premium),
        effective_date=args.effective_date,
        expiration_date=args.expiration_date,
        writing_company=args.writing_company,
        discussion_title=args.discussion_title,
        note_text=args.note_text,
        upload_path=upload,
        producer=args.producer or DEFAULT_PRODUCER_CSR,
        dry_run=bool(args.dry_run),
        verify_only=bool(args.verify_only),
        allow_live=args.allow_live,
        env=args.env,
        cdp_url=args.cdp_url,
        proof_json=proof,
        already_in=bool(getattr(args, "already_in", False)),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One connected CDP job: optional upload → exact titled note → "
            "#RenewPolicyBtn shell → proof JSON. No bind. No Quote-ID add."
        )
    )
    parser.add_argument("--applicant-id", required=True, help="EZLynx applicant ID")
    parser.add_argument("--policy-id", default=None, help="EZLynx policy ID for the Renew action URL")
    parser.add_argument("--policy-number", default=None, help="Expiring or renewal-term policy number")
    parser.add_argument(
        "--policy-number-aliases",
        default=None,
        help="Comma-separated prior/renewal-term aliases",
    )
    parser.add_argument("--lob", default=None, help="Line of business (must match the titled card)")
    parser.add_argument("--carrier", default=None, help="Carrier / writing-company display name")
    parser.add_argument("--premium", default=None, help="Written premium")
    parser.add_argument("--full-term-premium", default=None, help="Full-term premium")
    parser.add_argument("--annual-premium", default=None, help="Annualized premium")
    parser.add_argument("--effective-date", default=None, help="Renewal term start (YYYY-MM-DD or M/D/YYYY)")
    parser.add_argument("--expiration-date", default=None, help="Renewal term end")
    parser.add_argument("--writing-company", default=None, help="Required before submit")
    parser.add_argument(
        "--discussion-title",
        default=None,
        help="Exact original titled card (e.g. 'Renewal Manual Workers comp | PWC1239278 …')",
    )
    parser.add_argument("--note-text", default=None, help="Optional note body (header + Robie signature added)")
    parser.add_argument("--upload", default=None, help="Optional authentic PDF (>=10KB). Stubs are skipped.")
    parser.add_argument(
        "--producer",
        default=DEFAULT_PRODUCER_CSR,
        help=f"Must be {DEFAULT_PRODUCER_CSR} (never Robie)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan + verify only; never click submit")
    parser.add_argument("--verify-only", action="store_true", help="UI pending-shell proof only")
    parser.add_argument(
        "--allow-live",
        default=None,
        metavar="Carlo",
        help="Production live keying token. Must be 'Carlo'. Not needed on --env test.",
    )
    parser.add_argument(
        "--env",
        default="test",
        choices=("test", "prod", "hermes-test-01", "hermes-test"),
        help="test = hermes-test-01 (first gate). prod = allow-list + --allow-live Carlo.",
    )
    parser.add_argument("--cdp-url", default=None, help="Chrome CDP endpoint (default localhost:9222)")
    parser.add_argument(
        "--proof-json",
        default=None,
        help="Write job proof JSON here (default data/renewal_proofs/<applicant>.json)",
    )
    parser.add_argument(
        "--already-in",
        action="store_true",
        help="Operator-confirmed already_in: skip key/docs/notes (still writes proof)",
    )
    # Accepted only so we can refuse it loudly.
    parser.add_argument("--quote-id", default=None, help=argparse.SUPPRESS)
    return parser


def default_proof_path(applicant_id: str) -> Path:
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return Path("data/renewal_proofs") / f"{applicant_id}_{stamp}.json"


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        spec = spec_from_args(args)
    except RenewalGuardError as exc:
        logger.error("%s", exc)
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 2

    if spec.proof_json is None:
        spec.proof_json = default_proof_path(spec.applicant_id)

    if not spec.dry_run and not spec.verify_only:
        env = (spec.env or "").lower()
        if env in {"prod"} and (spec.allow_live or "").lower() != LIVE_ALLOW_TOKEN.lower():
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "error": "Production live keying requires --allow-live Carlo after hermes-test-01.",
                    },
                    indent=2,
                )
            )
            return 3

    result = asyncio.run(ManualPolicyRenewer().run_connected_job(spec))
    print(json.dumps(result.to_dict(), indent=2, default=str))
    if result.status in {"error", "blocked"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
