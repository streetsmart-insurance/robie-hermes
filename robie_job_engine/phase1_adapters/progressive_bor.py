"""Progressive BOR-takeover renewal check (Phase 1, read-only, API only).

NO portal pull. Progressive manual-renewal rows that StreetSmart set up
by hand are broker-of-record takeovers: at renewal the policy SHOULD
download from Progressive automatically. The worker's first step is to
check whether the renewal downloaded into EZLynx.

Method: EZLynx PolicyApi ``search_policy_by_number`` (read-only, box
venv). KNOWN LIMITATION (repo standing architecture): the API has no
applicant-scoped policy list, so a renewal term under a DIFFERENT policy
number is NOT API-discoverable. A miss here is never proof the renewal
didn't download — it stays a human check in the EZLynx UI.

Runtime: API (no browser at all).
Allowed actions: {"api_read"} only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AdapterSpec, DownloadResult, PolicyRef

ADAPTER = AdapterSpec(
    carrier_id="progressive_bor",
    carrier_name="Progressive Insurance",
    portal_url="",  # no portal — API check only
    runtime="api",
    runtime_reason=(
        "BOR-takeover rule: check via EZLynx PolicyApi whether the renewal "
        "downloaded. No browser, no portal login — api_read only."
    ),
    username_env="",
    password_env="",
    doc_kind="bor_download_check",
    allowed_actions=frozenset({"api_read"}),
    notes=(
        "KNOWN LIMITATION: a renewal term under a different policy number "
        "is not API-discoverable; 'unknown' always stays a human check in "
        "the EZLynx UI, never an automatic 'not downloaded'.",
    ),
)


def _clean_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("1900-01-01"):
        return ""
    return text.split("T")[0]


def check(
    policy: PolicyRef,
    dest_dir: Path,
    ezlynx_client: Any,
    accessor=None,
) -> DownloadResult:
    """Check via PolicyApi whether a renewal term is visible. Read-only."""
    _ = (dest_dir, accessor)  # no files, no credentials for the API path
    try:
        res = ezlynx_client.search_policy_by_number(policy.policy_number)
    except Exception as exc:  # noqa: BLE001 - record, never raise past the runner
        return DownloadResult(
            ok=False,
            detail=f"progressive BOR API check failed: {type(exc).__name__}",
        )
    data = res.get("data") or {}
    results = data.get("results") if isinstance(data, dict) else data
    rec = None
    if isinstance(results, list):
        for row in results:
            if str(row.get("policyNumber") or "").strip() == policy.policy_number:
                rec = row
                break
        if rec is None and results:
            rec = results[0]
    if not rec:
        return DownloadResult(
            ok=False,
            detail=f"policy {policy.policy_number} not found via PolicyApi",
            extra={"renewal_downloaded": "unknown"},
        )
    status = rec.get("policyStatus") or ""
    expiration = _clean_date(rec.get("expirationDate"))
    if str(status).strip().lower() != "active":
        return DownloadResult(
            ok=True,
            detail=(
                f"policy {policy.policy_number} is {status} in EZLynx — "
                "no renewal expected (dead-policy gate)"
            ),
            extra={
                "policy_status": status,
                "expiration_date": expiration,
                "renewal_downloaded": "not_applicable",
            },
        )
    return DownloadResult(
        ok=True,
        detail=(
            f"policy {policy.policy_number} is Active (exp {expiration}); "
            "renewal term under a different number is not API-discoverable "
            "— human check in EZLynx UI required"
        ),
        extra={
            "policy_status": status,
            "expiration_date": expiration,
            "renewal_downloaded": "unknown",
        },
    )
