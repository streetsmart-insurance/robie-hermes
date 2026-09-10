"""
Escalation Rules & E&O Safeguard Protocols for Robie Conversational AI.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("escalation_rules")


class EscalationType(str, enum.Enum):
    BIND_REQUEST = "BIND_REQUEST"
    COVERAGE_ADVICE = "COVERAGE_ADVICE"
    HUMAN_REQUEST = "HUMAN_REQUEST"
    COMPLAINT = "COMPLAINT"
    CONFUSION = "CONFUSION"
    NONE = "NONE"


@dataclass
class EscalationDecision:
    escalation_type: EscalationType
    spoken_transition: str
    action: str  # "WARM_TRANSFER", "CREATE_HIGH_PRIORITY_TASK", "CLARIFY_AND_OFFER_CALLBACK"
    requires_producer_transfer: bool
    eo_risk_level: str  # "HIGH", "MEDIUM", "LOW"
    task_priority: str  # "URGENT", "HIGH", "NORMAL"
    log_summary: str


class EscalationClassifier:
    BIND_PATTERNS = [
        r"\b(?:want\s+to\s+bind|ready\s+to\s+bind|bind\s+this|bind\s+the\s+policy)\b",
        r"\b(?:buy\s+(?:the\s+policy|this|coverage)|ready\s+to\s+buy)\b",
        r"\b(?:sign\s+(?:me\s+)?up|take\s+my\s+payment|pay\s+now|start\s+(?:the\s+)?policy)\b",
        r"\b(?:let'?s\s+do\s+it|go\s+ahead\s+with\s+this|lock\s+it\s+in)\b",
    ]

    COVERAGE_ADVICE_PATTERNS = [
        r"\b(?:what\s+(?:\w+\s+)?limits?\s+(?:should|do)\s+i\s+(?:get|need|choose))\b",
        r"\b(?:what\s+(?:\w+\s+)?limits?\s+do\s+you\s+recommend)\b",
        r"\b(?:do\s+i\s+need\s+(?:collision|comprehensive|gap|umbrella|pip|full\s+coverage))\b",
        r"\b(?:what\s+does\s+(?:collision|comprehensive|liability|deductible)\s+mean)\b",
        r"\b(?:is\s+(?:\$?[0-9]+[kK]?|[0-9]+,\d{3})\s+enough\s+coverage)\b",
        r"\b(?:can\s+you\s+recommend|what\s+do\s+you\s+recommend|what\s+coverage\s+is\s+better)\b",
        r"\b(?:am\s+i\s+covered\s+if)\b",
        r"\b(?:advise\s+me\s+on\s+coverage)\b",
    ]

    HUMAN_PATTERNS = [
        r"\b(?:speak\s+to\s+(?:a\s+)?(?:human|person|agent|producer|representative|someone|real\s+person))\b",
        r"\b(?:give\s+me\s+a\s+person|transfer\s+me|get\s+me\s+someone)\b",
        r"\b(?:are\s+you\s+(?:a\s+)?(?:robot|ai|machine|automated|bot))\b",
        r"\b(?:let\s+me\s+talk\s+to\s+(?:jake|carlo|an\s+agent))\b",
    ]

    COMPLAINT_PATTERNS = [
        r"\b(?:stop\s+spamming|why\s+are\s+you\s+calling|harassing\s+me)\b",
        r"\b(?:ridiculous|terrible|horrible|angry|upset|pissed|lawyer|attorney|sue)\b",
        r"\b(?:too\s+expensive|rip\s*off|scam)\b",
    ]

    CONFUSION_PATTERNS = [
        r"\b(?:who\s+are\s+you(?:\s+again)?|who\s+is\s+this|what\s+is\s+streetsmart)\b",
        r"\b(?:i\s+didn'?t\s+(?:request|ask\s+for)\s+(?:a\s+)?quote)\b",
        r"\b(?:why\s+are\s+you\s+calling\s+me|what\s+is\s+this\s+about)\b",
        r"\b(?:i\s+don'?t\s+understand|what\s+do\s+you\s+mean)\b",
    ]

    @classmethod
    def evaluate(
        cls,
        utterance: str,
        producer_name: str = "Jake Ferrara",
        producer_first_name: str = "Jake",
    ) -> EscalationDecision:
        text = utterance.strip().lower()

        # 1. Bind Request
        if any(re.search(pat, text) for pat in cls.BIND_PATTERNS):
            return EscalationDecision(
                escalation_type=EscalationType.BIND_REQUEST,
                spoken_transition=(
                    f"That's wonderful! Let me connect you directly with {producer_first_name} right now "
                    f"so they can finalize your application and bind your coverage."
                ),
                action="WARM_TRANSFER",
                requires_producer_transfer=True,
                eo_risk_level="HIGH",
                task_priority="URGENT",
                log_summary=f"Prospect requested to bind coverage. Initiating warm transfer to {producer_name}.",
            )

        # 2. Coverage Advice (E&O Protection Gate)
        if any(re.search(pat, text) for pat in cls.COVERAGE_ADVICE_PATTERNS):
            return EscalationDecision(
                escalation_type=EscalationType.COVERAGE_ADVICE,
                spoken_transition=(
                    f"As an automated assistant, I can't advise on specific coverage limits or legal options, "
                    f"but {producer_first_name} is licensed and right here to advise you. Let me get them on the line."
                ),
                action="WARM_TRANSFER",
                requires_producer_transfer=True,
                eo_risk_level="HIGH",
                task_priority="URGENT",
                log_summary=f"E&O Guard Triggered: Prospect requested coverage advice. Routing to licensed producer {producer_name}.",
            )

        # 3. Human Requested
        if any(re.search(pat, text) for pat in cls.HUMAN_PATTERNS):
            return EscalationDecision(
                escalation_type=EscalationType.HUMAN_REQUEST,
                spoken_transition=(
                    f"Of course! Let me connect you with {producer_first_name} right now."
                ),
                action="WARM_TRANSFER",
                requires_producer_transfer=True,
                eo_risk_level="LOW",
                task_priority="HIGH",
                log_summary=f"Prospect requested human agent. Connecting to {producer_name}.",
            )

        # 4. Complaints / Displeasure
        if any(re.search(pat, text) for pat in cls.COMPLAINT_PATTERNS):
            return EscalationDecision(
                escalation_type=EscalationType.COMPLAINT,
                spoken_transition=(
                    f"I completely understand, and I apologize for any frustration. Let me connect you "
                    f"with {producer_first_name} right away so we can take care of this."
                ),
                action="WARM_TRANSFER",
                requires_producer_transfer=True,
                eo_risk_level="MEDIUM",
                task_priority="URGENT",
                log_summary=f"Complaint detected. Transferring to {producer_name} and generating leadership alert.",
            )

        # 5. Confusion / Context Clarification
        if any(re.search(pat, text) for pat in cls.CONFUSION_PATTERNS):
            return EscalationDecision(
                escalation_type=EscalationType.CONFUSION,
                spoken_transition=(
                    f"No problem at all! This is Robie from StreetSmart Insurance. We're following up on "
                    f"the recent quote inquiry submitted for your insurance. If now isn't a good time, "
                    f"I can have {producer_first_name} send over an email or call back at a better time."
                ),
                action="CLARIFY_AND_OFFER_CALLBACK",
                requires_producer_transfer=False,
                eo_risk_level="LOW",
                task_priority="NORMAL",
                log_summary="Prospect expressed confusion. Provided agency context and offered async callback.",
            )

        return EscalationDecision(
            escalation_type=EscalationType.NONE,
            spoken_transition="",
            action="CONTINUE_CONVERSATION",
            requires_producer_transfer=False,
            eo_risk_level="LOW",
            task_priority="NORMAL",
            log_summary="Standard conversational flow.",
        )
