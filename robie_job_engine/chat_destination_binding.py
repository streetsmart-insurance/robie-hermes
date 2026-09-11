"""Slice 1 — bind a real destination to the Job before verification runs.

Why this exists
---------------
A freeform Chat job's payload is a prompt. Job 0e76fc96's payload was
``{gmail_message_id, prompt, worker}`` — no applicant, no policy. So when
the verifier is told "the payload beats the worker's claim", there is
nothing in the payload to beat it with, and ``intended_destination_identity``
has nothing to cross-check. Every freeform job then lands BLOCKED with
"nothing to re-read", which is honest but useless.

This module derives what it can from evidence and is explicit about what it
cannot.

What is derivable, and what is not
----------------------------------
``playwright_exec`` rows are machine-recorded: the driver wrote them, not
the agent's narration. EZLynx URLs inside them carry the **applicant id**
(``/web/account/{id}``, ``/applicantportal/policy/actions/edit/{id}``, …) —
see ``ezlynx_write_scope.applicant_id_from_ezlynx_url``, reused here rather
than re-implemented.

EZLynx URLs do **not** carry the carrier's policy number. The numeric id in
``/applicantportal/policy/{n}/formentry/index/{n}`` is an internal policy
id, not the policy number the verifier searches by. So:

    applicant_id   DERIVED from playwright_exec  -> trustworthy
    policy_number  CLAIMED by the worker         -> untrusted

That asymmetry is the point, not a shortcoming. The untrusted half is
checked against the trusted half: the verifier looks the claimed policy
number up and requires it to exist **on the derived applicant**. A worker
that invents a policy number fails, and a worker that names a real policy
belonging to someone else fails too.

Refusals (CR-6: one job, one applicant, no fan-out)
---------------------------------------------------
Two or more distinct applicants in one job's exec log is not a destination
this module will guess at. It refuses and says so, rather than picking one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .ezlynx_write_scope import applicant_id_from_ezlynx_url

# Any app.ezlynx.com or app.uatezlynx.com URL, wherever it appears in a recorded exec row.
_EZLYNX_URL = re.compile(r"https?://app\.ezlynx\.com/[^\s\"'\\<>)\]]+", re.IGNORECASE)

DERIVED = "derived_from_playwright_exec"
CLAIMED = "claimed_by_worker"


@dataclass
class DestinationBinding:
    """What we are willing to say the job touched, and on whose authority."""

    applicant_id: str = ""
    policy_number: str = ""
    discussion_title: str = ""
    document_names: list[str] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)
    applicants_seen: list[str] = field(default_factory=list)
    urls_seen: int = 0
    refusal: str | None = None

    @property
    def bindable(self) -> bool:
        return self.refusal is None and bool(self.applicant_id)

    def payload_patch(self) -> dict[str, str]:
        """Only ever the identifiers. Never prose, never the worker's summary."""
        patch: dict[str, str] = {}
        if self.applicant_id:
            patch["applicant_id"] = self.applicant_id
        if self.policy_number:
            patch["policy_number"] = self.policy_number
        return patch

    def checkpoint(self, job_id: str) -> dict[str, Any]:
        """The ``action`` checkpoint chat_guard looks for before verifying."""
        return {
            "action": "ezlynx.destination_mutation",
            "destination": {
                "applicant_id": self.applicant_id,
                "policy_number": self.policy_number,
                "discussion_title": self.discussion_title,
                "document_names": list(self.document_names),
            },
            "detail": {
                "job_id": job_id,
                "provenance": dict(self.provenance),
                "applicants_seen": list(self.applicants_seen),
                "playwright_urls_seen": self.urls_seen,
                # Loud, because a reader three months from now must not
                # mistake this row for evidence.
                "this_is_a_claim_not_evidence": True,
            },
        }


def extract_ezlynx_urls(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Pull every app.ezlynx.com / app.uatezlynx.com URL out of recorded exec rows.

    Scans ``code_preview`` and ``result_json`` because the driver records
    the navigation in one and the outcome in the other, and which one holds
    the URL varies by tool.
    """
    urls: list[str] = []
    for row in rows:
        for key in ("code_preview", "result_json"):
            blob = row.get(key)
            if blob is None:
                continue
            if not isinstance(blob, str):
                try:
                    blob = json.dumps(blob)
                except Exception:
                    blob = str(blob)
            urls.extend(_EZLYNX_URL.findall(blob))
    return urls


def derive_destination(
    exec_rows: Iterable[dict[str, Any]],
    claimed: dict[str, Any] | None = None,
) -> DestinationBinding:
    """Build a binding from recorded browser activity plus the worker's claim."""
    claimed = dict(claimed or {})
    urls = extract_ezlynx_urls(exec_rows)

    applicants: list[str] = []
    for url in urls:
        found = applicant_id_from_ezlynx_url(url)
        if found and found not in applicants:
            applicants.append(found)

    binding = DestinationBinding(
        applicants_seen=applicants,
        urls_seen=len(urls),
        discussion_title=str(claimed.get("discussion_title") or "").strip(),
        document_names=[
            str(n).strip() for n in (claimed.get("document_names") or []) if str(n).strip()
        ],
    )

    # The policy number is never in the URL. It is the worker's word, and
    # the verifier is what tests it — against the derived applicant.
    binding.policy_number = str(claimed.get("policy_number") or "").strip()
    if binding.policy_number:
        binding.provenance["policy_number"] = CLAIMED

    if len(applicants) > 1:
        binding.refusal = (
            "playwright_exec shows more than one EZLynx applicant "
            f"({', '.join(applicants[:5])}); one job, one applicant — refusing "
            "to guess which one this job mutated"
        )
        return binding

    if applicants:
        binding.applicant_id = applicants[0]
        binding.provenance["applicant_id"] = DERIVED
        claim_applicant = str(claimed.get("applicant_id") or "").strip()
        if claim_applicant and claim_applicant != binding.applicant_id:
            binding.refusal = (
                f"worker claimed applicant {claim_applicant} but the recorded "
                f"browser activity only touched {binding.applicant_id}"
            )
        return binding

    # Nothing machine-recorded. Fall back to the claim, clearly labelled, so
    # the verifier can still run — it will independently confirm or refuse.
    claim_applicant = str(claimed.get("applicant_id") or "").strip()
    if claim_applicant:
        binding.applicant_id = claim_applicant
        binding.provenance["applicant_id"] = CLAIMED
    else:
        binding.refusal = (
            "no EZLynx applicant in playwright_exec and none claimed; "
            "there is no destination to verify"
        )
    return binding


# ---------------------------------------------------------------------- #
# store-facing helpers
# ---------------------------------------------------------------------- #


def read_exec_rows(store: Any, job_id: str) -> list[dict[str, Any]]:
    """Read this job's recorded Playwright activity. Read-only."""
    reader = getattr(store, "playwright_exec_rows", None)
    if callable(reader):
        return list(reader(job_id) or [])
    conn = getattr(store, "_conn", None) or getattr(store, "conn", None)
    if conn is None:
        return []
    cur = conn.execute(
        "SELECT tool, status, code_preview, result_json, created_at"
        " FROM playwright_exec WHERE job_id = ? ORDER BY id",
        (job_id,),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def bind_destination_for_job(
    store: Any, job: dict[str, Any], claimed: dict[str, Any] | None = None
) -> DestinationBinding:
    """Derive, then write the checkpoint and patch the Job payload.

    Writes nothing when the binding is refused — a refused binding must
    leave the job exactly as verifiable (or not) as it already was, so a
    later run can try again with better evidence.
    """
    job_id = job["id"]
    binding = derive_destination(read_exec_rows(store, job_id), claimed)
    if not binding.bindable:
        return binding

    store.checkpoint(job_id, "action", binding.checkpoint(job_id))

    patch = binding.payload_patch()
    if patch:
        payload = dict(job.get("payload") or {})
        # Never overwrite an identifier the Job was already bound to.
        for key, value in patch.items():
            payload.setdefault(key, value)
        updater = getattr(store, "update_payload", None)
        if callable(updater):
            updater(job_id, payload)
        job["payload"] = payload
    return binding
