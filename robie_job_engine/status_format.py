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

# Whole-message codes that must not appear in "What happened".
# The raw text stays in Details. Match is display-only.
_PLAIN_CODE_SENTENCES = (
    (
        "PLAYWRIGHT_SILENT",
        "The browser never recorded a step, so Robie stopped instead of guessing.",
    ),
    (
        "recording is required but disabled",
        "The screen recording this job needs was turned off, so Robie stopped.",
    ),
    (
        "ACTION_GATE_REFUSED",
        "This action is blocked until a clean Test pass is on file.",
    ),
    (
        "no structured destination action checkpoint",
        "Robie finished talking, but nothing was checked against the destination.",
    ),
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
    folded = text.casefold()
    for needle, sentence in _PLAIN_CODE_SENTENCES:
        if needle.casefold() in folded:
            return sentence
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


def plain_retry_refusal(reason: str | None, *, status: str = "") -> str:
    """One plain sentence for a refused retry. The code stays in Details."""
    folded = str(reason or "").casefold()
    current = str(status or "").strip().upper()
    if "24 hour" in folded:
        return "That job is more than 24 hours old, so it was not restarted."
    if current == "FAILED":
        return (
            "This job already failed, and retry is not turned on here, "
            "so it was not restarted."
        )
    if current == "UNVERIFIED":
        return (
            "This job was not verified, and retry is not turned on here, "
            "so it was not restarted."
        )
    if current == "AWAITING_HUMAN_INPUT" and "older than" in folded:
        return "That pause is more than an hour old, so retry will not reopen it."
    if "no usable" in folded:
        return "Retry was refused because that job has no usable status."
    if current == "COMPLETE":
        return "That job already finished, so retry did not restart it."
    return "Retry was refused. Send the request again if you still want it done."


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
