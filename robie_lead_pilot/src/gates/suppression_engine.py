"""
Global Suppression Record & Cross-Workflow Opt-Out Ledger for Robie.

Maintains an immutable, persistent registry of suppressed prospects, phone numbers,
and emails. Checked before every outreach attempt across all Robie workflows.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.models.cadence_models import (
    CadenceType,
    ChannelType,
    GlobalSuppressionRecord,
    StopReason,
    hash_identifier,
)

logger = logging.getLogger("suppression_engine")


class SuppressionEngine:
    def __init__(self, persistence_file: Optional[Path] = None):
        self.persistence_file = persistence_file or Path("data/suppression_registry.json")
        self._phone_hash_index: Dict[str, List[GlobalSuppressionRecord]] = {}
        self._email_hash_index: Dict[str, List[GlobalSuppressionRecord]] = {}
        self._applicant_id_index: Dict[str, List[GlobalSuppressionRecord]] = {}
        self._records: List[GlobalSuppressionRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.persistence_file.exists():
            return
        try:
            with open(self.persistence_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            for item in raw_data:
                record = GlobalSuppressionRecord(
                    record_id=item["record_id"],
                    phone_hash=item.get("phone_hash"),
                    email_hash=item.get("email_hash"),
                    applicant_id=item.get("applicant_id"),
                    channel=ChannelType(item.get("channel", "ALL")),
                    reason=StopReason(item.get("reason", "OPT_OUT_CALL")),
                    source_workflow=CadenceType(item.get("source_workflow", "INBOUND_LEAD")),
                    notes=item.get("notes"),
                )
                self._index_record(record)
            logger.info("Loaded %d suppression records from %s", len(self._records), self.persistence_file)
        except Exception as e:
            logger.warning("Could not load suppression registry from %s: %s", self.persistence_file, e)

    def _save(self) -> None:
        try:
            self.persistence_file.parent.mkdir(parents=True, exist_ok=True)
            serializable = [
                {
                    "record_id": r.record_id,
                    "phone_hash": r.phone_hash,
                    "email_hash": r.email_hash,
                    "applicant_id": r.applicant_id,
                    "channel": r.channel.value,
                    "reason": r.reason.value,
                    "source_workflow": r.source_workflow.value,
                    "created_at": r.created_at.isoformat(),
                    "notes": r.notes,
                }
                for r in self._records
            ]
            temp_file = self.persistence_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
            os.replace(temp_file, self.persistence_file)
        except Exception as e:
            logger.error("Failed to save suppression registry to %s: %s", self.persistence_file, e)

    def _index_record(self, record: GlobalSuppressionRecord) -> None:
        self._records.append(record)
        if record.phone_hash:
            self._phone_hash_index.setdefault(record.phone_hash, []).append(record)
        if record.email_hash:
            self._email_hash_index.setdefault(record.email_hash, []).append(record)
        if record.applicant_id:
            self._applicant_id_index.setdefault(str(record.applicant_id), []).append(record)

    def add_suppression(
        self,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        applicant_id: Optional[str] = None,
        channel: ChannelType = ChannelType.ALL,
        reason: StopReason = StopReason.OPT_OUT_CALL,
        source_workflow: CadenceType = CadenceType.INBOUND_LEAD,
        notes: Optional[str] = None,
    ) -> GlobalSuppressionRecord:
        """
        Adds a new global suppression record and persists it immediately.
        """
        record_id = f"SUPP-{uuid.uuid4().hex[:12].upper()}"
        record = GlobalSuppressionRecord.create(
            record_id=record_id,
            phone=phone,
            email=email,
            applicant_id=applicant_id,
            channel=channel,
            reason=reason,
            source_workflow=source_workflow,
            notes=notes,
        )
        self._index_record(record)
        self._save()
        logger.info(
            "🛑 Added Global Suppression Record %s: applicant_id=%s reason=%s channel=%s source=%s",
            record.record_id,
            applicant_id,
            reason.value,
            channel.value,
            source_workflow.value,
        )
        return record

    def is_suppressed(
        self,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        applicant_id: Optional[str] = None,
        channel: ChannelType = ChannelType.ALL,
    ) -> Tuple[bool, Optional[GlobalSuppressionRecord]]:
        """
        Checks if the candidate is globally suppressed for the requested channel.
        """
        # 1. Check applicant_id
        if applicant_id and str(applicant_id) in self._applicant_id_index:
            for rec in self._applicant_id_index[str(applicant_id)]:
                if rec.channel in (ChannelType.ALL, channel):
                    return True, rec

        # 2. Check phone hash
        if phone:
            p_hash = hash_identifier(phone)
            if p_hash and p_hash in self._phone_hash_index:
                for rec in self._phone_hash_index[p_hash]:
                    if rec.channel in (ChannelType.ALL, channel):
                        return True, rec

        # 3. Check email hash
        if email:
            e_hash = hash_identifier(email)
            if e_hash and e_hash in self._email_hash_index:
                for rec in self._email_hash_index[e_hash]:
                    if rec.channel in (ChannelType.ALL, channel):
                        return True, rec

        return False, None

    def clear(self) -> None:
        """Clears in-memory records and persistence file (used for test isolation)."""
        self._records.clear()
        self._phone_hash_index.clear()
        self._email_hash_index.clear()
        self._applicant_id_index.clear()
        if self.persistence_file.exists():
            try:
                self.persistence_file.unlink()
            except OSError:
                pass
