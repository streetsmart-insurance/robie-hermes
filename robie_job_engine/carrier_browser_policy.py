"""Per-carrier browser runtime policy.

Permanent rule (2026-09-18, Carlo): Hartford's website is unreachable from
the ROBIE servers. Proven by curl probes on hermes-poc-01 on 2026-09-18
(Ralph), independently re-proven by Dusty the same day with the exact probe:
  - direct:                curl 92 HTTP/2 INTERNAL_ERROR
  - via residential proxy 9.142.10.166:5822: curl 92 HTTP/2 INTERNAL_ERROR
  - via proxy forced HTTP/1.1: curl 28 timeout, 0 bytes in 25s
The sandbox browser reaches Hartford fine (full EBC session 2026-09-17/18).

Consequence: a Hartford portal / EBC Playwright job must never start on a
hermes-* server. It fails closed here with a plain-English reason telling the
operator to run it in the sandbox browser instead (or get the documents by
email / IVANS). Do not debug proxy or stealth settings for Hartford on hermes
— that path is dead. Other carriers keep the designed stealth+proxy path
unchanged; this module refuses nothing for them.

The gate is host-scoped on purpose: it only fires on hosts named hermes-*
(the ROBIE server fleet). Dev machines, CI runners, and the sandbox browser
path are unaffected. If Hartford reachability from the servers is ever
re-proven end to end, update this module — it is the single place that owns
the rule.
"""

from __future__ import annotations

import socket
from typing import Any


# Any of these in the job means the browser target is Hartford's site.
HARTFORD_SITE_MARKERS = ("thehartford.com",)

# A bare "hartford" name mention is NOT enough on its own (same principle as
# the 264a708f Ascend lesson in action_gate.py: name-only mentions must not
# fail-close real work). It only counts alongside a portal word.
HARTFORD_NAME_MARKER = "hartford"
HARTFORD_PORTAL_WORDS = ("ebc", "portal", "thehartford")

# ROBIE server fleet. The rule is proven for hermes-poc-01; hermes-test-01 is
# unverified, so it fails closed too rather than burning time re-proving it.
HERMES_HOST_PREFIX = "hermes-"

HARTFORD_REFUSAL = (
    "Hartford's site does not load from this server (proven 2026-09-18: direct "
    "and proxy connections both fail). Do not run Hartford portal / EBC browser "
    "jobs here. Run them in the sandbox browser instead, or get the documents "
    "by email or IVANS. Do not debug proxy or stealth settings for Hartford on "
    "this server."
)


def is_hermes_server_host(hostname: str | None = None) -> bool:
    """True when this code is running on a ROBIE server (hermes-* fleet)."""
    host = hostname if hostname is not None else socket.gethostname()
    return str(host or "").strip().lower().startswith(HERMES_HOST_PREFIX)


def hartford_target_in_blob(blob: str) -> bool:
    """True when the normalized job text targets Hartford's portal/site."""
    text = str(blob or "").casefold()
    if any(marker in text for marker in HARTFORD_SITE_MARKERS):
        return True
    return HARTFORD_NAME_MARKER in text and any(
        word in text for word in HARTFORD_PORTAL_WORDS
    )


def _blob_from_job_parts(
    text: str = "",
    payload: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    code: str = "",
) -> str:
    """Normalized searchable text from the pieces a gate caller has."""
    job = dict(job or {})
    explicit_payload = dict(payload or {})
    # Jobs usually nest their fields under job["payload"]; merge both so the
    # gate sees URLs and carrier names wherever the caller put them.
    payload = {**(job.get("payload") or {}), **explicit_payload}
    parts = [
        text,
        code,
        payload.get("text"),
        payload.get("url"),
        payload.get("site"),
        payload.get("skill"),
        payload.get("skill_name"),
        payload.get("action"),
        job.get("action_type"),
    ]
    return " ".join(" ".join(str(part or "").casefold().split()) for part in parts)


def hartford_playwright_refusal(
    *,
    text: str = "",
    payload: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    code: str = "",
    hostname: str | None = None,
) -> str | None:
    """Fail-closed gate for Hartford browser jobs on ROBIE servers.

    Returns the plain-English refusal when a Hartford portal / EBC Playwright
    job would start on a hermes-* host; returns None otherwise (not a Hartford
    target, or not on a ROBIE server). `hostname` is injectable for tests;
    when omitted the real host name is used.
    """
    if not is_hermes_server_host(hostname):
        return None
    blob = _blob_from_job_parts(text=text, payload=payload, job=job, code=code)
    if not hartford_target_in_blob(blob):
        return None
    return HARTFORD_REFUSAL
