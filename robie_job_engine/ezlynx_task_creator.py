"""EZLynx Automated Task Creation Module for Unreturned Client Calls.

Automatically identifies active EZLynx clients with orphaned voicemails/missed calls
and generates high-priority service tasks assigned to the appropriate CSR or Department Lead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EZLynxTaskPayload:
    """Payload structure for creating an EZLynx Task."""
    applicant_id: str
    assigned_user: str
    task_title: str
    task_description: str
    due_date: str
    priority: str = "High"  # "High", "Medium", "Low"
    task_category: str = "Client Service - Phone Callback"


class EZLynxTaskEngine:
    """Manages creation of SLA breach tasks inside EZLynx."""

    DEPARTMENT_LEADS = {
        "Commercial Queue": "Zeus Quezada",
        "Commercial": "Zeus Quezada",
        "Trucking Queue": "Ricardo Aguilar",
        "Trucking": "Ricardo Aguilar",
        "Personal Lines": "Diana Cabrera",
        "Personal Lines Queue": "Diana Cabrera",
        "Spanish Commercial": "Andrea Illanes",
    }

    @classmethod
    def generate_task_payload(
        cls,
        unreturned_incident: Dict[str, Any],
        applicant_id: str,
        assigned_csr: Optional[str] = None,
        assigned_producer: Optional[str] = None,
    ) -> EZLynxTaskPayload:
        """Constructs a structured task payload for an active EZLynx client."""
        caller_name = unreturned_incident.get("name") or "Unknown Client"
        phone = unreturned_incident.get("phone") or ""
        call_time = unreturned_incident.get("time") or ""
        call_date = unreturned_incident.get("date") or ""
        rep_or_queue = unreturned_incident.get("rep_or_queue") or "General Queue"
        duration = unreturned_incident.get("duration") or "0m"

        # Determine task assignee
        assignee = assigned_csr or assigned_producer
        if not assignee:
            assignee = cls.DEPARTMENT_LEADS.get(rep_or_queue, "Carlo Ferrara")

        title = f"🚨 URGENT CALLBACK: {caller_name} ({phone})"
        desc = (
            f"Automated ROBIE Alert: Client called on {call_date} at {call_time} "
            f"and left a voicemail ({duration}) on '{rep_or_queue}'. "
            f"No return dial or EZLynx discussion note was detected within SLA. "
            f"Please call the insured back immediately and log a discussion note."
        )

        today_str = datetime.now(timezone.utc).strftime("%m/%d/%Y")

        return EZLynxTaskPayload(
            applicant_id=applicant_id,
            assigned_user=assignee,
            task_title=title,
            task_description=desc,
            due_date=today_str,
            priority="High",
            task_category="Client Service - Phone Callback",
        )
