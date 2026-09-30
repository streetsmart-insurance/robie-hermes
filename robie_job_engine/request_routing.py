from __future__ import annotations

import os
import re
from dataclasses import dataclass


WORKER_FOR_ACTION = {
    "accountability.daily": "accountability-report",
    "accountability.weekly": "accountability-report",
    "accountability.monthly": "accountability-report",
    "meeting.synthesis.weekly": "meeting-synthesis",
    "staff.fun.monthly": "staff-fun",
    "drive.skill_sync": "drive-skill-sync",
    "carrier.proposal": "carrier-proposal",
    "browser.read": "browser-read",
    "ezlynx.reassign": "hermes-cua",
    "ezlynx.move_document": "hermes-cua",
    "ezlynx.apply_label": "hermes-cua",
    "ezlynx.submission_audit": "submission-audit",
    "ezlynx.overdue_submission_reports": "overdue-submission-reports",
    "ezlynx.session_refresh": "session-refresh",
    "filesystem.skill_update": "hermes-cua",
    "appsheet.smart_reward": "hermes-cua",
    "appsheet.qa_audit": "hermes-cua",
    "hermes.plain_english": "hermes-cua",
    "hermes.google_chat_task": "hermes-cua",
    "hermes.needs_clarification": "hermes-cua",
    "hermes.unavailable": "hermes-cua",
    # Playground Chat types. Not bounded: they run on the general agent.
    # Production job-type gate is not bypassed because they are not in
    # BOUNDED_ENGINE_ACTIONS; classification itself stays off unless playground.
    "ezlynx.quote": "hermes-cua",
    "ezlynx.commercial_auto": "hermes-cua",
    "ezlynx.policy_change": "hermes-cua",
    "ezlynx.policy_setup": "hermes-cua",
    "ezlynx.certificate": "hermes-cua",
    "manual_renewal_verification": "manual-renewal",
    "audit_verification": "audit-verification",
    "mortgagee_verification": "mortgagee-verification",
    "policy_change_verification": "policy-change-verification",
    "daily_verification_digest": "verification-digest",
}

BOUNDED_ENGINE_ACTIONS = frozenset(
    {
        "accountability.daily",
        "accountability.weekly",
        "accountability.monthly",
        "meeting.synthesis.weekly",
        "staff.fun.monthly",
        "drive.skill_sync",
        "carrier.proposal",
        "browser.read",
        "ezlynx.reassign",
        "ezlynx.move_document",
        "ezlynx.apply_label",
        "ezlynx.submission_audit",
        "ezlynx.overdue_submission_reports",
        "ezlynx.session_refresh",
        # appsheet.smart_reward / appsheet.qa_audit were bounded but have never
        # had a verifier class, so every such job terminated UNVERIFIED by
        # construction. Production jobs.db shows ZERO rows for either, all
        # time, so nothing is losing a code path here. They fall through to the
        # freeform handler, which is where they effectively already were. Add
        # them back only alongside a real verifier.
        "manual_renewal_verification",
        "audit_verification",
        "mortgagee_verification",
        "policy_change_verification",
        "daily_verification_digest",
    }
)

_PROPOSAL_PHRASES = (
    "carrier proposal",
    "create a proposal",
    "generate a proposal",
    "make a proposal",
    "write a proposal",
    "proposal pdf",
)

_BROWSER_READ_PHRASES = (
    "read only",
    "read-only",
    "just look",
    "look up the page",
    "what does the page say",
    "check the page",
    "read the page",
    "open the page and tell me",
    "browser-only read",
    "browser only read",
)

_MUTATION_WORDS = (
    "upload",
    "move",
    "delete",
    "apply",
    "send",
    "create",
    "generate",
    "reassign",
    "submit",
)

_VAGUE = frozenset(
    {
        "do it",
        "handle this",
        "fix it",
        "take care of this",
        "you know what to do",
        "please help",
        "go ahead",
    }
)


@dataclass(frozen=True)
class RequestClassification:
    action_type: str
    worker: str
    hold_status: str | None = None
    answer_only: bool = False


# General-agent framing for playground Chat types. Existing EZLynx skills
# are named where they exist. A missing readback must not block the reply.
PLAYGROUND_TASK_FRAMING = {
    "ezlynx.quote": (
        "Task: quote request. Use the EZLynx quote flow. "
        "Do not bind, take payment, or email the client. "
        "Writes stay on the EZLynx test account only. "
        "If an EZLynx applicant, document, or note readback is available, "
        "keep it as evidence. Do not wait on that readback to answer."
    ),
    "ezlynx.commercial_auto": (
        "Task: commercial auto from an existing quote. Follow the "
        "ezlynx-commercial-auto-from-quote skill. A policy shell is not done. "
        "Do not bind, take payment, or email the client. "
        "Writes stay on the EZLynx test account only. "
        "If an EZLynx readback is available, keep it as evidence. "
        "Do not wait on that readback to answer."
    ),
    "ezlynx.policy_change": (
        "Task: policy change. File the note with ezlynx_discussion_note on the "
        "existing discussion title named in the request. For a mailing-address "
        "change, use that existing title. Do not create a discussion. "
        "Do not use Playwright Add Note or Save Note. "
        "Do not bind, take payment, or email the client. "
        "Do not read Robie's source, jobs.db, or token files. "
        "Writes stay on the EZLynx test account only. "
        "If an EZLynx readback is available, keep it as evidence. "
        "Do not wait on that readback to answer."
    ),
    "ezlynx.policy_setup": (
        "Task: homeowners policy setup on the EZLynx test account. "
        "Call the ezlynx_policy_setup tool before any browser step. "
        "Do not bind, take payment, or email the client. "
        "If an EZLynx readback is available, keep it as evidence. "
        "Do not wait on that readback to answer."
    ),
    "ezlynx.certificate": (
        "Task: certificate request. Use ezlynx_discussion_note and "
        "robie_job_engine.certificate_filing to draft and file the holder note "
        "on the existing discussion. Draft only. "
        "Do not bind, take payment, or email the client or the certificate holder. "
        "Do not browse EZLynx by hand. Do not read Robie's source, jobs.db, or token files. "
        "Writes stay on the EZLynx test account only. "
        "If an EZLynx readback is available, keep it as evidence. "
        "Do not wait on that readback to answer."
    ),
}

_QUOTE_RE = re.compile(
    r"\b(?:quote request|get a quote|need a quote|new quote|request a quote|quotes?)\b"
)
_POLICY_CHANGE_RE = re.compile(
    r"\b(?:policy change|change the policy|change this policy|endorsements?|endorse)\b"
    r"|\b(?:change|update|endorse)\b.{0,48}\bpolic"
    r"|\bpolic\w*\b.{0,48}\b(?:change|update|endorsement)\b"
)
_CERTIFICATE_RE = re.compile(
    r"\b(?:certificate of insurance|certificate request|cert request|certificates?|coi)\b"
)


def _normalized(text: str) -> str:
    return " ".join(str(text or "").casefold().split())


def _is_ascend_enabled() -> bool:
    return os.environ.get("ROBIE_ASCEND_API_ENABLED", "").strip().lower() in {"true", "1", "yes"}


def classify_request(text: str, *, attachment_count: int = 0) -> RequestClassification:
    """Deterministically classify a staff request before any worker runs."""
    normalized = _normalized(text)
    if _is_ascend_request(normalized):
        if _is_ascend_enabled():
            return RequestClassification(
                "hermes.google_chat_task",
                WORKER_FOR_ACTION["hermes.google_chat_task"],
            )
        return RequestClassification(
            "hermes.unavailable",
            WORKER_FOR_ACTION["hermes.unavailable"],
            hold_status="FAILED",
        )
    if is_skill_sync_command(normalized):
        return RequestClassification(
            "drive.skill_sync", WORKER_FOR_ACTION["drive.skill_sync"]
        )
    if _is_carrier_proposal(normalized):
        return RequestClassification("carrier.proposal", WORKER_FOR_ACTION["carrier.proposal"])
    if "ezlynx" in normalized and "reassign" in normalized:
        return RequestClassification("ezlynx.reassign", WORKER_FOR_ACTION["ezlynx.reassign"])
    if "ezlynx" in normalized and "move" in normalized and "document" in normalized:
        return RequestClassification("ezlynx.move_document", WORKER_FOR_ACTION["ezlynx.move_document"])
    if "ezlynx" in normalized and "label" in normalized:
        return RequestClassification("ezlynx.apply_label", WORKER_FOR_ACTION["ezlynx.apply_label"])
    if _is_overdue_submission_report(normalized):
        return RequestClassification(
            "ezlynx.overdue_submission_reports",
            WORKER_FOR_ACTION["ezlynx.overdue_submission_reports"],
        )
    if _is_submission_audit(normalized):
        return RequestClassification(
            "ezlynx.submission_audit", WORKER_FOR_ACTION["ezlynx.submission_audit"]
        )
    if _is_skill_update(normalized):
        return RequestClassification(
            "filesystem.skill_update", WORKER_FOR_ACTION["filesystem.skill_update"]
        )
    if _is_browser_read(normalized):
        return RequestClassification("browser.read", WORKER_FOR_ACTION["browser.read"])
    if normalized in _VAGUE and attachment_count == 0:
        return RequestClassification(
            "hermes.needs_clarification",
            WORKER_FOR_ACTION["hermes.needs_clarification"],
            hold_status="NEEDS_CLARIFICATION",
        )
    if _is_smart_reward(normalized):
        return RequestClassification(
            "appsheet.smart_reward", WORKER_FOR_ACTION["appsheet.smart_reward"]
        )
    if _is_qa_entry(normalized):
        return RequestClassification(
            "appsheet.qa_audit", WORKER_FOR_ACTION["appsheet.qa_audit"]
        )
    # Vague short asks and questions win before "can you " becomes a job
    # and before playground treats the word "quote" as an EZLynx write.
    from .answer_only import is_informational_ask, is_vague_short_request

    if is_vague_short_request(text, attachment_count=attachment_count):
        return RequestClassification(
            "hermes.needs_clarification",
            WORKER_FOR_ACTION["hermes.needs_clarification"],
            hold_status="NEEDS_CLARIFICATION",
        )
    if is_informational_ask(text):
        return RequestClassification(
            "hermes.plain_english",
            WORKER_FOR_ACTION["hermes.plain_english"],
            answer_only=True,
        )
    playground_route = _classify_playground_ezlynx(normalized)
    if playground_route is not None:
        return playground_route
    if _is_plain_english(normalized, attachment_count):
        return RequestClassification("hermes.plain_english", WORKER_FOR_ACTION["hermes.plain_english"])
    return RequestClassification(
        "hermes.google_chat_task", WORKER_FOR_ACTION["hermes.google_chat_task"]
    )


def _classify_playground_ezlynx(text: str) -> RequestClassification | None:
    """Quote, policy-change, and certificate types when playground is on.

    Flag off returns None on Test and Production, so classification stays
    exactly as it is today. Existing bounded routes above this call still
    win. Where an EZLynx skill already exists, the action type names it;
    otherwise the general agent gets the task framing. Readback is
    evidence, not a gate.
    """
    from .runtime_env import playground_enabled

    if not playground_enabled():
        return None
    if _is_commercial_auto_from_quote(text):
        return RequestClassification(
            "ezlynx.commercial_auto", WORKER_FOR_ACTION["ezlynx.commercial_auto"]
        )
    if _is_quote_request(text):
        return RequestClassification("ezlynx.quote", WORKER_FOR_ACTION["ezlynx.quote"])
    from .policy_setup_dispatch import detect_policy_setup_request

    if detect_policy_setup_request(text):
        return RequestClassification(
            "ezlynx.policy_setup", WORKER_FOR_ACTION["ezlynx.policy_setup"]
        )
    if _is_policy_change_request(text):
        return RequestClassification(
            "ezlynx.policy_change", WORKER_FOR_ACTION["ezlynx.policy_change"]
        )
    if _is_certificate_request(text):
        return RequestClassification(
            "ezlynx.certificate", WORKER_FOR_ACTION["ezlynx.certificate"]
        )
    return None


def _is_commercial_auto_from_quote(text: str) -> bool:
    return "commercial auto" in text and "quote" in text


def _is_quote_request(text: str) -> bool:
    return _QUOTE_RE.search(text) is not None


def _is_policy_change_request(text: str) -> bool:
    return _POLICY_CHANGE_RE.search(text) is not None


def _is_certificate_request(text: str) -> bool:
    return _CERTIFICATE_RE.search(text) is not None


def is_skill_sync_command(text: str) -> bool:
    """Recognize deterministic admin phrasing without catching Skill edits."""
    normalized = _normalized(text).strip(" .!?")
    return normalized in {
        "/sync-skills",
        "/sync skills",
        "sync skills",
        "update memory",
        "refresh skills",
        "refresh memory",
    }


def _is_carrier_proposal(text: str) -> bool:
    if any(phrase in text for phrase in _PROPOSAL_PHRASES):
        return True
    return "proposal" in text and ("fee" in text or "$350" in text or "350" in text)


def _is_browser_read(text: str) -> bool:
    if any(word in text for word in _MUTATION_WORDS):
        return False
    return any(phrase in text for phrase in _BROWSER_READ_PHRASES)


def _is_ascend_request(text: str) -> bool:
    """Recognize Ascend before generic browser or chat routing."""
    return bool(
        re.search(r"\bascend\b", text)
        or any(
            marker in text
            for marker in (
                "useascend",
                "premium finance",
                "premium-finance",
                "pawiva",
                "221398001",
            )
        )
    )


def _is_submission_audit(text: str) -> bool:
    if "submission center" not in text:
        return False
    if _has_positive_mutation(text, ("upload", "move", "delete", "send", "submit")):
        return False
    return any(word in text for word in ("audit", "review", "check", "read", "overdue"))


def _is_overdue_submission_report(text: str) -> bool:
    if "submission center" not in text or "overdue" not in text:
        return False
    return _has_positive_mutation(text, ("send", "email", "contact", "notify"))


def _has_positive_mutation(text: str, words: tuple[str, ...]) -> bool:
    positive = _positive_request_text(text)
    pattern = r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b"
    for match in re.finditer(pattern, positive):
        prefix = positive[max(0, match.start() - 24):match.start()]
        if not re.search(r"(?:do not|don't|never)\s+$", prefix):
            return True
    return False


def _positive_request_text(text: str) -> str:
    """Remove explicit safety prohibitions before mutation-word routing."""
    kept: list[str] = []
    for clause in re.split(r"(?<=[.!?;])\s+", text):
        normalized = clause.strip()
        if re.match(r"^(?:do not|don't|never)\b", normalized):
            contrast = re.search(r"\b(?:but|however)\b(.+)$", normalized)
            if contrast:
                kept.append(contrast.group(1))
            continue
        kept.append(normalized)
    return " ".join(kept)


def _is_skill_update(text: str) -> bool:
    has_target = "skill.md" in text or " skill file" in text or " skill-file" in text
    return has_target and any(word in text for word in ("update", "edit", "write", "create"))


def _is_plain_english(text: str, attachment_count: int) -> bool:
    if not text or attachment_count:
        return False
    return any(
        text.startswith(prefix)
        for prefix in (
            "please ",
            "can you ",
            "could you ",
            "i need ",
            "we need ",
        )
    )


def _is_smart_reward(text: str) -> bool:
    """Recognize staff reward, smart reward, or employee recognition commands."""
    if "reward" in text:
        return True
    if "smart reward" in text or "smart-reward" in text:
        return True
    return any(phrase in text for phrase in ("nominate ", "nomination", "give kudos", "kudos to "))


def _is_qa_entry(text: str) -> bool:
    """Recognize quality assurance audit entries."""
    return any(
        phrase in text
        for phrase in (
            "quality assurance",
            "qa audit",
            "qa score",
            "log qa",
            "add qa",
            "enter qa",
        )
    )
