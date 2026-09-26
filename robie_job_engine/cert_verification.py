"""Chunk 2: client verification for certificates intake.

Read-only. Takes step-1 intake records and verifies the client identity
before anything is filed. Produces VERIFIED records (with evidence) or HOLD
records (with reasons). Never writes to Gmail or EZLynx. Never creates or
merges applicants — there is no code path in this module that does.

Verification sources, in order:
  1. Full-book report match from step 1, anchored by policy number against
     EZLynx PolicyApi.
  2. EZLynx PolicyApi fallback: search each policy number from the email;
     exactly one distinct applicant across all hits verifies the client.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

VERIFIED = "VERIFIED"
HOLD = "HOLD"

ACTION_NEW_REQUEST = "new_request"
ACTION_ACK = "acknowledgement"
ACTION_UNKNOWN = "unknown"
ACTION_AUTOREPLY = "auto_reply"
# EZLynx Client Center portal request: the customer submitted the form
# themselves and EZLynx auto-created the task. Genuine request, but the
# worker must never create a duplicate task (task_action already_exists).
ACTION_CLIENT_CENTER = "client_center"

POLICY_API_BASE = "https://app.ezlynx.com/PolicyApi"
DISCUSSION_API_BASE = "https://app.ezlynx.com/DiscussionApi"


class FilingTargetMismatch(RuntimeError):
    """Fail-closed: the filing target did not pass the triple check."""


# ---------------------------------------------------------------------------
# Subject-line insured extraction lives in cert_intake (shared with the
# intake extractor so both agree). Re-exported here for compatibility.
# ---------------------------------------------------------------------------
try:
    from .cert_intake import (
        _MC_SUFFIX,
        _NAME_LIKE,
        _POLICY_TAIL,
        _SUBJECT_PATTERNS,
        _SUBJECT_PREFIXES,
        _strip_subject_prefixes,
        extract_subject_insured,
    )
except ImportError:  # loaded outside the package
    from robie_job_engine.cert_intake import (
        _MC_SUFFIX,
        _NAME_LIKE,
        _POLICY_TAIL,
        _SUBJECT_PATTERNS,
        _SUBJECT_PREFIXES,
        _strip_subject_prefixes,
        extract_subject_insured,
    )


# ---------------------------------------------------------------------------
# Requested-action classification (feeds the task-state rule)
# ---------------------------------------------------------------------------

_ACK_PATTERNS = [
    re.compile(r"\bthank\s*you\b", re.IGNORECASE),
    re.compile(r"\bthanks\b", re.IGNORECASE),
    re.compile(r"\breceived the certificate\b", re.IGNORECASE),
    re.compile(r"\bgot the certificate\b", re.IGNORECASE),
    re.compile(r"\bconfirming receipt\b", re.IGNORECASE),
]

_NEW_REQUEST_PATTERNS = [
    re.compile(r"\bplease\s+(issue|provide|send|forward)\b", re.IGNORECASE),
    re.compile(r"\brequest(ing|ed)?\s+(a|the|for)?\s*(certificate|coi)\b", re.IGNORECASE),
    re.compile(r"\bneed\s+a\s+(certificate|coi)\b", re.IGNORECASE),
    re.compile(r"\brenewal\s+certificate\b", re.IGNORECASE),
]

# Auto-replies quote the original request's subject ("Re: Renewal
# Certificate Request- ..."), so subject/body request language must NOT
# classify them as new requests — that is what mints duplicate tasks.
# Body language is the signal; sender-based detection would wrongly flag
# genuine RMIS requests (donotreply@ senders are real requesters).
_AUTOREPLY_PATTERNS = [
    re.compile(r"\bautomated response\b", re.IGNORECASE),
    re.compile(r"\bauto[\-\s]?reply\b", re.IGNORECASE),
    re.compile(r"\bout of office\b", re.IGNORECASE),
    re.compile(r"\bdo not reply\b", re.IGNORECASE),
    re.compile(r"\bthis (mailbox|inbox|email) is not monitored\b", re.IGNORECASE),
    re.compile(r"\bdelivery (failure|status notification)\b", re.IGNORECASE),
    re.compile(r"\bmessage not delivered\b", re.IGNORECASE),
    re.compile(r"\bvacation responder\b", re.IGNORECASE),
    # Bounce/NDR body language the earlier patterns missed: "Undeliverable
    # email: Certificate of Insurance ..." quoted 39 such bounces in the
    # 2026-09-26 sweep and they verified. Never again.
    re.compile(r"\bundeliverable\b", re.IGNORECASE),
    re.compile(r"\bdelivery has failed\b", re.IGNORECASE),
    re.compile(r"\bmail delivery (failed|failure)\b", re.IGNORECASE),
]

# Sender local-parts that are always automated responders. Kept narrow:
# "donotreply" is deliberately absent — RMIS requests come from
# donotreply@ senders and are genuine.
_AUTOREPLY_SENDERS = (
    "canned.response",
    "mailer-daemon",
    "postmaster",
    "autoresponder",
)

# Bounce subjects: a delivery failure quoting the original request subject
# ("Undeliverable email: Certificate of Insurance for ...") is never a new
# request, even though the quoted tail carries certificate language.
_BOUNCE_SUBJECT_RE = re.compile(
    r"(?i)^\s*(undeliverable|undelivered|delivery (status notification|"
    r"failure)|failure notice|returned mail|mail delivery (failed|failure))"
)

# The subject is shaped like a certificate request when it names the
# insured in one of the known request shapes ("Certificate of Insurance
# LA Burger LLC to Anderson Market", "Renewal Certificate Request-
# Abg Transportation", "COI - Fonseca General Contractor LLC"). The
# subject must also carry certificate/coi language — a bare policy tail
# ("Haris Uddin 008265/15/00") is not enough on its own.
_CERT_SUBJECT_RE = re.compile(r"(?i)\b(certificate|coi)\b")

# A terse subject that is nothing but "Coi" (optionally after Re:/Fwd:)
# is a genuine client certificate request — the word "certificate"
# spelled out is not required. Deliberately narrow: "COI received" or
# "Coi attached" (a statement, not a request) does NOT match.
_BARE_COI_SUBJECT_RE = re.compile(r"^\s*coi\s*$", re.IGNORECASE)


def _subject_is_request_shape(subject: str) -> bool:
    if not _CERT_SUBJECT_RE.search(subject or ""):
        return False
    return extract_subject_insured(subject) is not None


def _subject_is_bare_coi(subject: str) -> bool:
    """True when the subject is exactly "Coi" (after Re:/Fwd: stripping).

    Terse client subjects ("Coi") are genuine requests. The automated and
    bounce filters in classify_requested_action run BEFORE this, so
    postmaster bounces ("Undeliverable: Coi") and canned auto-replies
    ("Re: Coi" from the agency responder) stay held.
    """
    return bool(_BARE_COI_SUBJECT_RE.match(
        _strip_subject_prefixes(subject or "")))


def _sender_is_autoresponder(sender: str | None) -> bool:
    local = (sender or "").split("@", 1)[0].lower()
    return any(token in local for token in _AUTOREPLY_SENDERS)


def classify_requested_action(subject: str, body: str,
                              sender: str | None = None) -> str:
    """new_request vs acknowledgement vs auto_reply vs unknown.

    Acknowledgements ("received the certificate", "thanks") must never be
    treated as new certificate requests — that is what creates duplicate
    tasks. Auto-replies are checked before new-request phrases because they
    often quote an original request subject. But genuine request language in
    the BODY always beats auto-reply boilerplate: vendor systems (RMIS et
    al.) routinely footer real requests with "please do not reply", and that
    footer must not turn a real request into an auto-reply.

    A subject shaped like a certificate request ("Certificate of Insurance
    X to Y", "Renewal Certificate Request- X", "COI - X") counts as request
    language even when the body is a bare certificate PDF — the subject is
    the request. Bounce/undeliverable subjects are checked first so a
    failure notice quoting a request subject never classifies as a request.
    """
    # A bounce quoting the original subject is definitive: it never issues
    # a genuine request, even when the quoted tail names a certificate.
    if _BOUNCE_SUBJECT_RE.search(subject or ""):
        return ACTION_AUTOREPLY
    # A known autoresponder address is definitive: it never issues a
    # genuine request, even when it quotes the original subject.
    if _sender_is_autoresponder(sender):
        return ACTION_AUTOREPLY
    body_text = body or ""
    body_has_request = any(
        p.search(body_text) for p in _NEW_REQUEST_PATTERNS)
    if not body_has_request and any(
            p.search(body_text) for p in _AUTOREPLY_PATTERNS):
        return ACTION_AUTOREPLY
    text = f"{subject or ''}\n{body_text}"
    if any(p.search(text) for p in _ACK_PATTERNS):
        return ACTION_ACK
    if (any(p.search(text) for p in _NEW_REQUEST_PATTERNS)
            or _subject_is_request_shape(subject)
            or _subject_is_bare_coi(subject)):
        return ACTION_NEW_REQUEST
    return ACTION_UNKNOWN


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

_ENTITY_SUFFIXES = ("llc", "inc", "corp", "ltd", "co", "company", "pllc",
                    "pa", "pc", "lp", "llp")


def normalize_insured_name(name: str | None) -> str:
    """Normalize for comparison only — never for display or matching keys."""
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name).encode(
        "ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-z0-9 ]", " ", folded.lower())
    n = re.sub(r"\s+", " ", n).strip()
    words = n.split()
    while words and words[-1] in _ENTITY_SUFFIXES:
        words.pop()
    return " ".join(words)


def detect_insured_conflict(candidates: dict[str, str | None]) -> str | None:
    """Return a conflict description, or None when sources agree.

    ``candidates`` maps source name -> insured name (None = not found there).
    Two sources naming genuinely different insureds is a hold.
    """
    seen: dict[str, str] = {}
    for source, name in candidates.items():
        norm = normalize_insured_name(name)
        if norm:
            seen[source] = norm
    distinct = set(seen.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{s}={seen[s]!r}" for s in sorted(seen))
        return f"conflicting insured names across sources: {detail}"
    return None


# ---------------------------------------------------------------------------
# OCR hook (scanned PDFs)
# ---------------------------------------------------------------------------

def ocr_pdf_bytes(data: bytes) -> str | None:
    """Best-effort OCR of scanned PDF bytes.

    Returns extracted text, or None when OCR is unavailable or yields
    nothing. tesseract is not installed on the worker host, so scanned
    PDFs hold for human review until it is.
    """
    if not data or not shutil.which("tesseract"):
        return None
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng"],
            input=data, capture_output=True, timeout=120,
        )
        text = proc.stdout.decode("utf-8", "replace").strip()
        return text or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Read-only EZLynx client
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _read_secret_json(resource_name: str) -> dict[str, Any]:
    """Read a Secret Manager JSON secret. Transient — never stored."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    resp = client.access_secret_version(request={"name": resource_name})
    return json.loads(resp.payload.data.decode("utf-8"))


def ezlynx_oauth_token(
    scope_override: str | None = None,
    secret_resource: str = "",
    token_endpoint: str = "",
) -> str:
    """Mint an EZLynx OAuth token via the vendor_data_access grant.

    Reads the integration secret named by ROBIE_EZLYNX_API_PROD_SECRET
    (full Secret Manager resource name). Values are transient.
    """
    secret_resource = secret_resource or _env("ROBIE_EZLYNX_API_PROD_SECRET")
    if not secret_resource:
        raise RuntimeError("ROBIE_EZLYNX_API_PROD_SECRET is not set")
    cfg = _read_secret_json(secret_resource)
    data = urllib.parse.urlencode({
        "grant_type": "vendor_data_access",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "username": cfg["username"],
        "integration_group_id": cfg["integration_group_id"],
        "scope": scope_override or str(cfg.get("scope", "")),
    }).encode()
    endpoint = token_endpoint or cfg["token_endpoint"]
    req = urllib.request.Request(endpoint, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["access_token"]


class EzlynxReadClient:
    """Read-only EZLynx API client. No write methods exist on this class."""

    def __init__(self, token_provider: Any = None) -> None:
        self._token_provider = token_provider or ezlynx_oauth_token
        self._token = ""
        self._token_at = 0.0

    def _token_now(self) -> str:
        if not self._token or time.time() - self._token_at > 3000:
            self._token = self._token_provider()
            self._token_at = time.time()
        return self._token

    def _get(self, url: str, params: dict[str, str] | None = None) -> Any:
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(
            url + query,
            headers={"Authorization": f"Bearer {self._token_now()}",
                     "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            return {"_http_error": e.code,
                    "_body": e.read().decode("utf-8", "replace")[:500]}

    def search_policies(self, policy_number: str) -> list[dict[str, Any]]:
        """GET /PolicyApi/policy/v1/search?PolicyNumber=... Rows carry the
        applicant id — the anchor for the triple check."""
        rows: list[dict[str, Any]] = []
        for candidate in dict.fromkeys([policy_number,
                                        re.sub(r"[^A-Za-z0-9]", "",
                                               policy_number or "")]):
            if not candidate:
                continue
            body = self._get(f"{POLICY_API_BASE}/policy/v1/search",
                             {"PolicyNumber": candidate,
                              "PageIndex": "1", "PageSize": "20"})
            if isinstance(body, dict) and body.get("_http_error"):
                rows.append({"_search_error": body["_http_error"],
                             "_policy_number": candidate})
                continue
            for key in ("Records", "records", "Items", "items", "Data",
                        "data", "policies", "Policies"):
                if isinstance(body, dict) and isinstance(body.get(key), list):
                    rows.extend(body[key])
                    break
            else:
                if isinstance(body, list):
                    rows.extend(body)
        return rows

    def get_discussions(self, applicant_id: int | str) -> list[dict[str, Any]]:
        """GET DiscussionApi v8/discussions/by-applicant (host-only base)."""
        body = self._get(
            f"{DISCUSSION_API_BASE}/v8/discussions/by-applicant",
            {"applicantId": str(applicant_id)},
        )
        if isinstance(body, dict):
            for key in ("Records", "records", "Items", "items", "Data",
                        "data", "discussions", "Discussions"):
                if isinstance(body.get(key), list):
                    return body[key]
            return []
        return body if isinstance(body, list) else []


def _applicant_id_from_policy_row(row: dict[str, Any]) -> int | None:
    for key in ("applicantId", "ApplicantId", "applicantID", "accountId",
                "AccountId", "AccountID"):
        val = row.get(key)
        if val:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return None


def _row_name_fields(row: dict[str, Any]) -> list[str]:
    names = []
    for key in ("namedInsured", "insuredName", "accountName", "Name",
                "name", "ApplicantName", "applicantName"):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            names.append(val.strip())
    return names


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    status: str = HOLD
    applicant_id: int | None = None
    insured_name: str | None = None
    policy_numbers: list[str] = field(default_factory=list)
    requester_name: str | None = None
    requester_email: str | None = None
    requester_is_third_party: bool = False
    holder_names: list[str] = field(default_factory=list)
    requested_action: str = ACTION_UNKNOWN
    evidence: list[str] = field(default_factory=list)
    hold_reasons: list[str] = field(default_factory=list)
    source: str = ""  # "report" | "ezlynx" | ""
    # "client_center" when the request came through the EZLynx Client
    # Center portal — the task already exists, never duplicate it.
    origin: str = ""

    def hold(self, reason: str) -> "VerificationResult":
        self.status = HOLD
        self.hold_reasons.append(reason)
        return self


def _policy_anchors(rows: list[dict[str, Any]],
                    applicant_id: int | None) -> tuple[bool, list[str]]:
    """True when at least one policy row anchors to the applicant."""
    evidence: list[str] = []
    if applicant_id is None:
        return False, evidence
    for row in rows:
        if row.get("_search_error"):
            evidence.append(
                f"policy search error {row['_search_error']} "
                f"for {row.get('_policy_number')}")
            continue
        rid = _applicant_id_from_policy_row(row)
        if rid == applicant_id:
            evidence.append(
                f"policy {row.get('policyNumber') or row.get('PolicyNumber')}"
                f" anchors to applicant {applicant_id}")
            return True, evidence
    return False, evidence


def verify_record(record: Any, index: Any,
                  verifier: EzlynxReadClient | None = None) -> VerificationResult:
    """Verify one intake record. Read-only; never creates or merges
    applicants."""
    res = VerificationResult(
        insured_name=record.facts.insured_name,
        policy_numbers=list(record.facts.policy_numbers),
        requester_name=record.facts.requester_name,
        requester_email=record.facts.requester_email,
        holder_names=list(record.facts.holder_names),
    )

    # Scanned/unreadable PDFs: OCR attempt, else hold.
    if record.facts.pdf_unreadable:
        ocr_texts = []
        for att in getattr(record, "attachments", []) or []:
            text = ocr_pdf_bytes(getattr(att, "content", b"") or b"")
            if text:
                ocr_texts.append(text)
        if not ocr_texts:
            return res.hold(
                "PDF attachment unreadable (likely scanned) and OCR is "
                "unavailable on the worker host — holding for human review, "
                "never guessing")
        record.facts.pdf_texts.extend(ocr_texts)
        res.evidence.append("OCR recovered text from scanned PDF")

    # Gather insured candidates from every source; conflicts hold.
    pdf_names = []
    for text in record.facts.pdf_texts:
        for pat in _SUBJECT_PATTERNS:
            m = pat.search(text or "")
            if m and _NAME_LIKE.search(m.group(1)):
                pdf_names.append(m.group(1).strip())
                break
    candidates = {
        "email_body": record.facts.insured_name,
        "subject": extract_subject_insured(record.subject),
        "pdf": pdf_names[0] if pdf_names else None,
    }
    conflict = detect_insured_conflict(candidates)
    if conflict:
        return res.hold(conflict)
    agreed = next((normalize_insured_name(v) for v in candidates.values()
                   if normalize_insured_name(v)), "")
    if agreed:
        # Keep the most complete original spelling for display.
        for v in candidates.values():
            if normalize_insured_name(v) == agreed:
                res.insured_name = v
                break

    # Third-party requester classification: the sender is the requester,
    # never automatically the client.
    res.requester_is_third_party = bool(
        record.facts.requester_is_third_party)
    if res.requester_is_third_party:
        res.evidence.append(
            f"third-party requester: {res.requester_name} "
            f"<{res.requester_email}> is not the insured")

    # Requested action: new request vs acknowledgement vs auto-reply.
    # Classified on the email body AND the PDF texts: the request language
    # usually lives in the body ("Please issue a certificate"), while
    # attached request forms live in the PDFs. Subject is included by the
    # classifier itself.
    #
    # EZLynx Client Center notifications bypass generic classification:
    # intake already proved the portal origin and the certificate request
    # type. They are genuine requests whose task EZLynx auto-created.
    if getattr(record.facts, "origin", None) == "client_center":
        res.requested_action = ACTION_CLIENT_CENTER
        res.origin = "client_center"
        res.evidence.append(
            "EZLynx Client Center notification: the customer submitted the "
            "request in the portal and EZLynx auto-created the task — "
            "the worker never creates a duplicate task")
    else:
        body_text = "\n".join(
            t for t in [getattr(record.facts, "body_text", "") or ""]
            + list(record.facts.pdf_texts) if t
        )
        res.requested_action = classify_requested_action(
            record.subject, body_text,
            sender=getattr(record.facts, "requester_email", None))
    if res.requested_action == ACTION_ACK:
        res.evidence.append(
            "message classified as acknowledgement/thank-you — not a new "
            "certificate request")
    elif res.requested_action == ACTION_AUTOREPLY:
        res.evidence.append(
            "message classified as automated response — never a new "
            "certificate request")
        return res.hold(
            "automated response (auto-reply/bounce), not a certificate "
            "request — holding, never filing or tasking")
    elif res.requested_action == ACTION_UNKNOWN:
        # Not a proven request: internal digests, call-analysis forwards,
        # vendor-doc updates, and anything else without request language.
        # Verifying these is what minted the 54 false positives in the
        # 2026-09-26 sweep — hold, never file or task.
        res.evidence.append(
            "message could not be classified as a certificate request — "
            "no request language in subject or body")
        return res.hold(
            "unclassifiable message (requested_action unknown): not a "
            "proven certificate request — holding, never filing or tasking")

    match = record.match
    status = getattr(match, "status", "")

    if status == "MATCHED":
        res.applicant_id = match.applicant_id
        res.source = "report"
        res.evidence.append(
            f"full-book report match: applicant {match.applicant_id}")
        if res.policy_numbers and verifier is not None:
            anchored = False
            for num in res.policy_numbers:
                ok, ev = _policy_anchors(verifier.search_policies(num),
                                         res.applicant_id)
                res.evidence.extend(ev)
                anchored = anchored or ok
            if not anchored:
                return res.hold(
                    "policy number(s) from the email do not anchor to "
                    f"applicant {res.applicant_id} in EZLynx — possible "
                    "wrong-applicant match; holding")
        res.status = VERIFIED
        return res

    if status == "AMBIGUOUS":
        return res.hold(
            f"name matches multiple applicants in the report: "
            f"{getattr(match, 'candidates', '')} — holding for human")

    # NO_MATCH: EZLynx fallback via policy numbers.
    if verifier is None:
        return res.hold(
            "no applicant in the full-book report; holding for EZLynx lookup")
    applicant_ids: set[int] = set()
    for num in res.policy_numbers:
        for row in verifier.search_policies(num):
            if row.get("_search_error"):
                res.evidence.append(
                    f"policy search error {row['_search_error']} for {num}")
                continue
            rid = _applicant_id_from_policy_row(row)
            if rid:
                applicant_ids.add(rid)
                for field_name in _row_name_fields(row):
                    if (normalize_insured_name(field_name)
                            and normalize_insured_name(field_name)
                            != normalize_insured_name(res.insured_name or "")):
                        res.evidence.append(
                            f"EZLynx row names {field_name!r} vs email "
                            f"{res.insured_name!r}")
    if len(applicant_ids) == 1:
        res.applicant_id = next(iter(applicant_ids))
        res.source = "ezlynx"
        res.evidence.append(
            f"EZLynx PolicyApi anchors policy to applicant {res.applicant_id}")
        res.status = VERIFIED
        return res
    if len(applicant_ids) > 1:
        return res.hold(
            f"policy numbers anchor to multiple applicants in EZLynx: "
            f"{sorted(applicant_ids)} — holding")
    return res.hold(
        "no applicant in the full-book report and no policy anchor in "
        "EZLynx — holding for human")


# ---------------------------------------------------------------------------
# Triple filing guard (Carlo's standing rule, 2026-09-25)
# ---------------------------------------------------------------------------

def verify_filing_target(verified: VerificationResult, discussion_id: Any,
                         verifier: EzlynxReadClient) -> bool:
    """Fail-closed triple check before any email-sourced EZLynx write.

    1. The verified record's insured/policy agrees with the email's
       (guaranteed by verify_record — status must be VERIFIED).
    2. The email's policy digits are anchored to the applicant in EZLynx.
    3. The selected discussion belongs to that applicant.

    Raises FilingTargetMismatch on any failure. Nothing is written here;
    the filing step calls this first.
    """
    if verified.status != VERIFIED or not verified.applicant_id:
        raise FilingTargetMismatch(
            "record is not VERIFIED — refusing to file")

    if verified.policy_numbers:
        anchored = False
        for num in verified.policy_numbers:
            ok, _ = _policy_anchors(verifier.search_policies(num),
                                    verified.applicant_id)
            anchored = anchored or ok
        if not anchored:
            raise FilingTargetMismatch(
                f"policy {verified.policy_numbers} does not anchor to "
                f"applicant {verified.applicant_id} in EZLynx")
    else:
        # No policy digits in the email: the anchor is the report/EZLynx
        # name evidence recorded on the verified result.
        if not verified.evidence:
            raise FilingTargetMismatch(
                "no policy numbers and no verification evidence — refusing")

    discussions = verifier.get_discussions(verified.applicant_id)
    ids = {str(d.get("id") or d.get("discussionId") or d.get("DiscussionId"))
           for d in discussions if isinstance(d, dict)}
    if str(discussion_id) not in ids:
        raise FilingTargetMismatch(
            f"discussion {discussion_id} does not belong to applicant "
            f"{verified.applicant_id}")
    return True
