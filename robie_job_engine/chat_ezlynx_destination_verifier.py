"""Destination verifier for freeform Chat jobs that mutate EZLynx.

Closes the Bond gap: job 85f5eae0 ended UNVERIFIED ("no structured
destination action checkpoint") even though Playwright really did create
policy 73834086 on applicant 194066748, post a titled Bond discussion, and
upload a PDF. 74 playwright_exec rows, 0 verification_evidence.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
----------------------------------------
ROBIE posts a discussion note after it mutates EZLynx. That note is a
receipt for humans. It is NOT evidence. If a verifier marked evidence
authoritative because it found a note, ROBIE would be confirming its own
prose -- a job could post a note about work it never did and self-verify.
That is the exact false-success mode the job contract exists to prevent.

So: the POLICY RECORD is the proof. Documents are proof. The note is
recorded in ``observed`` as a receipt and can never make evidence
authoritative on its own.

Read-only by construction. Every read is a fresh authenticated API call --
never a DOM snapshot, never the worker's report of what it did.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from .models import VerificationEvidence, VerificationResult


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


class EzlynxDestinationReadPort(Protocol):
    """Fresh authenticated reads of EZLynx destination state.

    Backed by the existing client in
    ``renewal-automation-system/src/ezlynx/api_client.py``:

    * ``policy_by_number``  -> GET /PolicyApi/policy/v1/search?PolicyNumber=
                               (OAuth2 gateway, scope PolicyApi)
    * ``documents_for_applicant``
                            -> GET /ezlynxapi/api/documentlibrary/list/
                               {applicant_id}/{pageIndex}/{pageSize}/{policyId}
                               (Classic REST). Rows carry ``Description``
                               (the filename) and ``PolicyId``.
    * ``discussions_for_applicant`` -> receipt only, never authoritative.

    A concrete adapter over the real client lives in
    ``ezlynx_api_read_port.EzlynxApiClientReadPort``.
    """

    def policy_by_number(self, policy_number: str) -> dict[str, Any]: ...

    def documents_for_applicant(
        self, applicant_id: str, policy_id: int = 0
    ) -> list[dict[str, Any]]:
        """NORMALIZED rows: [{"name": str, "policy_id": Any}].

        Normalizing is the adapter's job, not the verifier's. api_client's
        own docstring warns the raw envelope varies by tenant ("do not
        assume Records/DocumentList or DocumentName/PolicyNumber"), which is
        why ``extract_document_records`` / ``document_display_fields``
        exist. Use them in the adapter so this class never guesses a shape.
        """

    def discussions_for_applicant(self, applicant_id: str) -> list[dict[str, Any]]:
        """NORMALIZED rows: [{"title": str}]. Receipt only, never evidence."""


class HermesChatEzlynxDestinationVerifier:
    """Re-read EZLynx and decide whether the claimed mutation really landed.

    ``action["destination"]`` is the worker's CLAIM about what it changed.
    It is never trusted as evidence. Where the bound job payload also names
    a destination, the payload wins -- a worker cannot redirect verification
    at an applicant the job was not bound to. (The engine independently
    re-checks this via ``intended_destination_identity`` /
    ``destination_identity_missing``; this is the same rule applied early so
    the mismatch is named rather than merely failing.)
    """

    def __init__(self, port: EzlynxDestinationReadPort):
        self._port = port

    # ------------------------------------------------------------------ #

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        claimed = dict((action or {}).get("destination") or {})
        payload = dict(job.get("payload") or {})

        # Payload beats claim. A worker may not choose its own subject.
        applicant_id = str(payload.get("applicant_id") or claimed.get("applicant_id") or "").strip()
        policy_number = str(payload.get("policy_number") or claimed.get("policy_number") or "").strip()
        expected_documents = [
            str(name).strip()
            for name in (claimed.get("document_names") or [])
            if str(name).strip()
        ]
        discussion_title = str(claimed.get("discussion_title") or "").strip()

        expected = {
            "applicant_id": applicant_id,
            "policy_number": policy_number,
            "document_names": expected_documents,
            "discussion_title": discussion_title,
        }

        bound_applicant = str(payload.get("applicant_id") or "").strip()
        claim_applicant = str(claimed.get("applicant_id") or "").strip()
        if bound_applicant and claim_applicant and bound_applicant != claim_applicant:
            return self._fail(
                expected,
                {"applicant_id_claimed": claim_applicant, "applicant_id_bound": bound_applicant},
                "worker claimed a different applicant than the Job is bound to",
                locator=None,
                retryable=False,
            )

        if not policy_number:
            return self._fail(
                expected,
                {},
                "no policy number on the Job or the action checkpoint; nothing to re-read",
                locator=None,
                retryable=False,
            )

        # ---- 1. THE PROOF: does the policy actually exist? --------------
        try:
            policy_result = self._port.policy_by_number(policy_number)
        except Exception as exc:
            return self._fail(
                expected,
                {"error": f"{type(exc).__name__}: {exc}"},
                "EZLynx PolicyApi read-back failed; destination state unknown",
                locator=policy_number,
                retryable=True,
            )

        matches = _policy_matches(policy_result, policy_number, applicant_id)
        observed: dict[str, Any] = {
            "policy_number": policy_number,
            "policy_found": bool(matches),
            "policy_records": matches[:3],
            "read_method": "PolicyApi/policy/v1/search",
        }

        if not matches:
            return self._fail(
                expected,
                observed,
                f"policy {policy_number} is not present in EZLynx on re-read",
                locator=policy_number,
                retryable=False,
                # A real absence is authoritative: we looked and it is not there.
                authoritative=True,
            )

        # ---- 2. Documents, if the claim named any -----------------------
        documents_ok = True
        if expected_documents:
            try:
                rows = self._port.documents_for_applicant(applicant_id) if applicant_id else []
            except Exception as exc:
                observed["documents_error"] = f"{type(exc).__name__}: {exc}"
                return self._fail(
                    expected,
                    observed,
                    "document library read failed; cannot confirm the uploaded document",
                    locator=policy_number,
                    retryable=True,
                )
            seen = [str(r.get("name") or "") for r in rows]
            observed["documents_seen"] = seen[:25]
            missing = [
                name for name in expected_documents
                if not any(_norm(name) in _norm(s) for s in seen)
            ]
            observed["documents_missing"] = missing
            documents_ok = not missing

        # ---- 3. The note. RECEIPT ONLY. Never gates authoritative. ------
        if applicant_id:
            try:
                notes = self._port.discussions_for_applicant(applicant_id)
                titles = [str(n.get("title") or "") for n in notes]
                observed["discussion_titles_seen"] = titles[:25]
                same_title = [t for t in titles if _norm(discussion_title) == _norm(t)]
                observed["discussion_receipt_present"] = bool(
                    discussion_title and same_title
                )
                # Standing rule: append to the EXISTING titled discussion.
                # Never create a second "Bond". Surfaced, not fatal -- the
                # note is a receipt, so a duplicate is a housekeeping defect
                # for a human, not a reason to call real work unverified.
                if discussion_title and len(same_title) > 1:
                    observed["discussion_duplicate_titles"] = len(same_title)
            except Exception as exc:
                observed["discussion_receipt_present"] = None
                observed["discussion_error"] = f"{type(exc).__name__}: {exc}"
        observed["note_is_receipt_not_evidence"] = True

        verified = bool(matches) and documents_ok
        error = None
        if not documents_ok:
            error = (
                "policy confirmed but expected document(s) not found in the "
                f"applicant document library: {observed.get('documents_missing')}"
            )

        evidence = VerificationEvidence(
            method="EZLYNX_API_DESTINATION_READBACK",
            source="ezlynx-policyapi+documentlibrary",
            expected=expected,
            observed=observed,
            # Authoritative because every field above came from a fresh
            # authenticated API read of destination state. The discussion
            # note contributed nothing to this decision.
            authoritative=True,
            captured_at=_utc_now(),
            locator=policy_number,
        )
        return VerificationResult(verified, evidence, retryable=False, error=error)

    # ------------------------------------------------------------------ #

    def _fail(
        self,
        expected: dict[str, Any],
        observed: dict[str, Any],
        error: str,
        *,
        locator: str | None,
        retryable: bool,
        authoritative: bool = False,
    ) -> VerificationResult:
        evidence = VerificationEvidence(
            method="EZLYNX_API_DESTINATION_READBACK",
            source="ezlynx-policyapi+documentlibrary",
            expected=expected,
            observed=observed,
            authoritative=authoritative,
            captured_at=_utc_now(),
            locator=locator,
        )
        return VerificationResult(False, evidence, retryable=retryable, error=error)


def _policy_matches(
    result: Any, policy_number: str, applicant_id: str
) -> list[dict[str, Any]]:
    """Pull policy rows out of the PolicyApi envelope and match them.

    The client wraps responses as {"status": "success", "data": <raw>}; the
    raw shape varies by tenant, so accept the common containers rather than
    assuming one. A row counts only if the policy number matches exactly
    (case-insensitive) and, where the row carries an applicant, that matches
    the bound applicant too.
    """
    if isinstance(result, dict) and result.get("status") == "error":
        return []
    data = result.get("data") if isinstance(result, dict) else result
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("Policies", "policies", "Results", "results", "Items", "items", "Data"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
        else:
            rows = [data]

    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = (
            row.get("PolicyNumber") or row.get("policyNumber") or row.get("policy_number")
        )
        if _norm(number) != _norm(policy_number):
            continue
        row_applicant = (
            row.get("ApplicantId") or row.get("applicantId") or row.get("applicant_id")
        )
        if applicant_id and row_applicant and _norm(row_applicant) != _norm(applicant_id):
            continue
        out.append(row)
    return out
