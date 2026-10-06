"""EOD missed-call digest: post the day's digest to all department Spaces.

Carlo 2026-10-06: the digest goes to ALL THREE departments (Commercial,
Personal, Trucking). This explicitly overrides the phone-alert blackout that
hard-disables Chat alerts for Personal Lines and Trucking — that blackout
applies to real-time phone alerts only, never to this end-of-day digest.

Webhooks come from the server environment (already configured on hermes-poc-01):
  GOOGLE_CHAT_COMMERCIAL_WEBHOOK
  GOOGLE_CHAT_PERSONAL_WEBHOOK
  GOOGLE_CHAT_TRUCKING_WEBHOOK

Fail-open on digest delivery: a digest post failure is recorded in the result
but never fails the report run (the Sheet is the ledger; the digest is a
courtesy copy). Dry-run never posts.
"""

from __future__ import annotations

import os
from typing import Any

from .models import RunSummary, SheetRow

# Department label -> webhook env var. All three always receive the digest.
DEPARTMENT_WEBHOOKS: dict[str, str] = {
    "Commercial": "GOOGLE_CHAT_COMMERCIAL_WEBHOOK",
    "Personal": "GOOGLE_CHAT_PERSONAL_WEBHOOK",
    "Trucking": "GOOGLE_CHAT_TRUCKING_WEBHOOK",
}


def build_digest_text(summaries: list[RunSummary]) -> str:
    """One plain-text digest covering every date in the run."""
    lines = ["*Missed Call Report — end of day*"]
    total = 0
    for s in summaries:
        if s.fail_closed:
            lines.append(f"\n_{s.tab_name}_: report failed closed — no data")
            continue
        rows = s.rows or []
        total += len(rows)
        lines.append(f"\n_{s.tab_name}_ — {len(rows)} missed call(s)")
        for r in rows:
            who = r.department or "unassigned"
            profile = r.profile_text or "No Account"
            extra = f" x{r.call_count}" if r.call_count > 1 else ""
            lines.append(f"• {r.phone_display}{extra} — {profile} ({who})")
    if total == 0:
        lines.append("\nNo missed calls today.")
    lines.append(
        "\nWas addressed? / Updated by are human-only — "
        "update the Sheet directly."
    )
    return "\n".join(lines)


def post_digests(
    summaries: list[RunSummary],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Post the digest to all three department webhooks.

    Returns {department: {"posted": bool, "skipped": str} | ...}.
    Never raises: failures are captured per department.
    """
    from robie_job_engine.staff_jobs_common import post_chat_webhook

    result: dict[str, Any] = {}
    if dry_run:
        text = build_digest_text(summaries)
        for dept in DEPARTMENT_WEBHOOKS:
            result[dept] = {"posted": False, "skipped": "dry-run", "chars": len(text)}
        return result

    text = build_digest_text(summaries)
    for dept, env_var in DEPARTMENT_WEBHOOKS.items():
        url = os.environ.get(env_var, "").strip()
        if not url:
            result[dept] = {"posted": False, "skipped": f"{env_var} not set"}
            continue
        try:
            ok = post_chat_webhook(text, webhook_url=url)
            result[dept] = {"posted": bool(ok)}
        except Exception as exc:  # fail-open: never break the report run
            result[dept] = {"posted": False, "skipped": f"post failed: {exc}"}
    return result
