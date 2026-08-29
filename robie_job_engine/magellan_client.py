"""Magellan AI Phone & Conversational Intelligence Ingestion Engine.

Ingests real customer sentiment (Satisfied, Neutral, Sad/Frustrated),
intent tags (At-Risk, Cancellation, Claims, Billing, COI, Quotes),
and call transcripts directly from Magellan (https://app.magellan.insure).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MagellanCallRecord:
    """Represents a call analyzed by Magellan AI."""
    date_time: datetime
    from_phone: str
    to_phone: str
    duration_seconds: int
    sentiment: str  # "Satisfied", "Neutral", "Sad", "Frustrated"
    tags: List[str]
    is_handled: bool
    caller_name: Optional[str] = None
    transcript_summary: Optional[str] = None
    at_risk_flag: bool = False
    cancellation_flag: bool = False


class MagellanAuditor:
    """Analyzes Magellan sentiment metrics and correlates them with RingCentral and EZLynx."""

    @staticmethod
    def classify_sentiment(raw_sentiment: str) -> str:
        s = raw_sentiment.strip().lower()
        if "sad" in s or "frustrat" in s or "angry" in s or "negative" in s:
            return "SAD_FRUSTRATED"
        elif "neutral" in s:
            return "NEUTRAL"
        elif "satisf" in s or "positive" in s or "happy" in s:
            return "SATISFIED"
        return "UNKNOWN"

    @staticmethod
    def correlate_with_ringcentral(
        magellan_records: List[MagellanCallRecord],
        unreturned_missed_calls: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Matches unreturned missed calls with Magellan AI sentiment tags to flag critical churn risk."""
        enriched = []
        magellan_by_phone = {
            "".join(c for c in m.from_phone if c.isdigit())[-10:]: m
            for m in magellan_records
        }

        for call in unreturned_missed_calls:
            clean_phone = "".join(c for c in str(call.get("from", "")) if c.isdigit())[-10:]
            mag_record = magellan_by_phone.get(clean_phone)
            
            item = dict(call)
            if mag_record:
                item["magellan_sentiment"] = mag_record.sentiment
                item["magellan_tags"] = mag_record.tags
                item["magellan_at_risk"] = mag_record.at_risk_flag or "Cancellation" in mag_record.tags or "At-Risk Customer" in mag_record.tags
            else:
                item["magellan_sentiment"] = "UNRECORDED"
                item["magellan_tags"] = []
                item["magellan_at_risk"] = False
            
            enriched.append(item)
        return enriched
