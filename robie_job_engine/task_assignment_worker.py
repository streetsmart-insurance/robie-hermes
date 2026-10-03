#!/usr/bin/env python3
"""Task assignment worker — processes EZLynx tasks assigned to Roby.

Polls the task report (via email CSV) and for each task assigned to
"Robie AI":
1. Acknowledges the task by posting a note to the applicant's discussion
2. Attempts to work the task based on its description
3. If the task can't be handled automatically, flags it for human review

Safety invariants:
- Never deletes tasks or applicants
- Never emails clients directly about tasks
- All actions are logged and auditable
- Fail-closed: unclear tasks are flagged, not guessed at
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .ezlynx_task_report import AssignedTask

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """Result of processing a single assigned task."""
    task_id: str
    applicant_id: str
    action: str  # "acknowledged", "worked", "flagged_for_human", "failed"
    detail: str
    timestamp: str


class TaskAssignmentWorker:
    """Processes tasks assigned to Roby from EZLynx."""

    def __init__(self, discussion_client=None):
        """Initialize with a DiscussionApiClient (or None for dry-run)."""
        self.client = discussion_client
        self.results: list[TaskResult] = []

    def process_tasks(self, tasks: list[AssignedTask]) -> list[TaskResult]:
        """Process a list of assigned tasks.

        For each task:
        1. Acknowledge by posting a note
        2. Try to categorize and work it
        3. Flag for human if unclear
        """
        for task in tasks:
            try:
                result = self._process_single(task)
                self.results.append(result)
            except Exception as e:
                logger.error(f"Failed to process task {task.task_id}: {e}")
                self.results.append(TaskResult(
                    task_id=task.task_id,
                    applicant_id=task.applicant_id,
                    action="failed",
                    detail=f"Error: {str(e)[:200]}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ))
        return self.results

    def _process_single(self, task: AssignedTask) -> TaskResult:
        """Process a single task: acknowledge and attempt to work it."""
        timestamp = datetime.now(timezone.utc).isoformat()

        # Step 1: Acknowledge the task
        ack_note = (
            f"Roby acknowledged task: {task.title}\n"
            f"Task ID: {task.task_id}\n"
            f"Received: {timestamp}\n"
            f"Status: Reviewing..."
        )
        self._post_note(task.applicant_id, ack_note)

        # Step 2: Categorize the task based on description
        category = self._categorize_task(task)

        if category == "unknown":
            # Flag for human review
            flag_note = (
                f"Roby needs help with this task.\n"
                f"Task: {task.title}\n"
                f"Description: {task.description[:500]}\n"
                f"Reason: Could not determine how to handle automatically.\n"
                f"Please review and assign to appropriate team member."
            )
            self._post_note(task.applicant_id, flag_note)
            return TaskResult(
                task_id=task.task_id,
                applicant_id=task.applicant_id,
                action="flagged_for_human",
                detail=f"Unclear task, flagged for review: {task.title[:80]}",
                timestamp=timestamp,
            )

        # Step 3: Attempt to work the task (placeholder for specific handlers)
        # For v1, we acknowledge and categorize. Specific task handlers
        # (follow-up calls, document requests, etc.) will be added incrementally.
        work_note = (
            f"Roby is working on: {task.title}\n"
            f"Category: {category}\n"
            f"Task ID: {task.task_id}"
        )
        self._post_note(task.applicant_id, work_note)

        return TaskResult(
            task_id=task.task_id,
            applicant_id=task.applicant_id,
            action="worked",
            detail=f"Categorized as {category}: {task.title[:80]}",
            timestamp=timestamp,
        )

    def _categorize_task(self, task: AssignedTask) -> str:
        """Categorize a task based on its title and description.

        Returns a category string, or "unknown" if unclear.
        """
        text = f"{task.title} {task.description}".lower()

        # Simple keyword-based categorization for v1
        # More sophisticated NLP can be added later
        if any(kw in text for kw in ["call", "phone", "callback", "reach out"]):
            return "callback"
        if any(kw in text for kw in ["document", "upload", "attach", "pdf"]):
            return "document"
        if any(kw in text for kw in ["quote", "premium", "price"]):
            return "quote"
        if any(kw in text for kw in ["follow up", "follow-up", "check status"]):
            return "follow_up"
        if any(kw in text for kw in ["email", "send"]):
            return "email"

        return "unknown"

    def _post_note(self, applicant_id: str, note_body: str) -> None:
        """Post a note to the applicant's discussion.

        If no client is configured (dry-run), logs instead of posting.
        """
        if self.client is None:
            logger.info(f"[DRY-RUN] Would post to applicant {applicant_id}: {note_body[:100]}...")
            return

        # Find or create a discussion for the applicant, then append the note
        # For v1, we use the existing discussion pattern
        try:
            discussions = self.client.get_discussions(applicant_id)
            if discussions:
                discussion_id = discussions[0].get('id')
                self.client.append_note(discussion_id, note_body)
                logger.info(f"Posted note to applicant {applicant_id}")
            else:
                logger.warning(f"No discussions found for applicant {applicant_id}")
        except Exception as e:
            logger.error(f"Failed to post note for {applicant_id}: {e}")
            raise
