"""
SMS Dispatcher for Robie Multi-Channel Cadences.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.models.cadence_models import ApplicantLead, CadenceType, Opportunity
from src.scripts.email_scripts import EmailAndSMSScriptBuilder, SMSPackage

logger = logging.getLogger("sms_dispatcher")


class SMSDispatcher:
    def __init__(self, from_number: str = "+17322986745"):
        self.from_number = from_number

    def dispatch(
        self,
        lead: ApplicantLead,
        opportunity: Opportunity,
        cadence_type: CadenceType,
        touch_number: int,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        sms_pkg = EmailAndSMSScriptBuilder.build_sms(
            cadence_type=cadence_type,
            touch_number=touch_number,
            lead=lead,
            opportunity=opportunity,
        )

        if dry_run:
            logger.info(
                "[DRY RUN] Simulated SMS to %s: '%s' (Tracking: %s)",
                lead.phone,
                sms_pkg.body_text,
                sms_pkg.tracking_code,
            )
            return {
                "success": True,
                "mode": "DRY_RUN",
                "recipient": lead.phone,
                "body": sms_pkg.body_text,
                "tracking_code": sms_pkg.tracking_code,
            }

        return {
            "success": True,
            "mode": "LIVE_SIMULATED",
            "recipient": lead.phone,
            "body": sms_pkg.body_text,
            "tracking_code": sms_pkg.tracking_code,
        }
