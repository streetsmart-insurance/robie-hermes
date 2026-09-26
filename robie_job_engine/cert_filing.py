"""Certificates filing: verify the client, then write via EZLynx APIs.

Every write path runs the fail-closed ``verify_filing_target`` gate
first — the three checks Carlo set after the 2026-09-25 Rivera misfile:

1. The worker's matched record (insured name + policy number) agrees
   with what the email itself says.
2. The email's policy number is anchored to the applicant in EZLynx.
3. The discussion being filed into belongs to that applicant.

Any failure raises ``FilingTargetMismatch`` and NOTHING is written.
Post-write, every note and document is read back and asserted before
the result is reported.

Pluggable boundaries (duck-typed, fakeable in tests):

- ``applicant_lookup``: ``find_applicant(policy_number) ->
  applicant | None``. Applicant is a dict with ``applicant_id``,
  ``insured_name``, ``policy_numbers`` (list).
- ``discussion_client``: ``get_discussions(applicant_id)``,
  ``get_discussion(discussion_id)``, ``append_note(discussion_id, body)``
  — matches ``DiscussionApiClient``; may also be the certificate-filing
  helper's client.
- ``document_client``: ``upload(applicant_id, document_name, file_bytes,
  filename=...) -> document_id`` and ``search(applicant_id) ->
  list[dict]`` where each row carries the uploaded document's id/name.

The module never creates an applicant, never creates a discussion, and
never fires the review-task Zap itself — it builds the verified payload
and returns it for the runtime to fire.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .cert_intake import CertEmail, RequestFacts, render_email_source
from .certificate_filing import file_certificate_notes
from .ezlynx_discussions import reject_phone_numbers


class FilingTargetMismatch(RuntimeError):
    """A pre-write identity check failed. Nothing was written."""


class HoldForHuman(RuntimeError):
    """The request is real but cannot be filed safely. Human reviews."""


def _norm_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _norm_policy(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").upper())


def verify_filing_target(
    facts: RequestFacts,
    applicant: dict[str, Any],
    discussion: dict[str, Any],
) -> dict[str, Any]:
    """Run the triple check. Returns the verified target or raises.

    1. ``facts`` (what the email says) agrees with ``applicant``
       (what EZLynx says) on insured name and policy number.
    2. The policy number is anchored to the applicant's own policy list.
    3. The discussion belongs to the applicant.
    """
    applicant_id = str(applicant.get("applicant_id") or "").strip()
    if not applicant_id:
        raise FilingTargetMismatch("applicant has no applicant_id")

    # Check 1: email vs matched record.
    email_insured = _norm_name(facts.insured_name)
    record_insured = _norm_name(applicant.get("insured_name"))
    if facts.insured_name and record_insured:
        if email_insured != record_insured and email_insured not in record_insured and record_insured not in email_insured:
            raise FilingTargetMismatch(
                f"insured mismatch: email says {facts.insured_name!r}, "
                f"record says {applicant.get('insured_name')!r}"
            )
    elif not record_insured:
        raise FilingTargetMismatch("matched record has no insured name to agree with")

    email_policies = [_norm_policy(p) for p in facts.policy_numbers if _norm_policy(p)]
    record_policies = [
        _norm_policy(p)
        for p in (applicant.get("policy_numbers") or [])
        if _norm_policy(p)
    ]
    if not email_policies:
        raise FilingTargetMismatch("email carries no policy number to anchor")
    if not any(p in record_policies for p in email_policies):
        raise FilingTargetMismatch(
            f"policy {facts.policy_numbers[0]!r} is not on applicant {applicant_id}"
        )

    # Check 2 is the anchoring above: the policy number matched a policy
    # on THIS applicant's record, not merely a policy-shaped string.

    # Check 3: the discussion belongs to the applicant.
    disc_applicant = str(
        discussion.get("applicant_id") or discussion.get("applicantId") or ""
    ).strip()
    if disc_applicant and disc_applicant != applicant_id:
        raise FilingTargetMismatch(
            f"discussion {discussion.get('discussion_id')} belongs to "
            f"applicant {disc_applicant}, not {applicant_id}"
        )
    return {
        "applicant_id": applicant_id,
        "policy_number": facts.policy_numbers[0],
        "discussion_id": str(discussion.get("discussion_id") or "").strip(),
    }


def resolve_discussion(
    discussion_client: Any, applicant_id: str, discussion_id: str | None = None
) -> dict[str, Any]:
    """Resolve the existing discussion to file into.

    Never creates one. With an explicit discussion id, asserts it exists
    and belongs to the applicant. Without one, prefers an existing
    certificate-named discussion; refuses zero or ambiguous matches.
    """
    discussions = discussion_client.get_discussions(applicant_id) or []
    if discussion_id:
        for d in discussions:
            if str(d.get("discussion_id")) == str(discussion_id):
                return dict(d, applicant_id=applicant_id)
        raise FilingTargetMismatch(
            f"discussion {discussion_id} not found on applicant {applicant_id}"
        )
    cert_named = [
        d
        for d in discussions
        if re.search(r"(?i)cert", str(d.get("title") or ""))
    ]
    if len(cert_named) == 1:
        return dict(cert_named[0], applicant_id=applicant_id)
    if not cert_named:
        raise HoldForHuman(
            f"applicant {applicant_id} has no certificate discussion; "
            "human to pick the filing discussion"
        )
    raise HoldForHuman(
        f"applicant {applicant_id} has {len(cert_named)} certificate "
        "discussions; human to pick the filing discussion"
    )


def extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """Best-effort PDF text via pdftotext. None when unreadable."""
    if not pdf_bytes:
        return None
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=pdf_bytes,
            capture_output=True,
            timeout=60,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace").strip()
    return text or None


def _document_name(email: CertEmail, filename: str) -> str:
    # The gmail id in the name makes re-runs idempotent: the exact same
    # delivery is never uploaded twice.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "attachment")
    if "." not in safe:
        safe += ".pdf"
    stamp = (email.date or "nodate")[:10]
    return f"Certificates-{stamp}-{email.gmail_id}-{safe}"[:150]


def _upload_once(
    document_client: Any,
    applicant_id: str,
    name: str,
    content: bytes,
    *,
    filename: str,
    content_hash: str,
) -> dict[str, Any]:
    """Upload unless a document with this exact name already exists.

    The name carries the Gmail id, so "already exists" means the exact
    same delivery was already filed — never a coincidence. The search
    is best-effort: if it errors, the durable dedupe store upstream
    remains the authoritative guard and we proceed.
    """
    try:
        rows = document_client.search(applicant_id) or []
    except Exception:
        rows = []
    for row in rows:
        for key in ("name", "documentName", "document_name", "fileName"):
            if str(row.get(key) or "") == name:
                return {
                    "document_id": str(
                        row.get("document_id")
                        or row.get("documentId")
                        or row.get("id")
                    ),
                    "name": name,
                    "sha256": content_hash,
                    "size": len(content),
                    "duplicate_skipped": True,
                }
    document_id = document_client.upload(
        applicant_id, name, content, filename=filename
    )
    try:
        rows = document_client.search(applicant_id) or []
    except Exception:
        rows = []
    hit = any(
        str(r.get("document_id") or r.get("documentId") or r.get("id"))
        == str(document_id)
        for r in rows
    )
    if not hit:
        raise RuntimeError(
            f"document {document_id} uploaded but not found on read-back"
        )
    return {
        "document_id": str(document_id),
        "name": name,
        "sha256": content_hash,
        "size": len(content),
        "duplicate_skipped": False,
    }


@dataclass
class FilingResult:
    applicant_id: str
    discussion_id: str
    documents: list[dict[str, Any]] = field(default_factory=list)
    note: dict[str, Any] = field(default_factory=dict)
    task_payload: dict[str, Any] = field(default_factory=dict)
    held: str | None = None


def file_certificate_request(
    *,
    email: CertEmail,
    facts: RequestFacts,
    applicant_lookup: Any,
    discussion_client: Any,
    document_client: Any,
    note_text: str,
    discussion_id: str | None = None,
    assignee: str = "",
    due_date: str = "",
    task_source: str = "certificates-intake",
    note_guard_cutoff: datetime | None = None,
) -> FilingResult:
    """Verify, upload documents, file the note, build the task payload.

    Raises ``FilingTargetMismatch`` (nothing written) or ``HoldForHuman``
    (nothing written). On success every write is read back and asserted.

    ``note_guard_cutoff``: pass the timestamp this email was first filed
    at (from the durable intake store). The filing tool then refuses to
    append a second note when the discussion was modified at/after that
    instant — retries never duplicate the note. Documents are deduped by
    their gmail-id-bearing names on every run.
    """
    if facts.pdf_unreadable:
        raise HoldForHuman("a PDF attachment could not be read as text")
    if not facts.policy_numbers:
        raise HoldForHuman("no policy number in the email or its PDFs")

    applicant = applicant_lookup.find_applicant(facts.policy_numbers[0])
    if not applicant:
        raise HoldForHuman(
            f"policy {facts.policy_numbers[0]} matched no EZLynx applicant"
        )
    applicant_id = str(applicant.get("applicant_id") or "").strip()

    discussion = resolve_discussion(discussion_client, applicant_id, discussion_id)
    target = verify_filing_target(facts, applicant, discussion)

    # Documents first: the email itself, then its attachments. Each is
    # read back and asserted before the next step runs.
    email_source = render_email_source(email).encode("utf-8")
    filed: list[dict[str, Any]] = [
        _upload_once(
            document_client,
            applicant_id,
            _document_name(email, "email.txt"),
            email_source,
            filename="email.txt",
            content_hash=hashlib.sha256(email_source).hexdigest(),
        )
    ]
    for att in email.attachments:
        name = _document_name(email, att.filename)
        filed.append(
            _upload_once(
                document_client,
                applicant_id,
                name,
                att.content,
                filename=att.filename,
                content_hash=att.sha256,
            )
        )

    # The note, through the gated filing tool (it re-verifies read-back).
    # skip_if_modified_after makes retries note-idempotent.
    safe_note = reject_phone_numbers(note_text)
    filing = file_certificate_notes(
        discussion_client,
        [
            {
                "applicant_id": applicant_id,
                "discussion_id": target["discussion_id"],
                "note": safe_note,
                "source": f"certificates-intake:{email.gmail_id}",
            }
        ],
        skip_if_modified_after=note_guard_cutoff,
    )
    item = filing[0]
    if item["status"] == "skipped":
        note_result: dict[str, Any] = {
            "status": "skipped_guard",
            "detail": item.get("reason"),
        }
    else:
        if item["status"] not in ("filed", "filed_unverified"):
            raise RuntimeError(
                f"note filing failed: {item['status']}: {item.get('detail')}"
            )
        if item["status"] == "filed_unverified":
            raise RuntimeError("note filed but read-back verification failed")
        note_result = {"status": item["status"], "detail": item.get("detail")}

    task_payload = build_task_payload(
        applicant_id=applicant_id,
        email=email,
        facts=facts,
        note=safe_note,
        assignee=assignee,
        due_date=due_date,
        source=task_source,
    )
    return FilingResult(
        applicant_id=applicant_id,
        discussion_id=target["discussion_id"],
        documents=filed,
        note=note_result,
        task_payload=task_payload,
    )


def build_task_payload(
    *,
    applicant_id: str,
    email: CertEmail,
    facts: RequestFacts,
    note: str,
    assignee: str,
    due_date: str,
    source: str = "certificates-intake",
) -> dict[str, Any]:
    """Build (not fire) the Zapier review-task payload.

    ``assignee`` must be the reviewer's EZLynx login username and
    ``due_date`` an ISO date — both required, never defaulted, because
    the Zap rejects bad assignees and Carlo requires a due date.
    """
    applicant_id = str(applicant_id or "").strip()
    assignee = str(assignee or "").strip()
    due_date = str(due_date or "").strip()
    if not applicant_id:
        raise HoldForHuman("task payload needs a verified applicant_id")
    if not assignee:
        raise HoldForHuman("task payload needs the reviewer's EZLynx login username")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_date or ""):
        raise HoldForHuman("task payload needs an ISO due_date (YYYY-MM-DD)")
    if not note or not note.strip():
        raise HoldForHuman("task payload needs real note content, never a placeholder")
    insured = facts.insured_name or "insured not identified"
    return {
        "applicant_id": applicant_id,
        "task_title": f"Certificate review: {insured}",
        "assignee": assignee,
        "source": source,
        "email_subject": email.subject,
        "due_date": due_date,
        "note_text": note,
    }
