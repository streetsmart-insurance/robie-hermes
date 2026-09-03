"""SQLAlchemy Database Models for Renewal Lifecycle."""

import enum
from datetime import datetime, date
from typing import Optional, List
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Date, Boolean, Text, ForeignKey, Enum
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class RenewalStatus(str, enum.Enum):
    PENDING_EVALUATION = "PENDING_EVALUATION"
    CHECKING_PORTAL = "CHECKING_PORTAL"
    PORTAL_QUOTE_FOUND = "PORTAL_QUOTE_FOUND"
    PORTAL_UNAVAILABLE = "PORTAL_UNAVAILABLE"
    OUTREACH_PENDING = "OUTREACH_PENDING"
    EMAIL_SENT_AWAITING_REPLY = "EMAIL_SENT_AWAITING_REPLY"
    FOLLOWUP_SENT = "FOLLOWUP_SENT"
    REPLY_RECEIVED = "REPLY_RECEIVED"
    QUOTE_RECEIVED = "QUOTE_RECEIVED"
    INFO_REQUESTED = "INFO_REQUESTED"
    NON_RENEWAL_DECLINED = "NON_RENEWAL_DECLINED"
    UPLOADED_TO_EZLYNX = "UPLOADED_TO_EZLYNX"
    READY_FOR_AGENT_REVIEW = "READY_FOR_AGENT_REVIEW"
    ESCALATED_MANUAL = "ESCALATED_MANUAL"
    COMPLETED = "COMPLETED"

class ThreadStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    FOLLOWUP_DUE = "FOLLOWUP_DUE"
    REPLIED = "REPLIED"
    RESOLVED = "RESOLVED"
    EXHAUSTED = "EXHAUSTED"

class ActionType(str, enum.Enum):
    PORTAL_CHECK = "PORTAL_CHECK"
    INITIAL_EMAIL_SENT = "INITIAL_EMAIL_SENT"
    FOLLOWUP_EMAIL_SENT = "FOLLOWUP_EMAIL_SENT"
    UNDERWRITER_REPLIED = "UNDERWRITER_REPLIED"
    QUOTE_ATTACHED = "QUOTE_ATTACHED"
    EZLYNX_NOTE_ADDED = "EZLYNX_NOTE_ADDED"
    EZLYNX_TASK_CREATED = "EZLYNX_TASK_CREATED"
    STATUS_CHANGE = "STATUS_CHANGE"

class PolicyRenewal(Base):
    __tablename__ = "policy_renewals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_number = Column(String(100), nullable=False, index=True)
    insured_name = Column(String(255), nullable=False, index=True)
    applicant_id = Column(String(100), nullable=False, index=True)  # EZLynx Applicant ID
    discussion_title = Column(String(255), nullable=False, default="Manual Homeowners Renewal")
    line_of_business = Column(String(100), nullable=True, default="Homeowners")
    carrier_name = Column(String(150), nullable=False, index=True)
    
    effective_date = Column(Date, nullable=True)
    expiration_date = Column(Date, nullable=False, index=True)
    
    expiring_premium = Column(Float, nullable=True)
    renewal_premium = Column(Float, nullable=True)
    premium_change_pct = Column(Float, nullable=True)
    
    underwriter_name = Column(String(150), nullable=True)
    underwriter_email = Column(String(255), nullable=True)
    assigned_agent = Column(String(150), nullable=True)
    
    status = Column(Enum(RenewalStatus), default=RenewalStatus.PENDING_EVALUATION, nullable=False, index=True)
    portal_supported = Column(Boolean, default=False)
    portal_url = Column(String(255), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    threads = relationship("OutreachThread", back_populates="policy", cascade="all, delete-orphan")
    notes = relationship("AuditNoteLog", back_populates="policy", cascade="all, delete-orphan")
    documents = relationship("DocumentRecord", back_populates="policy", cascade="all, delete-orphan")

class OutreachThread(Base):
    __tablename__ = "outreach_threads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_id = Column(Integer, ForeignKey("policy_renewals.id"), nullable=False, index=True)
    tracking_code = Column(String(100), unique=True, nullable=False, index=True)  # e.g. RENEWAL-REQ-10023
    
    gmail_thread_id = Column(String(100), nullable=True, index=True)
    last_message_id = Column(String(255), nullable=True)
    recipient_email = Column(String(255), nullable=False)
    subject_line = Column(String(500), nullable=False)
    
    initial_sent_at = Column(DateTime, nullable=True)
    last_followup_at = Column(DateTime, nullable=True)
    followup_count = Column(Integer, default=0, nullable=False)
    next_followup_due = Column(Date, nullable=True, index=True)
    
    status = Column(Enum(ThreadStatus), default=ThreadStatus.ACTIVE, nullable=False)
    latest_reply_summary = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    policy = relationship("PolicyRenewal", back_populates="threads")

class AuditNoteLog(Base):
    __tablename__ = "audit_note_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_id = Column(Integer, ForeignKey("policy_renewals.id"), nullable=False, index=True)
    applicant_id = Column(String(100), nullable=False, index=True)
    discussion_title = Column(String(255), nullable=False)
    action_type = Column(Enum(ActionType), nullable=False)
    
    note_text = Column(Text, nullable=False)
    synced_to_ezlynx = Column(Boolean, default=False, nullable=False)
    ezlynx_note_id = Column(String(100), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    policy = relationship("PolicyRenewal", back_populates="notes")

class DocumentRecord(Base):
    __tablename__ = "document_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_id = Column(Integer, ForeignKey("policy_renewals.id"), nullable=False, index=True)
    
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_size_bytes = Column(Integer, nullable=True)
    source = Column(String(50), nullable=False)  # 'CARRIER_PORTAL' or 'UNDERWRITER_EMAIL'
    
    extracted_premium = Column(Float, nullable=True)
    extracted_effective_date = Column(Date, nullable=True)
    extracted_summary = Column(Text, nullable=True)
    uploaded_to_ezlynx = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    policy = relationship("PolicyRenewal", back_populates="documents")
