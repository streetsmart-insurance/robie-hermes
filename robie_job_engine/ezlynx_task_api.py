"""Direct EZLynx Task API via TaskCreationNote.

Replaces the Zapier-based task creation with direct API calls using
the Discussion API's TaskCreationNote type, per EZLynx support guidance
(Gazala Mujawar, ticket #2034799).

Supported operations:
- Create new discussion with a TaskCreationNote (POST /v8/discussions/with-note)
- Add TaskCreationNote to existing discussion (POST /v8/discussions/:id/notes)

TaskCreationNote fields:
- applicantId: root-level applicant ID (integer)
- assignedUserId: integer user ID of assignee
- dueDate: ISO 8601 datetime
- priority: High/Medium/Low
- description: task description text
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class TaskCreationRequest:
    """Request to create an EZLynx task via TaskCreationNote."""
    applicant_id: int
    assigned_user_id: int
    title: str
    description: str
    due_date: datetime
    priority: str = "High"  # High, Medium, Low

    def to_task_creation_note(self) -> Dict[str, Any]:
        """Convert to TaskCreationNote JSON structure."""
        return {
            "noteType": "TaskCreationNote",
            "applicantId": self.applicant_id,
            "assignedUserId": self.assigned_user_id,
            "title": self.title,
            "description": self.description,
            "dueDate": self.due_date.isoformat(),
            "priority": self.priority,
        }


class EZLynxTaskAPI:
    """Direct EZLynx task creation via Discussion API TaskCreationNote."""

    def __init__(self, discussion_client):
        """Initialize with a DiscussionApiClient instance."""
        self.client = discussion_client

    def create_task_new_discussion(
        self, request: TaskCreationRequest
    ) -> Dict[str, Any]:
        """Create a new discussion with a TaskCreationNote.
        
        POST /v8/discussions/with-note
        """
        payload = {
            "applicantId": request.applicant_id,
            "note": request.to_task_creation_note(),
        }
        logger.info(
            "Creating task via new discussion for applicant %s",
            request.applicant_id,
        )
        return self.client._post("v8/discussions/with-note", payload)

    def create_task_existing_discussion(
        self, discussion_id: str, request: TaskCreationRequest
    ) -> Dict[str, Any]:
        """Add a TaskCreationNote to an existing discussion.
        
        POST /v8/discussions/:discussionId/notes
        """
        payload = request.to_task_creation_note()
        logger.info(
            "Creating task in discussion %s for applicant %s",
            discussion_id,
            request.applicant_id,
        )
        return self.client._post(
            f"v8/discussions/{discussion_id}/notes", payload
        )

    def create_callback_task(
        self,
        applicant_id: int,
        assigned_user_id: int,
        caller_name: str,
        caller_phone: str,
        call_time: datetime,
        due_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Create a standard callback task for a missed call/voicemail.
        
        Convenience method for the common phone callback use case.
        """
        if due_date is None:
            # Default: next business day at 10 AM
            due_date = datetime.now(timezone.utc).replace(
                hour=10, minute=0, second=0, microsecond=0
            )

        request = TaskCreationRequest(
            applicant_id=applicant_id,
            assigned_user_id=assigned_user_id,
            title=f"Call back {caller_name}",
            description=(
                f"{caller_name} called from {caller_phone} on "
                f"{call_time.strftime('%m/%d/%Y at %I:%M %p')}. "
                f"Please return the call."
            ),
            due_date=due_date,
            priority="High",
        )
        return self.create_task_new_discussion(request)
