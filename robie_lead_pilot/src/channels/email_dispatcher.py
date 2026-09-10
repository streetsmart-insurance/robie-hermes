"""
Email Dispatcher for Robie Multi-Channel Cadences.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.models.cadence_models import ApplicantLead, CadenceType, Opportunity, QuoteSummary
from src.scripts.email_scripts import EmailAndSMSScriptBuilder, EmailPackage

logger = logging.getLogger("email_dispatcher")


class EmailDispatcher:
    def __init__(self, sender_email: str = "team@streetsmart.insurance"):
        self.sender_email = sender_email

    def dispatch(
        self,
        lead: ApplicantLead,
        opportunity: Opportunity,
        cadence_type: CadenceType,
        touch_number: int,
        quote: Optional[QuoteSummary] = None,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        if not lead.email:
            return {"success": False, "error": "MISSING_EMAIL", "applicant_id": lead.applicant_id}

        email_pkg = EmailAndSMSScriptBuilder.build_email(
            cadence_type=cadence_type,
            touch_number=touch_number,
            lead=lead,
            opportunity=opportunity,
            quote=quote,
        )

        if dry_run:
            logger.info(
                "[DRY RUN] Simulated Email '%s' to %s (Tracking: %s)",
                email_pkg.subject,
                lead.email,
                email_pkg.tracking_code,
            )
            return {
                "success": True,
                "mode": "DRY_RUN",
                "recipient": lead.email,
                "subject": email_pkg.subject,
                "tracking_code": email_pkg.tracking_code,
                "package": email_pkg,
            }

        # In production VM, hooks into Gmail client / API
        return {
            "success": True,
            "mode": "LIVE_SIMULATED",
            "recipient": lead.email,
            "subject": email_pkg.subject,
            "tracking_code": email_pkg.tracking_code,
        }
