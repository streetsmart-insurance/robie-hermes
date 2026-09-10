"""
Data models and schemas for Robie's Sales Center and Bland AI lead cadence engine.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


class ApplicantStatus(str, enum.Enum):
    PROSPECT_LEAD = "ProspectLead"
    ACTIVE_CLIENT = "ActiveClient"
    INACTIVE_CLIENT = "InactiveClient"
    UNKNOWN = "Unknown"


class OpportunityStage(str, enum.Enum):
    NEW = "New"
    UNREACHED = "Unreached"
    CONTACTED = "Contacted"
    QUOTED = "Quoted"
    WON = "Won"
    LOST = "Lost"
    DEAD = "Dead"
    CLOSED = "Closed"


class CadenceType(str, enum.Enum):
    INBOUND_LEAD = "INBOUND_LEAD"
    QUOTED_PROSPECT = "QUOTED_PROSPECT"
    XDATE_OPPORTUNITY = "XDATE_OPPORTUNITY"


class CadenceStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"


class ChannelType(str, enum.Enum):
    VOICE = "VOICE"
    SMS = "SMS"
    EMAIL = "EMAIL"
    ALL = "ALL"


class StopReason(str, enum.Enum):
    WON_BOUND = "WON_BOUND"
    LOST_CLOSED = "LOST_CLOSED"
    DEAD = "DEAD"
    OPT_OUT_CALL = "OPT_OUT_CALL"
    OPT_OUT_SMS_STOP = "OPT_OUT_SMS_STOP"
    OPT_OUT_EMAIL = "OPT_OUT_EMAIL"
    EZLYNX_NOTE_KEYWORD = "EZLYNX_NOTE_KEYWORD"
    EMPLOYEE_PAUSE = "EMPLOYEE_PAUSE"
    WRONG_NUMBER = "WRONG_NUMBER"
    SUPPRESSION_MATCH = "SUPPRESSION_MATCH"
    DISPOSITION_NOT_INTERESTED = "DISPOSITION_NOT_INTERESTED"


def hash_identifier(identifier: Optional[str]) -> Optional[str]:
    """Generates a consistent SHA-256 hash for phone or email suppression."""
    if not identifier:
        return None
    cleaned = identifier.strip().lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


@dataclass
class ContactConsent:
    voice_consent: bool = True
    sms_consent: bool = True
    email_consent: bool = True
    consent_timestamp: Optional[datetime] = None
    consent_source: str = "WebForm_TCPA"

    def has_consent(self, channel: ChannelType) -> bool:
        if channel == ChannelType.VOICE:
            return self.voice_consent
        if channel == ChannelType.SMS:
            return self.sms_consent
        if channel == ChannelType.EMAIL:
            return self.email_consent
        if channel == ChannelType.ALL:
            return self.voice_consent and self.sms_consent and self.email_consent
        return False


@dataclass
class ApplicantLead:
    applicant_id: str
    first_name: str
    last_name: str
    phone: str
    email: Optional[str] = None
    business_name: Optional[str] = None
    lead_source: str = "StreetSmart Website"
    assigned_producer: str = "Jake Ferrara"
    assigned_producer_phone: Optional[str] = "+17324812520"
    client_status: ApplicantStatus = ApplicantStatus.PROSPECT_LEAD
    state: str = "NJ"
    zip_code: Optional[str] = "07728"
    consent: ContactConsent = field(default_factory=ContactConsent)
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def spoken_first_name(self) -> str:
        name = self.first_name.strip()
        # Handle compound or title prefixes gracefully
        parts = name.split()
        return parts[0] if parts else name


@dataclass
class Opportunity:
    opportunity_id: str
    applicant_id: str
    line_of_business: str
    stage: OpportunityStage = OpportunityStage.NEW
    producer_name: str = "Jake Ferrara"
    expiration_date: Optional[date] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class QuoteSummary:
    quote_id: str
    opportunity_id: str
    applicant_id: str
    carrier_name: str
    line_of_business: str
    quoted_premium: Optional[float] = None
    quote_date: Optional[date] = None
    verified_expiration_date: Optional[date] = None
    verified_rate_guarantee_date: Optional[date] = None


@dataclass
class GlobalSuppressionRecord:
    record_id: str
    phone_hash: Optional[str] = None
    email_hash: Optional[str] = None
    applicant_id: Optional[str] = None
    channel: ChannelType = ChannelType.ALL
    reason: StopReason = StopReason.OPT_OUT_CALL
    source_workflow: CadenceType = CadenceType.INBOUND_LEAD
    created_at: datetime = field(default_factory=datetime.utcnow)
    notes: Optional[str] = None

    @classmethod
    def create(
        cls,
        record_id: str,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        applicant_id: Optional[str] = None,
        channel: ChannelType = ChannelType.ALL,
        reason: StopReason = StopReason.OPT_OUT_CALL,
        source_workflow: CadenceType = CadenceType.INBOUND_LEAD,
        notes: Optional[str] = None,
    ) -> GlobalSuppressionRecord:
        return cls(
            record_id=record_id,
            phone_hash=hash_identifier(phone),
            email_hash=hash_identifier(email),
            applicant_id=str(applicant_id) if applicant_id else None,
            channel=channel,
            reason=reason,
            source_workflow=source_workflow,
            notes=notes,
        )


@dataclass
class CadenceEnrollment:
    enrollment_id: str
    applicant_id: str
    opportunity_id: str
    cadence_type: CadenceType
    current_touch: int = 0
    status: CadenceStatus = CadenceStatus.ACTIVE
    stop_reason: Optional[StopReason] = None
    last_touch_at: Optional[datetime] = None
    next_touch_due: Optional[datetime] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
