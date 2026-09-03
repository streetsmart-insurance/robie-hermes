"""Rule-based and LLM-assisted Intent Classifier for Underwriter Replies."""

import re
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

class EmailClassification(BaseModel):
    intent: str  # QUOTE_ATTACHED, INFO_REQUESTED, NON_RENEWAL_DECLINED, ACKNOWLEDGED, UNKNOWN
    confidence: float
    summary: str
    action_needed: str
    has_quote_attachment: bool = False
    requested_items: List[str] = []

class UnderwriterIntentClassifier:
    """Classifies the content and sentiment of underwriter responses."""

    INFO_KEYWORDS = [
        r"loss runs?",
        r"signed app(?:lication)?",
        r"driver(?:'s)? license",
        r"payroll",
        r"sales figures?",
        r"updated exposure",
        r"questionnaire",
        r"supplemental",
        r"fill out",
        r"please provide"
    ]

    QUOTE_KEYWORDS = [
        r"attached is the renewal",
        r"please find the attached quote",
        r"renewal proposal",
        r"renewal terms",
        r"quote attached",
        r"see attached renewal",
        r"bound",
        r"renewal offer"
    ]

    DECLINE_KEYWORDS = [
        r"declined? to renew",
        r"unable to offer",
        r"non-?renewal notice",
        r"cannot write",
        r"market restriction",
        r"will not be renewing"
    ]

    ACK_KEYWORDS = [
        r"working on (?:it|this)",
        r"will send over",
        r"received",
        r"in review",
        r"by end of day",
        r"by tomorrow"
    ]

    def classify(
        self,
        subject: str,
        body_text: str,
        attachment_filenames: List[str]
    ) -> EmailClassification:
        clean_body = (body_text or "").lower()
        has_pdf = any(f.lower().endswith(".pdf") for f in attachment_filenames)

        # 1. Check for Decline / Non-Renewal (Highest Priority)
        for kw in self.DECLINE_KEYWORDS:
            if re.search(kw, clean_body):
                return EmailClassification(
                    intent="NON_RENEWAL_DECLINED",
                    confidence=0.95,
                    summary="Carrier is declining to renew or issuing non-renewal notice.",
                    action_needed="CRITICAL: Re-market policy immediately to secondary carriers.",
                    has_quote_attachment=False
                )

        # 2. Check for Information / Exposure Requests
        matched_items = []
        for kw in self.INFO_KEYWORDS:
            m = re.findall(kw, clean_body)
            if m:
                matched_items.extend(m)

        if matched_items:
            return EmailClassification(
                intent="INFO_REQUESTED",
                confidence=0.90,
                summary=f"Underwriter requested additional information: {', '.join(set(matched_items))}",
                action_needed="Notify Account Manager to supply requested documentation.",
                has_quote_attachment=has_pdf,
                requested_items=list(set(matched_items))
            )

        # 3. Check for Quote Attached or Provided
        if has_pdf:
            return EmailClassification(
                intent="QUOTE_ATTACHED",
                confidence=0.95,
                summary="Renewal quote proposal PDF received from underwriter.",
                action_needed="Parse quote PDF, upload to EZLynx, and notify Account Manager.",
                has_quote_attachment=True
            )

        for kw in self.QUOTE_KEYWORDS:
            if re.search(kw, clean_body):
                return EmailClassification(
                    intent="QUOTE_ATTACHED",
                    confidence=0.85,
                    summary="Underwriter provided renewal quote proposal / terms.",
                    action_needed="Parse quote terms, upload to EZLynx, and notify Account Manager.",
                    has_quote_attachment=False
                )

        # 4. Check for Acknowledged / In Review
        for kw in self.ACK_KEYWORDS:
            if re.search(kw, clean_body):
                return EmailClassification(
                    intent="ACKNOWLEDGED",
                    confidence=0.85,
                    summary="Underwriter acknowledged receipt and is preparing terms.",
                    action_needed="Post note to discussion and defer next follow-up by 5 days.",
                    has_quote_attachment=False
                )

        return EmailClassification(
            intent="UNKNOWN",
            confidence=0.50,
            summary="General underwriter response received.",
            action_needed="Review email thread manually in Gmail inbox.",
            has_quote_attachment=False
        )
