"""Prompt construction for Eva (Bland voice agent).

Jake's requirements (2026-10-02, after 5 live test calls):
- AI name: Eva.
- Identify UP FRONT as an AI assistant calling on behalf of Jake from
  StreetSmart Insurance. State the reason for the call in ONE sentence.
- Screener handling: if someone other than the target answers, answer
  directly and slowly, repeat if asked, stay on the line until connected
  to the target person. Never hang up on a screener.
- End the call ONLY when: the person is unavailable, or voicemail is reached.

Two prompt paths:
1. freeform (campaign "robie-call"): the Zapier "New Note" trigger sees
   the full note text — which the EZLynx API never returns — so the Zap
   POSTs it as `note_body` and THAT is Eva's instruction. Eva receives
   FULL applicant context (name, policies with carriers/numbers/dates)
   plus the note text, and is told to infer the concrete reason and run
   with it rather than asking the client what the call is about.
   Fallback: the discussion TITLE (via the v8 API), used only when the
   Zap didn't send note_body.
2. deterministic (10 campaigns): canned script per campaign_id, loaded
   from scripts_config.json. Carlo approves the scripts before go-live.
"""
import json
import os
from typing import Any, Dict, List, Optional

from config import Config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_PATH = os.path.join(BASE_DIR, "scripts_config.json")

# ----------------------------------------------------------------------
# Shared identity / behavior blocks (Jake's requirements)
# ----------------------------------------------------------------------
EVA_IDENTITY = (
    "Your name is Eva. You are an AI assistant calling on behalf of Jake "
    "from StreetSmart Insurance. You MUST say this up front, in your very "
    "first sentence: that you are Eva, an AI assistant, calling for Jake "
    "from StreetSmart Insurance."
)

REASON_ONE_SENTENCE = (
    "Immediately after identifying yourself, state the reason for your call "
    "in exactly one clear sentence."
)

SCREENER_RULES = (
    "If someone other than the person you are trying to reach answers "
    "(a receptionist, family member, coworker, or screener): answer their "
    "questions directly and SLOWLY, repeat yourself if asked, and stay on "
    "the line until you are connected to the right person. Do NOT hang up "
    "on a screener."
)

END_RULES = (
    "End the call ONLY in these two cases: (1) the person you need is "
    "unavailable and no one else can help, or (2) you reached voicemail. "
    "Otherwise, keep working toward the goal of the call."
)

TRANSFER_RULES = (
    f"If the person asks to speak with Jake directly, or the conversation "
    f"needs a human, offer to transfer them to Jake now."
)

BASE_BEHAVIOR = "\n\n".join(
    [EVA_IDENTITY, REASON_ONE_SENTENCE, SCREENER_RULES, END_RULES, TRANSFER_RULES]
)


def voicemail_message(reason_sentence: str) -> str:
    """Full SLOW voicemail for the 2nd dial attempt.

    Includes AI disclosure and the callback number digit by digit.
    """
    digits = " ".join(c for c in Config.CALLBACK_NUMBER if c.isdigit())
    return (
        f"Hi, this is Eva, an A.I. assistant calling on behalf of Jake "
        f"from StreetSmart Insurance. {reason_sentence} "
        f"Please call Jake back at {digits}. "
        f"Again, that number is {digits}. Thank you, and have a great day."
    )


def first_sentence(client_first_name: str, reason_sentence: str) -> str:
    """Bland 'first_sentence': the literal opening line (AI disclosure up front)."""
    name_bit = f" {client_first_name}" if client_first_name else ""
    return (
        f"Hi{name_bit}, this is Eva, an AI assistant calling on behalf of "
        f"Jake from StreetSmart Insurance. {reason_sentence}"
    )


# ----------------------------------------------------------------------
# Freeform flow ("robie-call")
# ----------------------------------------------------------------------
def build_freeform_task(
    client_name: str,
    client_first_name: str,
    instruction: str,
    policies: List[Dict[str, Any]],
    phone: str,
    instruction_source: str = "note",
) -> str:
    """Build Eva's task for the freeform Robie Call flow.

    The instruction is the note text POSTed by the Zapier "New Note"
    trigger (`note_body`) — note bodies are not available via the EZLynx
    API. When the Zap didn't send note_body, the discussion TITLE (v8 API
    fallback) is used instead. Eva gets the full applicant picture and is
    told to infer the concrete reason and run with it rather than asking
    the client what the call is about.

    instruction_source: "webhook note_body" (primary), "discussion ... title"
    or "most recent discussion title" (fallback) — controls the label Eva
    sees so the prompt stays honest about where the instruction came from.
    """
    policy_lines = []
    for p in policies[:8]:
        bits = [
            str(p.get("policy_number") or p.get("policyNumber") or "unknown #"),
            str(p.get("carrier_name") or p.get("carrierName") or p.get("carrier") or ""),
            str(p.get("line_of_business") or p.get("lineOfBusiness") or p.get("lob") or ""),
            str(p.get("expiration_date") or p.get("expirationDate") or ""),
        ]
        policy_lines.append(" | ".join(b for b in bits if b).strip())
    policies_block = (
        "\n".join(f"- {line}" for line in policy_lines)
        if policy_lines
        else "- No policies on file."
    )

    if "note_body" in (instruction_source or ""):
        source_label = ("THE INSTRUCTION FOR THIS CALL "
                        "(the EZLynx note the CSR wrote — verbatim):")
    elif "title" in (instruction_source or ""):
        source_label = ("THE INSTRUCTION FOR THIS CALL "
                        "(the EZLynx discussion title — the note text was not "
                        "available, so the title was used as a fallback):")
    else:
        source_label = "THE INSTRUCTION FOR THIS CALL:"

    return f"""{BASE_BEHAVIOR}

CALL CONTEXT (from StreetSmart's system — the client does NOT know you have this):
Client: {client_name}
Phone you dialed: {phone}
Policies on file:
{policies_block}

{source_label}
\"{instruction}\"

HOW TO RUN WITH IT:
- The instruction above may be short or vague (for example, "call about renewal").
  Do NOT ask the client what the call is about. Use the policies above
  to figure out the concrete reason yourself, then state it in one sentence
  right after your AI disclosure.
- Example: if the instruction is "call about renewal" and a policy expires
  soon, your reason sentence is about that specific policy's upcoming renewal.
- Speak naturally and slowly. Keep the call focused and brief.
- If you cannot determine a concrete reason from the context, say you are
  following up on their StreetSmart Insurance account and ask how you can help.
"""


# ----------------------------------------------------------------------
# Deterministic flow (10 canned campaigns)
# ----------------------------------------------------------------------
_DETERMINISTIC_CAMPAIGNS = [
    "robie-lead-followup",
    "robie-client-outreach",
    "robie-cancellation",
    "robie-audit",
    "robie-returned-mail",
    "robie-esign",
    "robie-additional-info",
    "robie-recommendations",
    "robie-unresponsive",
    "robie-renewal-reachout",
]


def load_scripts(path: str = SCRIPTS_PATH) -> Dict[str, Dict[str, str]]:
    """Load {campaign_id: {"reason_sentence": ..., "script": ...}}.

    Missing campaigns return {}. Callers must treat a missing script as a
    hard stop (never dial without an approved script).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if k in _DETERMINISTIC_CAMPAIGNS}


def build_deterministic_task(
    client_name: str,
    client_first_name: str,
    campaign_id: str,
    script: Dict[str, str],
) -> str:
    """Build Eva's task for a deterministic campaign from its approved script."""
    reason = script.get("reason_sentence", "").strip()
    body = script.get("script", "").strip()
    return f"""{BASE_BEHAVIOR}

THIS CALL'S SCRIPT (follow it exactly; do not improvise beyond it):
Client: {client_name}

{body}

If the client goes off-script with questions you cannot answer from the script
above, offer to have Jake call them back personally.
"""


def deterministic_campaigns() -> List[str]:
    return list(_DETERMINISTIC_CAMPAIGNS)
