"""Human-facing HITL copy for email and Chat.

Carlo 2026-09-13: write like a person. Name the missing thing. Say exactly
what to reply. No PLAYWRIGHT_BLOCKED dumps. No Job ID / Phase / Error as the
lead. Email never says Chat thread. Chat may say Chat thread.

Gmail mobile garbled earlier HITL/UNVERIFIED mail (letters smashed / extra
spaces). Causes we found: (1) one long facts line that Python then
quoted-printable-wraps mid-word, (2) invisible format characters (ZWSP,
soft hyphen, NBSP) if a composer injects them. This module emits short
plain sentences and strips those characters. The mailer sends text/plain
8bit only — no HTML, no letter-spacing.
"""

from __future__ import annotations

import ast
import re
from typing import Any

CHANNEL_EMAIL = "email"
CHANNEL_CHAT = "chat"

# Invisible / format chars that Gmail mobile can render as letter-spacing.
_STRIP_CHARS = dict.fromkeys(
    map(
        ord,
        (
            "\u200b"  # ZWSP
            "\u200c"  # ZWNJ
            "\u200d"  # ZWJ
            "\u200e"  # LRM
            "\u200f"  # RLM
            "\u2060"  # word joiner
            "\u2061"
            "\u2062"
            "\u2063"
            "\ufeff"  # BOM
            "\u00ad"  # soft hyphen
        ),
    ),
    None,
)

COVERAGE_AMOUNTS_MISSING_EMAIL = (
    "I opened the homeowners coverage page.\n"
    "I need the Coverage A, B, C, D, E, and F dollar amounts. "
    "They were not on the email and I will not invent them.\n"
    "Reply to this email with those six numbers, or attach a quote/dec PDF and say RETRY."
)

COVERAGE_AMOUNTS_MISSING_CHAT = (
    "I opened the homeowners coverage page.\n"
    "I need the Coverage A, B, C, D, E, and F dollar amounts. "
    "They were not on the job and I will not invent them.\n"
    "Reply in this Chat thread with those six numbers, or attach a quote/dec PDF and say RETRY."
)

COVERAGE_LABELS_EMPTY_EMAIL = (
    "I opened the homeowners coverage page.\n"
    "I could not match the coverage labels on the page. I will not invent dollar amounts.\n"
    "Reply to this email with Coverage A, B, C, D, E, and F, or attach a quote/dec PDF and say RETRY."
)

COVERAGE_LABELS_EMPTY_CHAT = (
    "I opened the homeowners coverage page.\n"
    "I could not match the coverage labels on the page. I will not invent dollar amounts.\n"
    "Reply in this Chat thread with Coverage A, B, C, D, E, and F, or attach a quote/dec PDF and say RETRY."
)

COVERAGE_TAB_STUCK_EMAIL = (
    "I am on the address tab and cannot open Coverages.\n"
    "I stopped so I would not fill the wrong page. "
    "The Coverage A through F amounts were already on the email.\n"
    "Reply to this email and say RETRY after Coverages is open."
)

COVERAGE_TAB_STUCK_CHAT = (
    "I am on the address tab and cannot open Coverages.\n"
    "I stopped so I would not fill the wrong page. "
    "The Coverage A through F amounts were already on the job.\n"
    "Reply in this Chat thread and say RETRY after Coverages is open."
)

_LIVE_NAV_RE = re.compile(r"live_nav=(\[[^\]]*\])")


def normalize_hitl_channel(channel: str | None) -> str:
    raw = str(channel or CHANNEL_CHAT).strip().casefold()
    if raw == CHANNEL_EMAIL:
        return CHANNEL_EMAIL
    return CHANNEL_CHAT


def reply_instruction(channel: str | None) -> str:
    if normalize_hitl_channel(channel) == CHANNEL_EMAIL:
        return "Reply to this email"
    return "Reply in this Chat thread"


def sanitize_plain_text(text: str) -> str:
    """Strip format junk. Keep real words. Do not smash letters together."""
    raw = str(text or "").translate(_STRIP_CHARS)
    raw = (
        raw.replace("\u00a0", " ")
        .replace("\u202f", " ")
        .replace("\u2007", " ")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    raw = re.sub(r"<[^>\n]+>", "", raw)
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank <= 1 and out:
                out.append("")
            continue
        blank = 0
        out.append(line)
    return "\n".join(out).strip()


def coverage_amounts_missing(error: str | None) -> bool:
    folded = " ".join(str(error or "").casefold().split())
    return (
        "coverage amounts not on the job" in folded
        or "will not guess coverage amounts" in folded
        or "will not invent them" in folded
    )


def still_on_location_tab(error: str | None) -> bool:
    """True when FormEntry is still Location/Address after a Coverages click."""
    folded = " ".join(str(error or "").casefold().split())
    compact = folded.replace(" ", "")
    return (
        "still on the formentry location" in folded
        or "could not open coverages" in folded
        or "i am on the address tab" in folded
        or "cannot open coverages" in folded
        or "cannot read the coverage fields" in folded
        or "cannot find coverages on this page" in folded
        or "no coverages name on the live formentry nav" in folded
        or "live_labels=[]" in compact
        or ("live_labels=" in folded and "location #" in folded)
    )


def live_nav_from_detail(detail: str) -> list[str]:
    """Parse ``live_nav=['Location', ...]`` from a checkpoint error."""
    match = _LIVE_NAV_RE.search(str(detail or ""))
    if not match:
        return []
    try:
        parsed = ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def format_seen_live_nav(live_nav: list[str]) -> str:
    """Name the live nav the worker actually saw. Never a placeholder."""
    from .formentry_coverages import normalize_coverage_label

    preferred: list[str] = []
    for want in ("location", "address"):
        for item in live_nav:
            folded = normalize_coverage_label(item)
            if folded == want or folded.startswith(want + " "):
                label = "Address" if folded.startswith("address") else item
                if label not in preferred:
                    preferred.append(label)
                break
    if preferred:
        return " / ".join(preferred[:3])
    if live_nav:
        return " / ".join(live_nav[:4])
    return "no section names"


def live_nav_missing_coverages(detail: str | None) -> bool:
    """True when the live nav list had no Coverages/Coverage name."""
    from .formentry_coverages import live_nav_coverage_name

    folded = " ".join(str(detail or "").casefold().split())
    if (
        "cannot find coverages on this page" in folded
        or "no coverages name on the live formentry nav" in folded
    ):
        return True
    nav = live_nav_from_detail(str(detail or ""))
    if "live_nav=" not in folded:
        return False
    return live_nav_coverage_name(nav) is None


def coverage_nav_missing_human_text(
    *,
    channel: str = CHANNEL_EMAIL,
    live_nav: list[str] | None = None,
    detail: str = "",
) -> str:
    """HITL when the live nav list has no Coverages name."""
    email = normalize_hitl_channel(channel) == CHANNEL_EMAIL
    nav = list(live_nav or []) or live_nav_from_detail(detail)
    seen = format_seen_live_nav(nav)
    ask = reply_instruction(channel)
    where = "email" if email else "job"
    return (
        f"I see {seen} and cannot find Coverages on this page.\n"
        "I stopped so I would not fill the wrong page. "
        f"The Coverage A through F amounts were already on the {where}.\n"
        f"{ask} and say RETRY after Coverages is open."
    )


def coverage_labels_empty(error: str | None) -> bool:
    folded = " ".join(str(error or "").casefold().split())
    return (
        "no coverage labels were filled" in folded
        or "could not match the coverage labels" in folded
        or "looked for ['address']" in folded
        or ("live_labels=" in folded and "location #" in folded)
    )


def coverage_letters_from_detail(detail: str) -> list[str]:
    """Letters named after 'still need' / 'missing Coverage X'.

    Include the Oxford-comma tail (``A, B, C, D, E, and F``). A letter-class
    match on ``and`` used to swallow F, so Gmail said ``I have Coverage F``.
    """
    folded = str(detail or "")
    letters: list[str] = []
    for match in re.finditer(
        r"(?:still need(?: the)? coverage|missing coverage)\s+"
        r"(.+?)(?:\s+dollar|\s+amount|\.|$)",
        folded,
        re.IGNORECASE | re.DOTALL,
    ):
        for letter in re.findall(r"\b([A-F])\b", match.group(1).upper()):
            if letter not in letters:
                letters.append(letter)
    if not letters:
        for match in re.finditer(r"\bCoverage\s+([A-F])\b", folded, re.IGNORECASE):
            letter = match.group(1).upper()
            if "still need" in folded.casefold() and letter not in letters:
                letters.append(letter)
    return letters


def missing_coverage_letters_human_text(
    *,
    channel: str = CHANNEL_EMAIL,
    missing: list[str],
    have: list[str] | None = None,
) -> str:
    """Ask again for only the omitted letter(s). Do not invent them."""
    ask = reply_instruction(channel)
    need = [str(item).upper() for item in missing if str(item).strip()]
    if not need:
        return coverage_fill_human_text(channel=channel)
    have_list = [str(item).upper() for item in (have or []) if str(item).strip()]
    need_text = ", ".join(need[:-1] + ([f"and {need[-1]}"] if len(need) > 1 else need))
    if len(need) == 1:
        need_text = need[0]
    lines = []
    if have_list:
        have_text = ", ".join(
            have_list[:-1] + ([f"and {have_list[-1]}"] if len(have_list) > 1 else have_list)
        )
        lines.append(f"I have Coverage {have_text}.")
    else:
        lines.append("I opened the homeowners coverage page.")
    if len(need) == 1:
        lines.append(
            f"I still need the Coverage {need_text} dollar amount. I will not invent it."
        )
        lines.append(f"{ask} with that number.")
    else:
        lines.append(
            f"I still need Coverage {need_text} dollar amounts. I will not invent them."
        )
        lines.append(f"{ask} with those numbers.")
    return "\n".join(lines)


def coverage_fill_human_text(*, channel: str = CHANNEL_EMAIL, detail: str = "") -> str:
    """Plain-English coverage HITL. Never dumps PLAYWRIGHT_BLOCKED."""
    email = normalize_hitl_channel(channel) == CHANNEL_EMAIL
    if live_nav_missing_coverages(detail):
        return coverage_nav_missing_human_text(channel=channel, detail=detail)
    if still_on_location_tab(detail):
        return COVERAGE_TAB_STUCK_EMAIL if email else COVERAGE_TAB_STUCK_CHAT
    # Amounts already on the email/prompt: never ask for A–F again when
    # the miss is empty/Location labels (live 712eccd0).
    missing = coverage_letters_from_detail(detail)
    if missing and (
        "still need" in str(detail or "").casefold()
        or "will not invent it" in str(detail or "").casefold()
    ):
        have = [
            letter
            for letter in ("A", "B", "C", "D", "E", "F")
            if letter not in missing
        ]
        return missing_coverage_letters_human_text(
            channel=channel, missing=missing, have=have
        )
    if coverage_labels_empty(detail):
        if missing:
            have = [
                letter
                for letter in ("A", "B", "C", "D", "E", "F")
                if letter not in missing
            ]
            return missing_coverage_letters_human_text(
                channel=channel, missing=missing, have=have
            )
        return COVERAGE_LABELS_EMPTY_EMAIL if email else COVERAGE_LABELS_EMPTY_CHAT
    if coverage_amounts_missing(detail):
        return COVERAGE_AMOUNTS_MISSING_EMAIL if email else COVERAGE_AMOUNTS_MISSING_CHAT
    return COVERAGE_AMOUNTS_MISSING_EMAIL if email else COVERAGE_AMOUNTS_MISSING_CHAT


def mint_miss_human_text(*, channel: str = CHANNEL_CHAT) -> str:
    ask = reply_instruction(channel)
    return (
        "I clicked Save and Continue Edit, but the coverage page did not open.\n"
        "I stopped so I would not guess the next click.\n"
        f"{ask} and say RETRY after the coverage page is open, or tell me what you see."
    )


def fail_closed_human_text(*, channel: str = CHANNEL_CHAT) -> str:
    ask = reply_instruction(channel)
    return (
        "I could not start the homeowners policy setup because the setup tool is not available.\n"
        "I stopped so I would not wander in the browser.\n"
        f"{ask} and say RETRY after the tool is registered, or tell me what to do."
    )


def worker_report_human_text(text: str, *, channel: str = CHANNEL_EMAIL) -> str:
    """Worker-report Gmail/Chat: short plain sentences. No PLAYWRIGHT_BLOCKED lead."""
    from .policy_setup_dispatch import (
        is_coverage_fill_miss,
        is_formentry_mint_miss,
        is_policy_setup_fail_closed,
    )

    raw = sanitize_plain_text(text)
    if (
        is_coverage_fill_miss(raw)
        or coverage_labels_empty(raw)
        or coverage_amounts_missing(raw)
        or still_on_location_tab(raw)
    ):
        return coverage_fill_human_text(channel=channel, detail=raw)
    if is_formentry_mint_miss(raw):
        return mint_miss_human_text(channel=channel)
    if is_policy_setup_fail_closed(raw):
        return fail_closed_human_text(channel=channel)
    folded = raw.casefold()
    if folded.startswith("playwright_blocked") or folded.startswith("robie_blocked"):
        return generic_stuck_human_text(channel=channel, what_happened=raw)
    return _short_plain_sentences(raw)


def _short_plain_sentences(text: str) -> str:
    raw = sanitize_plain_text(text)
    lines = [line for line in raw.split("\n") if line]
    if len(lines) >= 2 and all(len(line) <= 120 for line in lines):
        return raw
    parts = re.split(r"(?<=[.!?])\s+", raw)
    return "\n".join(part.strip() for part in parts if part.strip())


def generic_stuck_human_text(
    *,
    channel: str = CHANNEL_CHAT,
    what_happened: str = "",
) -> str:
    ask = reply_instruction(channel)
    happened = " ".join(str(what_happened or "").split())
    lowered = happened.casefold()
    for prefix in (
        "playwright_blocked:",
        "robie_blocked:",
        "robie hitl:",
        "robie_outcome_unknown:",
    ):
        if lowered.startswith(prefix):
            happened = happened.split(":", 1)[-1].strip()
            lowered = happened.casefold()
    happened = (
        happened.replace("STOP AND ASK.", "")
        .replace("STOP AND ASK", "")
        .strip(" :-")
    )
    if len(happened) > 180:
        happened = happened[:177].rstrip() + "..."
    first = "I got stuck and I will not guess the next click."
    if happened and "will not guess" not in happened.casefold():
        first = f"I got stuck ({happened}) and I will not guess the next click."
    return (
        f"{first}\n"
        f"{ask} with RETRY after the page is corrected, or tell me what to enter."
    )


def missing_field_human_text(
    *,
    channel: str = CHANNEL_CHAT,
    field_label: str,
    work_context: str,
    requester_name: str | None = None,
    sensitive: bool = False,
) -> str:
    ask = reply_instruction(channel)
    who = " ".join(str(requester_name or "").split())
    greet = f"{who}, I need a quick hand." if who else "I need a quick hand."
    if sensitive:
        value_warn = (
            "Do not send that value in this email."
            if normalize_hitl_channel(channel) == CHANNEL_EMAIL
            else "Do not send that value in Chat."
        )
        return (
            f"{greet}\n"
            f"I am working on {work_context}, but a sensitive field is missing: {field_label}.\n"
            f"Enter it directly in EZLynx, then {ask.casefold()} and say RETRY. "
            f"{value_warn}"
        )
    return (
        f"{greet}\n"
        f"I am working on {work_context}, but I am missing the {field_label}.\n"
        f"{ask} with the {field_label} so I can continue."
    )


def _gemini_sentence(request: Any, gemini_response: Any) -> str | None:
    """Only mention Gemini when it was actually asked. Never invent a failure."""
    named = str(getattr(request, "gemini_named_option", "") or "").strip()
    asked = bool(getattr(request, "gemini_asked", False)) or bool(named)
    applied = bool(getattr(request, "gemini_applied", False))
    shown = str(getattr(request, "live_control_shows", "") or "").strip()
    suggestion = ""
    if gemini_response is not None:
        suggestion = str(getattr(gemini_response, "suggestion", "") or "").strip()
        if getattr(gemini_response, "source", "") == "gemini":
            asked = True
    if getattr(request, "unguessable", False) or coverage_amounts_missing(
        getattr(request, "error", "")
    ):
        return None
    if not asked:
        return None
    if named and not applied:
        return (
            f"Gemini named a live option ({named}), but I could not apply it. "
            "I will not guess."
        )
    if named and applied and shown:
        return f"Gemini named {named} and the page shows {shown}."
    if suggestion and "unsure" in str(getattr(request, "error", "") or "").casefold():
        return "I asked Gemini which option to use and it was still unsure. I will not guess."
    if suggestion:
        if applied:
            return "Gemini answered and I applied a fill."
        return "Gemini answered, but I could not apply it. I will not guess."
    return "I asked Gemini and did not get a usable live option. I will not guess."


def human_hitl_notice(request: Any, gemini_response: Any = None) -> dict[str, str]:
    """Carlo-facing email + Chat HITL. Short sentences a phone can read.

    Email body never says Chat thread. Chat body may say Chat thread.
    Both are always filled so a mis-detected channel cannot leak Chat
    wording into Gmail.
    """
    from .policy_setup_dispatch import (
        is_coverage_fill_miss,
        is_formentry_mint_miss,
        is_policy_setup_fail_closed,
    )

    error = str(getattr(request, "error", "") or "")
    phase = str(getattr(request, "phase", "") or "")
    job_id = str(getattr(request, "job_id", "") or "")
    subject = f"[ROBIE HITL] Job {job_id} stuck at {phase or 'this step'}"

    if (
        phase == "coverage_fill"
        or coverage_amounts_missing(error)
        or coverage_labels_empty(error)
        or still_on_location_tab(error)
        or is_coverage_fill_miss(error)
    ):
        return {
            "subject": subject,
            "body": coverage_fill_human_text(channel=CHANNEL_EMAIL, detail=error),
            "chat": coverage_fill_human_text(channel=CHANNEL_CHAT, detail=error),
        }

    if is_formentry_mint_miss(error):
        return {
            "subject": subject,
            "body": mint_miss_human_text(channel=CHANNEL_EMAIL),
            "chat": mint_miss_human_text(channel=CHANNEL_CHAT),
        }

    if is_policy_setup_fail_closed(error):
        return {
            "subject": subject,
            "body": fail_closed_human_text(channel=CHANNEL_EMAIL),
            "chat": fail_closed_human_text(channel=CHANNEL_CHAT),
        }

    gemini_line = _gemini_sentence(request, gemini_response)
    lines: list[str] = ["I need a human to finish this step."]
    if gemini_line:
        lines.append(gemini_line)
    elif getattr(request, "save_skipped", False):
        lines.append("I skipped Save so I would not guess.")
    else:
        lines.append("I stopped so I would not guess the next click.")
    ask_line = " with RETRY after the page is corrected, or tell me what to enter."
    return {
        "subject": subject,
        "body": "\n".join(lines + [reply_instruction(CHANNEL_EMAIL) + ask_line]),
        "chat": "\n".join(lines + [reply_instruction(CHANNEL_CHAT) + ask_line]),
    }
