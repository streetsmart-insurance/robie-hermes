"""Simple status format for Robie's job reports.

Jake's standing template (2026-09-28, "Robie status emails -- simpler format"):

    What happened: <plain words>
    Anything needed: <one line; "No." when nothing is needed>
    Status: <plain verification state>

Rules enforced here:
- Lead with the outcome in plain words (done / couldn't finish /
  not verified / waiting). No job IDs or internal codes up top.
- "Anything needed" is exactly one line. When there is nothing for a
  human to do, it says so in that line.
- The job ID is a small reference line at the very bottom.
- Our verification detail (confirmed facts, gaps, evidence notes) is
  kept, but moves below the three lines as a detail section.

This module is shared by the Chat renderer (chat_guard) and the email
renderer (email_guard) so every user gets the same format -- it is not
scoped to any team or department.

Internal worker codes (ROBIE_OUTCOME_UNKNOWN, ROBIE_EXECUTION_BLOCKED,
...) are translated to plain words for DISPLAY ONLY. The raw codes stay
intact upstream: detection logic such as chat_guard._looks_in_progress
operates on the worker's raw text before rendering and is unaffected.
"""

from __future__ import annotations

# Internal code prefixes stripped for display. The raw text is preserved
# in the Details section for debugging.
_DISPLAY_STRIP_PREFIXES = (
    "ROBIE_OUTCOME_UNKNOWN:",
    "ROBIE_EXECUTION_BLOCKED:",
)

# Known internal sentences -> plain words (display only).
_PLAIN_SENTENCES = {
    "The agent ended without a complete final turn.":
        "The job stopped before producing a final answer.",
    "The agent returned no final response.":
        "The job produced no result.",
    "Email execution timed out.":
        "The email job timed out.",
    "No trustworthy final agent receipt was available.":
        "The job's result couldn't be confirmed.",
    "ROBIE could not safely finish the requested work.":
        "Robie couldn't safely finish the requested work.",
    "ROBIE is not treating this request as successful.":
        "Robie isn't treating this as successful.",
    "No success claims from the Computer Worker are being reported.":
        "No success claims from the worker are being reported.",
}


def plain_reason(text: str | None) -> str:
    """Translate internal worker phrasing to plain words for display."""
    text = str(text or "").strip()
    for prefix in _DISPLAY_STRIP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    for internal, plain in _PLAIN_SENTENCES.items():
        text = text.replace(internal, plain)
    # Collapse leftovers like "ROBIE did not independently verify" -> plain.
    text = text.replace("ROBIE did not independently verify",
                        "Robie didn't independently verify")
    text = text.replace("ROBIE attempted the work.",
                        "Robie tried the work.")
    text = text.replace("ROBIE", "Robie")
    return text.strip()


def short_job_ref(job_id: str | None) -> str:
    """Small reference line for the bottom of the message.

    Uses the full job ID so a reader can match the message to the exact
    job it reports on.
    """
    job_id = str(job_id or "").strip()
    if not job_id:
        return ""
    return f"Ref: job {job_id}"


def render_simple_status(
    *,
    headline: str,
    what_happened: str,
    anything_needed: str,
    status_line: str,
    details: str = "",
    job_id: str | None = None,
) -> str:
    """Render one status message in the simple shared format.

    ``headline`` is the plain-words outcome and always comes first.
    ``details`` is our existing verification content (confirmed facts,
    gaps, evidence) -- kept verbatim below the three lines.
    """
    parts = [
        headline.strip(),
        "",
        f"What happened: {what_happened.strip()}",
        f"Anything needed: {anything_needed.strip()}",
        f"Status: {status_line.strip()}",
    ]
    details = str(details or "").strip()
    if details:
        parts += ["", "Details", details]
    ref = short_job_ref(job_id)
    if ref:
        parts += ["", ref]
    return "\n".join(parts).strip() + "\n"


def render_end_state_report(
    *,
    summary: str,
    end_state: str,
    jev_line: str,
    details: str = "",
    job_id: str | None = None,
) -> str:
    """End-state layout used when ``ROBIE_END_STATE_REPORT`` is on.

    Order is fixed: one-sentence summary, the end state, Jev's verdict,
    the Details block, then the full job id on the last line. Replies
    with the flag off keep ``render_simple_status``.
    """
    parts = [
        summary.strip(),
        "",
        f"End state: {end_state.strip()}",
        jev_line.strip(),
    ]
    details = str(details or "").strip()
    if details:
        parts += ["", "Details", details]
    ref = short_job_ref(job_id)
    if ref:
        parts += ["", ref]
    return "\n".join(parts).strip() + "\n"
