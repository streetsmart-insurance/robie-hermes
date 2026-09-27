"""match() step for the hello intake sweep worker (PR #611 follow-up).

Carrier notices often carry the truth in a document, not in the email
body: the policy number in the email may not be in EZLynx, may be
inaccurate, or the insured name may be abbreviated ("Seci" vs
"SECI Construction Inc"). This module navigates that instead of
hard-failing.

Pipeline for one genuine hello@ message:

  1. retrieve_docs: attachments -> text; direct document links in the
     body -> text. Links that need a logged-in carrier portal session
     are NOT fetched — recorded as portal_link_needs_human and the
     worker moves on.
  2. extract_doc_identity: policy numbers + insured names as printed on
     the CARRIER DOC. These outrank the email body's claims; any
     conflict is recorded as evidence.
  3. match ladder (first hit wins, confidence attached):
       a. hello sender-alias store (human-confirmed "strong" aliases).
          Skipped for vendor/carrier/system senders — they never become
          fixed aliases (one address covers many insureds).
       b. report email: sender email against the client roster.
       c. policy ladder: exact -> normalized -> EZLynx policy anchor
          (via an injected policy_search, fail-closed like
          hello_triage.resolve_applicant_id).
       d. fuzzy name vs roster, scored. A weak fuzzy match is never a
          hit; a name-only match never auto-files.
  4. confidence high / medium / low. Only high auto-files. medium and
     low go to the hello unmatched queue with everything tried and
     found, so a human clears them in one glance — and resolving one
     teaches the alias store, so it never recurs.

Read-only: no EZLynx writes, no Zapier, no email. Network happens only
through injected fetchers (tests inject fakes); the default URL fetcher
is plain urllib GET with a timeout and size cap, no credentials, no
POST. The Gmail attachment bytes are passed in by the caller — this
module never calls the Gmail API itself.
"""

from __future__ import annotations

import io
import re
import urllib.request
from difflib import SequenceMatcher

from .hello_classifier import _POLICY_PATTERNS
from .hello_triage import resolve_applicant_id
from .hello_unmatched_queue import enqueue_unmatched, sender_is_vendor

# ---------------------------------------------------------------------------
# Document retrieval
# ---------------------------------------------------------------------------

# Extensions we will attempt to extract text from.
_DOC_EXTENSIONS = (".pdf", ".txt", ".rtf")

# Links that are clearly not documents are never fetched.
_NON_DOC_URL_HINTS = (
    "unsubscribe", "optout", "opt-out", "tracking", "pixel",
    "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "instagram.com", "youtube.com",
)

_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)

_DEFAULT_FETCH_TIMEOUT_SECS = 10
_DEFAULT_FETCH_MAX_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_LINKS = 5


def extract_pdf_text(data: bytes, max_pages: int = 8) -> str | None:
    """Plain text from PDF bytes via pypdf. None when unreadable."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        chunks: list[str] = []
        for page in reader.pages[:max_pages]:
            chunks.append(page.extract_text() or "")
        text = "\n".join(chunks).strip()
        return text or None
    except Exception:
        return None


def retrieve_attachment_texts(
    attachments: list[tuple[str, bytes]] | None,
) -> list[dict]:
    """Extract text from email attachments.

    attachments: [(filename, content_bytes)] as handed over by the Gmail
    layer. Returns [{filename, text}]. Unreadable/unsupported files are
    skipped with a note in the returned dict's "note" key — never fatal.
    """
    docs: list[dict] = []
    for filename, content in (attachments or []):
        name = (filename or "").strip()
        lowered = name.lower()
        entry: dict = {"filename": name or "(unnamed)", "text": None,
                       "note": None}
        try:
            if lowered.endswith(".pdf"):
                entry["text"] = extract_pdf_text(content or b"")
                if not entry["text"]:
                    entry["note"] = "PDF had no extractable text"
            elif lowered.endswith(".txt"):
                entry["text"] = (content or b"").decode("utf-8",
                                                        errors="replace")
            elif lowered.endswith(_DOC_EXTENSIONS):
                entry["text"] = (content or b"").decode("utf-8",
                                                        errors="replace")
            else:
                entry["note"] = f"unsupported attachment type {name}"
        except Exception as exc:  # noqa: BLE001 - one bad file never kills the run
            entry["note"] = f"extraction failed: {type(exc).__name__}"
        docs.append(entry)
    return docs


def _looks_like_non_doc(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in _NON_DOC_URL_HINTS)


def _default_url_fetch(url: str) -> tuple[bytes, str]:
    """GET a URL. Returns (content_bytes, content_type). No credentials,
    no POST, short timeout, size cap. Raises on network/HTTP errors."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "StreetSmart-hello-intake/1.0"})
    with urllib.request.urlopen(req,
                                timeout=_DEFAULT_FETCH_TIMEOUT_SECS) as resp:
        content_type = resp.headers.get("Content-Type", "")
        chunks: list[bytes] = []
        remaining = _DEFAULT_FETCH_MAX_BYTES
        while remaining > 0:
            piece = resp.read(min(65536, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        return b"".join(chunks), content_type


def retrieve_link_docs(body: str,
                       url_fetch=None) -> tuple[list[dict], list[str]]:
    """Fetch documents linked directly in the email body.

    Returns (doc_texts, portal_links). doc_texts are {source_url, text}
    for links that resolved to an actual document. portal_links are URLs
    that need a logged-in carrier portal session (HTML login/portal
    pages, 401/403) — the worker must NOT attempt a login; these go to
    the queue entry as portal_link_needs_human for a human to open.

    url_fetch(url) -> (bytes, content_type); inject a fake in tests.
    """
    fetch = url_fetch or _default_url_fetch
    doc_texts: list[dict] = []
    portal_links: list[str] = []
    seen: set[str] = set()
    urls = [u for u in _URL_RE.findall(body or "") if not _looks_like_non_doc(u)]
    for url in urls[:_DEFAULT_MAX_LINKS]:
        if url in seen:
            continue
        seen.add(url)
        try:
            content, content_type = fetch(url)
        except Exception as exc:  # noqa: BLE001 - one bad link never kills the run
            if getattr(exc, "code", None) in (401, 403):
                # Login wall — a human must open this in the carrier portal.
                portal_links.append(url)
            else:
                doc_texts.append({"source_url": url, "text": None,
                                  "note": f"fetch failed: {type(exc).__name__}"})
            continue
        ctype = (content_type or "").lower()
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            text = extract_pdf_text(content)
            doc_texts.append({"source_url": url, "text": text,
                              "note": None if text else
                              "PDF had no extractable text"})
        elif "text/plain" in ctype or url.lower().endswith(".txt"):
            doc_texts.append({"source_url": url,
                              "text": content.decode("utf-8",
                                                     errors="replace")})
        elif "html" in ctype:
            # A page, not a document — almost certainly a carrier portal
            # or login wall. Never attempt a login here.
            portal_links.append(url)
        elif any(code in ctype for code in ("msword", "officedocument")):
            doc_texts.append({"source_url": url, "text": None,
                              "note": "Word document — needs human"})
            portal_links.append(url)
        else:
            doc_texts.append({"source_url": url, "text": None,
                              "note": f"unhandled content type {ctype}"})
    return doc_texts, portal_links


# ---------------------------------------------------------------------------
# True identity from the carrier document (outranks the email body)
# ---------------------------------------------------------------------------

_DOC_INSURED_PATTERNS = [
    re.compile(
        r"(?im)^\s*(?:first\s+)?named\s+insured(?:\(s\))?\s*:\s*(.+?)\s*$"),
    re.compile(r"(?im)^\s*insured\s*:\s*(.+?)\s*$"),
    re.compile(r"(?im)^\s*policyholder\s*:\s*(.+?)\s*$"),
]


def _clean_insured(name: str | None) -> str | None:
    if not name:
        return None
    name = " ".join(name.split()).strip(" -:,.\"'")
    return name or None


def extract_doc_identity(text: str | None) -> dict:
    """Policy numbers + insured names as printed on the carrier doc.

    Returns {"policy_numbers": [...], "insured_names": [...]}. Empty
    lists when the text carries nothing usable — missing is a hold,
    never a guess.
    """
    text = text or ""
    policies: list[str] = []
    for pat in _POLICY_PATTERNS:
        for hit in pat.findall(text):
            hit = hit.strip().upper()
            if hit and hit not in policies:
                policies.append(hit)
    insureds: list[str] = []
    for pat in _DOC_INSURED_PATTERNS:
        for hit in pat.findall(text):
            clean = _clean_insured(hit)
            if clean and clean not in insureds:
                insureds.append(clean)
    return {"policy_numbers": policies, "insured_names": insureds}


# ---------------------------------------------------------------------------
# Policy-number reconciliation ladder
# ---------------------------------------------------------------------------

def normalize_policy_number(value: str | None) -> str:
    """Uppercase alphanumeric core: '008 985 366' == '008985366'."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def policy_digit_core(value: str | None) -> str:
    """Digits only: catches carrier suffix/prefix letters, e.g.
    '008985366C' vs '008985366'."""
    return re.sub(r"\D", "", value or "")


def policy_numbers_match(a: str | None, b: str | None) -> bool:
    """True when two policy numbers are the same modulo formatting and
    carrier letter affixes. Digit cores must be at least 5 digits to
    avoid matching on stubs like '12'."""
    na, nb = normalize_policy_number(a), normalize_policy_number(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    da, db = policy_digit_core(a), policy_digit_core(b)
    return len(da) >= 5 and da == db


# ---------------------------------------------------------------------------
# Fuzzy name matching with tolerance for abbreviations / DBAs
# ---------------------------------------------------------------------------

_ENTITY_SUFFIXES = frozenset({
    "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "pllc", "pa", "lp", "llp", "pllp", "dba",
})

# A name-only signal at/above this is strong; below _NAME_MEDIUM it is
# not a signal at all. Between the two it is weak (queue only).
_NAME_STRONG = 0.95
_NAME_MEDIUM = 0.85


def normalize_name_tokens(name: str | None) -> list[str]:
    """Lowercased tokens minus entity suffixes and leading 'the'."""
    tokens = re.sub(r"[^a-z0-9\s]", " ",
                    (name or "").lower()).split()
    tokens = [t for t in tokens if t not in _ENTITY_SUFFIXES]
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    return tokens


def fuzzy_name_score(a: str | None, b: str | None) -> float:
    """0.0–1.0 similarity for insured names.

    Pure abbreviations/shorts score 1.0 ("Seci" vs "SECI Construction
    Inc" — one token set contains the other). Otherwise the
    difflib ratio on normalized tokens. "ABC Trucking" vs
    "ABD Trucking" scores ~0.92 — deliberately below the strong
    threshold, because one different token means a different company.
    """
    ta, tb = normalize_name_tokens(a), normalize_name_tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    if sa <= sb or sb <= sa:
        return 1.0
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()


def best_name_matches(name: str | None,
                      roster: list[dict]) -> list[tuple[float, dict]]:
    """Roster rows scored against a name, best first. Rows are
    {"applicant_id", "account_name", ...}."""
    scored: list[tuple[float, dict]] = []
    for row in roster or []:
        score = fuzzy_name_score(name, row.get("account_name"))
        if score >= _NAME_MEDIUM:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# The match() ladder
# ---------------------------------------------------------------------------

# Confidence levels. Only "high" auto-files; "medium" and "low" go to the
# human queue with the evidence attached.
CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"


def _roster_emails(row: dict) -> list[str]:
    emails = row.get("emails") or []
    if isinstance(emails, str):
        emails = [emails]
    single = row.get("email")
    if single:
        emails = list(emails) + [single]
    return [str(e).strip().lower() for e in emails if str(e).strip()]


def _roster_policies(row: dict) -> list[str]:
    pols = row.get("policy_numbers") or []
    if isinstance(pols, str):
        pols = [pols]
    return [str(p).strip() for p in pols if str(p).strip()]


def match_hello(*, identity: dict, roster: list[dict] | None,
                alias_store: dict | None = None,
                policy_search=None,
                doc_identities: list[dict] | None = None,
                portal_links: list[str] | None = None) -> dict:
    """Run the match ladder for one genuine hello@ message.

    identity: extract_hello_identity() output (sender_email,
      company_name, policy_numbers, ...). Roster rows:
      {"applicant_id", "account_name", "emails": [...],
       "policy_numbers": [...]}.

    doc_identities: extract_doc_identity() outputs, one per retrieved
      document — doc values are tried BEFORE the email body's claims.
    policy_search: object with search_policy_by_number(number) for the
      EZLynx anchor (hello_triage.resolve_applicant_id semantics).

    Returns a result dict with status "matched" (confidence high) or
    "queue" (confidence medium/low), plus matched_on, strategies_tried,
    evidence, and doc_conflicts.
    """
    identity = identity or {}
    roster = roster or []
    sender_email = str(identity.get("sender_email") or "").strip().lower()
    email_company = (identity.get("company_name") or "").strip() or None
    email_policies = [p for p in (identity.get("policy_numbers") or [])
                      if p]

    doc_policies: list[str] = []
    doc_insureds: list[str] = []
    for doc in (doc_identities or []):
        for p in doc.get("policy_numbers") or []:
            if p and p not in doc_policies:
                doc_policies.append(p)
        for n in doc.get("insured_names") or []:
            if n and n not in doc_insureds:
                doc_insureds.append(n)

    strategies_tried: list[str] = []
    evidence: list[str] = []
    doc_conflicts: list[str] = []

    # The doc outranks the email: note any conflict, match on doc first.
    email_policy_set = {normalize_policy_number(p) for p in email_policies}
    for p in doc_policies:
        if (email_policies
                and normalize_policy_number(p) not in email_policy_set):
            doc_conflicts.append(
                f"doc policy {p} differs from email policy claim(s) "
                f"{', '.join(email_policies)} — doc wins")
    for n in doc_insureds:
        if (email_company and fuzzy_name_score(n, email_company) < _NAME_MEDIUM):
            doc_conflicts.append(
                f"doc insured '{n}' differs from email company "
                f"'{email_company}' — doc wins")
    evidence.extend(doc_conflicts)

    policy_candidates = doc_policies + [p for p in email_policies
                                        if p not in doc_policies]
    name_candidates = doc_insureds + ([email_company] if email_company
                                      and email_company not in doc_insureds
                                      else [])

    def _matched(applicant_id, account_name, matched_on, confidence,
                 note=None):
        if note:
            evidence.append(note)
        return {
            "status": "matched",
            "applicant_id": applicant_id,
            "account_name": account_name,
            "confidence": confidence,
            "matched_on": matched_on,
            "strategies_tried": strategies_tried,
            "evidence": evidence,
            "doc_conflicts": doc_conflicts,
            "portal_links": list(portal_links or []),
            "doc_identity": {"policy_numbers": doc_policies,
                             "insured_names": doc_insureds},
        }

    def _queued(hold_reason, confidence):
        return {
            "status": "queue",
            "applicant_id": None,
            "account_name": None,
            "confidence": confidence,
            "matched_on": None,
            "hold_reason": hold_reason,
            "strategies_tried": strategies_tried,
            "evidence": evidence,
            "doc_conflicts": doc_conflicts,
            "portal_links": list(portal_links or []),
            "doc_identity": {"policy_numbers": doc_policies,
                             "insured_names": doc_insureds},
            "policy_candidates": policy_candidates,
            "name_candidates": name_candidates,
        }

    # -- Step A: sender alias (human-confirmed). Never for vendors. ----
    if sender_email and not sender_is_vendor(sender_email):
        strategies_tried.append("hello_alias")
        aliases = (alias_store or {}).get("aliases") or []
        for a in aliases:
            if str(a.get("sender", "")).strip().lower() == sender_email:
                return _matched(
                    a.get("applicant_id"), a.get("account_name"),
                    "sender_alias", CONF_HIGH,
                    f"human-confirmed alias for {sender_email}")
        evidence.append(f"no hello alias for {sender_email}")
    elif sender_email:
        strategies_tried.append("hello_alias_skipped_vendor")
        evidence.append(
            f"sender {sender_email} is vendor/carrier/system — per-message "
            "matching only, never a fixed alias")

    # -- Step B: report email ------------------------------------------
    if sender_email:
        strategies_tried.append("report_email")
        for row in roster:
            if sender_email in _roster_emails(row):
                return _matched(row.get("applicant_id"),
                                row.get("account_name"),
                                "report_email", CONF_HIGH,
                                f"sender email on roster for "
                                f"{row.get('account_name')}")
        evidence.append(f"sender email {sender_email} not in roster")

    # -- Step C: policy-number ladder ----------------------------------
    if policy_candidates:
        strategies_tried.append("policy_roster")
        for candidate in policy_candidates:
            for row in roster:
                for roster_pol in _roster_policies(row):
                    if policy_numbers_match(candidate, roster_pol):
                        kind = ("policy_exact"
                                if normalize_policy_number(candidate)
                                == normalize_policy_number(roster_pol)
                                else "policy_normalized")
                        return _matched(
                            row.get("applicant_id"),
                            row.get("account_name"), kind, CONF_HIGH,
                            f"policy {candidate} reconciles to roster "
                            f"policy {roster_pol} on "
                            f"{row.get('account_name')}")
        evidence.append(
            f"policy candidate(s) {', '.join(policy_candidates)} not on "
            "any roster row")

        if policy_search is not None:
            strategies_tried.append("policy_anchor_ezlynx")
            for candidate in policy_candidates:
                applicant_id, skip = resolve_applicant_id(policy_search,
                                                          candidate)
                if applicant_id:
                    return _matched(int(applicant_id), None,
                                    "policy_anchor", CONF_HIGH,
                                    f"policy {candidate} anchors to "
                                    f"applicant {applicant_id} in EZLynx")
                evidence.append(skip or
                                f"policy {candidate}: no EZLynx anchor")

    # -- Step D: fuzzy name (scored, never a weak hit) ------------------
    name_hits: list[tuple[float, dict, str]] = []  # (score, row, name)
    if name_candidates:
        strategies_tried.append("name_fuzzy")
        for name in name_candidates:
            for score, row in best_name_matches(name, roster):
                name_hits.append((score, row, name))
        name_hits.sort(key=lambda h: h[0], reverse=True)

    strong = [h for h in name_hits if h[0] >= _NAME_STRONG]
    if len(strong) == 1:
        score, row, name = strong[0]
        note = (f"name '{name}' strongly resembles roster account "
                f"'{row.get('account_name')}' (score {score:.2f})")
        if doc_policies:
            note += ("; doc policy "
                     f"{', '.join(doc_policies)} not found in system — "
                     "human to confirm")
        evidence.append(note)
        # Name-only never auto-files: medium confidence -> human queue.
        return _queued(
            f"name match needs human confirmation: {note}", CONF_MEDIUM)
    if len(strong) > 1:
        names = "; ".join(f"{r.get('account_name')} ({s:.2f})"
                          for s, r, _ in strong[:4])
        evidence.append(f"name matches multiple roster accounts: {names}")
        return _queued(
            f"ambiguous name match across roster accounts: {names}",
            CONF_LOW)

    weak = [h for h in name_hits if _NAME_MEDIUM <= h[0] < _NAME_STRONG]
    if weak:
        names = "; ".join(f"{r.get('account_name')} ({s:.2f})"
                          for s, r, _ in weak[:4])
        evidence.append(f"weak name resemblance only: {names} — not a hit")
        return _queued(
            f"no confident match; weak name resemblance: {names}",
            CONF_LOW)

    if name_candidates:
        evidence.append(
            f"name candidate(s) {', '.join(name_candidates)} resemble no "
            "roster account")

    # -- Nothing held up ------------------------------------------------
    return _queued(
        "no applicant matched: alias, roster email, policy ladder, and "
        "name matching all came up empty",
        CONF_LOW)


# ---------------------------------------------------------------------------
# Queue wiring: matched -> filing path info; queue -> enqueue_unmatched
# ---------------------------------------------------------------------------

def handle_match_result(result: dict, *, sender_email: str, subject: str,
                        request_type: str | None = None,
                        route: str | None = None,
                        gmail_id: str | None = None,
                        message_id: str | None = None,
                        queue_path: str | None = None) -> dict:
    """Wire a match_hello() result into the hello unmatched queue.

    Matched (high confidence): returns {"action": "file", ...} — the
    worker proceeds down the existing HelloIntake routing/filing path
    with the applicant id. This module never files anything itself.

    Queue (medium/low): appends to the hello unmatched queue with the
    doc evidence attached and returns {"action": "queued", "entry": ...}.
    """
    if result.get("status") == "matched":
        return {
            "action": "file",
            "applicant_id": result.get("applicant_id"),
            "account_name": result.get("account_name"),
            "confidence": result.get("confidence"),
            "matched_on": result.get("matched_on"),
            "evidence": result.get("evidence") or [],
        }

    strategies = list(result.get("strategies_tried") or [])
    if result.get("portal_links"):
        strategies.append("portal_link_deferred")
    evidence = list(result.get("evidence") or [])
    for link in result.get("portal_links") or []:
        evidence.append(f"portal_link_needs_human: {link} "
                        "(carrier portal — a human must open it)")

    doc_identity = result.get("doc_identity") or {}
    doc_insureds = doc_identity.get("insured_names") or []
    doc_policies = doc_identity.get("policy_numbers") or []
    entry = enqueue_unmatched(
        sender_email=sender_email,
        subject=subject,
        hold_reason=result.get("hold_reason") or "unmatched",
        request_type=request_type,
        route=route,
        company_name=doc_insureds[0] if doc_insureds else None,
        policy_numbers=doc_policies or result.get("policy_candidates"),
        strategies_tried=strategies,
        gmail_id=gmail_id,
        message_id=message_id,
        evidence=evidence,
        queue_path=queue_path,
    )
    return {"action": "queued", "entry": entry,
            "confidence": result.get("confidence")}
