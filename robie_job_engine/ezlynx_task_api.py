"""EZLynx Task Creation via Discussion API TaskCreationNote.

Creates EZLynx follow-up tasks directly through the Discussion API,
replacing the Zapier catch-hook path.

Based on EZLynx Web Services confirmation (2026-10-02):
- Tasks are created as embedded TaskCreationNote objects via:
  POST /v8/discussions/:discussionId/notes
- applicantId at root level, assignedUserId (integer) in task object
- Discussion API scope only (already have it)
- Target org 36748

The assignee is dynamic: pass assigned_user_id directly, or use
get_applicant_assignee() to look up the applicant's assigned producer/CSR.
Due date and time are selectable via due_date (YYYY-MM-DD) and
due_time (HH:MM 24h) parameters.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class EZLynxTaskApiError(RuntimeError):
    """Task creation failed."""


def create_task(
    *,
    applicant_id: str,
    title: str,
    description: str = "",
    assigned_user_id: Optional[int] = None,
    assigned_user_name: Optional[str] = None,
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
    priority: str = "Medium",
    discussion_title_hint: Optional[str] = None,
) -> dict[str, Any]:
    """Create an EZLynx task via TaskCreationNote on the applicant's discussion.
    
    This is the main entry point. It finds the applicant's discussion,
    resolves the assignee, and posts a TaskCreationNote.
    
    Args:
        applicant_id: EZLynx applicant ID
        title: Task title (required)
        description: Task description/body
        assigned_user_id: Integer EZLynx user ID (preferred if known)
        assigned_user_name: Name to look up if ID not provided
        due_date: Due date as YYYY-MM-DD (e.g., "2026-10-05")
        due_time: Due time as HH:MM 24h (e.g., "14:30"), defaults to 17:00
        priority: "High", "Medium", or "Low"
        discussion_title_hint: Hint to find the right discussion
    
    Returns:
        Dict with status, task details, and verification info
    """
    from .ezlynx_api_only_writes import add_note_to_discussion
    
    applicant = str(applicant_id or "").strip()
    task_title = str(title or "").strip()
    
    if not applicant:
        raise EZLynxTaskApiError("applicant_id is required")
    if not task_title:
        raise EZLynxTaskApiError("title is required")
    
    # Resolve assignee to integer user ID
    user_id = assigned_user_id
    if user_id is None and assigned_user_name:
        user_id = lookup_user_id_by_name(assigned_user_name)
    if user_id is None:
        # Try to get applicant's assigned producer/CSR
        assignee = get_applicant_assignee(applicant)
        if assignee and assignee.get("user_id"):
            user_id = int(assignee["user_id"])
    
    if user_id is None:
        raise EZLynxTaskApiError(
            "Could not resolve assignee. Provide assigned_user_id or "
            "assigned_user_name, or ensure applicant has an assigned producer/CSR."
        )
    
    # Build due datetime
    due_at = _build_due_datetime(due_date, due_time)
    
    # Build the TaskCreationNote payload per EZLynx Postman docs:
    # https://documenter.getpostman.com/view/56716523/2sBY4Wpcsh
    # Task fields: "due" (ISO datetime), "assignedUserId" (int),
    # "isImportant" (bool). There is no "priority" field.
    task_obj: dict[str, Any] = {
        "assignedUserId": int(user_id),
        "isImportant": str(priority).lower() == "high",
    }
    if description:
        task_obj["description"] = str(description).strip()
    if due_at:
        task_obj["due"] = due_at
    
    # Post via the working discussion API path
    # Note: add_note_to_discussion uses note_type parameter
    # For TaskCreationNote, we need to pass the task object
    # This is done via the discussion_client directly
    
    from .ezlynx_discussions import (
        DiscussionApiClient,
        file_note_to_existing_discussion,
    )
    from .ezlynx_api_only_writes import _discussion_config_from_secret
    
    client = DiscussionApiClient(_discussion_config_from_secret())
    
    # Build the full TaskCreationNote payload
    note_payload = {
        "type": "TaskCreationNote",
        "applicantId": applicant,
        "body": task_title,
        "task": task_obj,
    }
    
    # Find the discussion and post
    # Use file_note_to_existing_discussion with custom payload
    logger.info(
        "Creating task via TaskCreationNote: applicant=%s assignee=%s title=%s",
        applicant, user_id, task_title,
    )
    
    # Get discussions for applicant
    discussions = client.get_discussions(applicant)
    if not discussions:
        raise EZLynxTaskApiError(f"No discussions found for applicant {applicant}")
    
    # Pick the discussion (use hint or first)
    discussion_id = _select_discussion(discussions, discussion_title_hint)
    
    # Post the TaskCreationNote
    result = client._post(
        f"v8/discussions/{discussion_id}/notes",
        note_payload,
    )
    
    return {
        "status": "created",
        "applicant_id": applicant,
        "discussion_id": discussion_id,
        "assigned_user_id": int(user_id),
        "title": task_title,
        "due_date": due_date,
        "due_time": due_time,
        "priority": priority,
        "api_result": result,
    }


def _select_discussion(
    discussions: list[dict[str, Any]],
    hint: Optional[str],
) -> str:
    """Pick the best discussion from the list."""
    if not discussions:
        raise EZLynxTaskApiError("No discussions available")
    
    if hint:
        hint_lower = hint.lower()
        for d in discussions:
            title = str(d.get("title", "")).lower()
            if hint_lower in title:
                return str(d.get("id"))
    
    # Default to first discussion
    return str(discussions[0].get("id"))


def lookup_user_id_by_name(user_name: str) -> Optional[int]:
    """Look up EZLynx integer user ID by name.
    
    Returns None if not found. The lookup uses the Discussion API
    user search endpoint.
    """
    name = str(user_name or "").strip()
    if not name:
        return None
    
    try:
        from .ezlynx_discussions import DiscussionApiClient
        from .ezlynx_api_only_writes import _discussion_config_from_secret
        
        client = DiscussionApiClient(_discussion_config_from_secret())
        result = client._get("v8/users/search", {"name": name})
        
        if isinstance(result, list) and result:
            user = result[0]
            if isinstance(user, dict):
                uid = user.get("id") or user.get("userId") or user.get("user_id")
                if uid:
                    return int(uid)
        elif isinstance(result, dict):
            users = result.get("users") or result.get("items") or []
            if users and isinstance(users[0], dict):
                uid = users[0].get("id") or users[0].get("userId")
                if uid:
                    return int(uid)
    except Exception as e:
        logger.warning("User lookup failed for '%s': %s", name, e)
    
    return None


def get_applicant_assignee(applicant_id: str) -> Optional[dict[str, Any]]:
    """Get the applicant's assigned producer or CSR.
    
    Returns dict with 'user_id', 'name', 'role' or None if not found.
    The role indicates whether they're the producer or CSR.
    """
    # TODO: Implement via Applicant API
    # The applicant record should contain assigned producer/CSR info
    # For now, return None to indicate manual assignee is needed
    logger.debug("Applicant assignee lookup for %s not yet implemented", applicant_id)
    return None


def _build_due_datetime(
    due_date: Optional[str],
    due_time: Optional[str],
) -> Optional[str]:
    """Build due date string for EZLynx TaskCreationNote.
    
    Args:
        due_date: YYYY-MM-DD format
        due_time: HH:MM format (24h), defaults to 22:00 (10 PM)
    
    Returns:
        YYYY-MM-DD date string or None.
        Note: EZLynx TaskCreationNote expects date-only format for dueDate.
        Time component is not supported in the current API.
    """
    if not due_date:
        return None
    
    date_str = str(due_date).strip()
    
    try:
        # Validate and return full ISO datetime with timezone
        # Per Postman docs, "due" expects a datetime like "2023-06-27T13:07:24.267+00:00"
        time_str = str(due_time).strip() if due_time else "22:00"
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError as e:
        logger.warning("Invalid due date/time '%s': %s", date_str, e)
        return None
