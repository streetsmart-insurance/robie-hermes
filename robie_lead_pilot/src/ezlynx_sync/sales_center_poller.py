"""
Sales Center Poller & EZLynx Candidate Intake.

Extracts new leads, unreached opportunities, and completed quotes from EZLynx
using Portal API endpoints:
- /EZLynxPortalAPI/SalesCenter/Opportunity/GetOpportunitiesForApplicant
- /applicantportal/ApplicantContext/GetApplicantSidebar
- /Quote/GetCompletedQuote/{quote_id}
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.models.cadence_models import (
    ApplicantLead,
    ApplicantStatus,
    ContactConsent,
    Opportunity,
    OpportunityStage,
    QuoteSummary,
)

logger = logging.getLogger("sales_center_poller")


class SalesCenterPoller:
    def __init__(self, ezlynx_client: Optional[Any] = None):
        self.ezlynx_client = ezlynx_client

    def parse_opportunity_payload(self, raw_opp: Dict[str, Any], applicant_id: str) -> Opportunity:
        opp_id = str(raw_opp.get("opportunityId") or raw_opp.get("id") or "UNKNOWN_OPP")
        lob = raw_opp.get("lineOfBusiness") or raw_opp.get("lob") or "Personal Auto"
        stage_str = raw_opp.get("status") or raw_opp.get("stage") or "New"
        producer = raw_opp.get("producerName") or raw_opp.get("assignedTo") or "Jake Ferrara"

        stage = OpportunityStage.NEW
        for st in OpportunityStage:
            if st.value.lower() == stage_str.strip().lower():
                stage = st
                break

        return Opportunity(
            opportunity_id=opp_id,
            applicant_id=applicant_id,
            line_of_business=lob,
            stage=stage,
            producer_name=producer,
        )

    def extract_lead_from_applicant_data(
        self,
        applicant_id: str,
        classic_applicant: Dict[str, Any],
        sidebar_data: Optional[Dict[str, Any]] = None,
        lead_source: str = "StreetSmart Website",
    ) -> ApplicantLead:
        first_name = (
            classic_applicant.get("FirstName")
            or classic_applicant.get("firstName")
            or "Valued"
        )
        last_name = (
            classic_applicant.get("LastName")
            or classic_applicant.get("lastName")
            or "Client"
        )
        phone = (
            classic_applicant.get("CellPhone")
            or classic_applicant.get("Phone")
            or classic_applicant.get("cellPhone")
            or ""
        )
        email = (
            classic_applicant.get("Email")
            or classic_applicant.get("email")
        )
        producer = "Jake Ferrara"
        if sidebar_data:
            assigned = (
                sidebar_data.get("Applicant", {})
                .get("Assignment", {})
                .get("AssignedTo")
            )
            if assigned:
                producer = assigned

        return ApplicantLead(
            applicant_id=applicant_id,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            email=email,
            lead_source=lead_source,
            assigned_producer=producer,
            client_status=ApplicantStatus.PROSPECT_LEAD,
            consent=ContactConsent(voice_consent=True, sms_consent=True, email_consent=True),
        )
