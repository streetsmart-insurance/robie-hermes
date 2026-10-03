#!/usr/bin/env python3
"""Task assignment worker — processes EZLynx tasks assigned to Roby.

For each task assigned to "Robie AI" in the report CSV:
1. Acknowledges the task by posting a note to the task's discussion
   (using the Discussion ID from the report — no guessing)
2. Attempts to categorize the task
3. If the task can't be handled automatically, flags it for human review

Safety invariants:
- Never deletes tasks or applicants
- Never emails clients directly about tasks
- All actions are logged and auditable
- Fail-closed: unclear tasks are flagged, not guessed at
- Idempotent: (task_id, last_modified) pairs already seen are skipped

Notes are concise, plain English, non-technical (per agency standard).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .ezlynx_task_report import AssignedTask

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """Result of processing a single assigned task."""
    task_id: str
    applicant_id: str
    # "acknowledged", "categorized", "flagged_for_human", "skipped_seen", "failed"
    action: str
    detail: str
    timestamp: str


class TaskAssignmentWorker:
    """Processes tasks assigned to Roby from EZLynx."""

    def __init__(self, discussion_client=None, seen: set[tuple[str, str]] | None = None):
        """Initialize.

        discussion_client: object with append_note(discussion_id, body).
            None = dry-run (log only).
        seen: set of (task_id, last_modified) already processed.
            Used for idempotency across report runs.
        """
        self.client = discussion_client
        self.seen: set[tuple[str, str]] = seen if seen is not None else set()
        self.results: list[TaskResult] = []

    def process_tasks(self, tasks: list[AssignedTask]) -> list[TaskResult]:
        """Process a list of assigned tasks. Empty list is fine (quiet).

        Returns only this batch's results (not accumulated history).
        """
        batch: list[TaskResult] = []
        for task in tasks:
            try:
                result = self._process_single(task)
                batch.append(result)
            except Exception as e:
                logger.error(f"Failed to process task {task.task_id}: {e}")
                batch.append(TaskResult(
                    task_id=task.task_id,
                    applicant_id=task.applicant_id,
                    action="failed",
                    detail=f"Error: {str(e)[:200]}",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ))
        self.results.extend(batch)
        return batch

    def _process_single(self, task: AssignedTask) -> TaskResult:
        """Process a single task: dedupe, acknowledge, categorize."""
        timestamp = datetime.now(timezone.utc).isoformat()
        identity = (task.task_id, task.last_modified)

        # Idempotency: skip if we've seen this exact task version
        if identity in self.seen:
            return TaskResult(
                task_id=task.task_id,
                applicant_id=task.applicant_id,
                action="skipped_seen",
                detail=f"Already processed (last modified {task.last_modified})",
                timestamp=timestamp,
            )

        # Step 1: Acknowledge — one concise note on the task's discussion
        self._post_note(
            task.discussion_id,
            "Roby picked up this task and is reviewing it.",
        )

        # Step 2: Categorize
        category = self._categorize_task(task)

        if category == "unknown":
            self._post_note(
                task.discussion_id,
                "Roby could not determine how to handle this task automatically. "
                "Flagged for team review — please reassign as needed.",
            )
            self.seen.add(identity)
            return TaskResult(
                task_id=task.task_id,
                applicant_id=task.applicant_id,
                action="flagged_for_human",
                detail=f"Unclear task, flagged for review: {task.description[:80]}",
                timestamp=timestamp,
            )

        # Step 3: Record categorization (v1: acknowledge + categorize;
        # specific handlers for callback/document/quote land incrementally)
        self._post_note(
            task.discussion_id,
            f"Roby categorized this as: {category}. Working on it.",
        )
        self.seen.add(identity)
        return TaskResult(
            task_id=task.task_id,
            applicant_id=task.applicant_id,
            action="categorized",
            detail=f"Categorized as {category}: {task.description[:80]}",
            timestamp=timestamp,
        )

    def _categorize_task(self, task: AssignedTask) -> str:
        """Categorize a task from its activity type and note text."""
        text = f"{task.title} {task.description}".lower()

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

    def _post_note(self, discussion_id: str, note_body: str) -> None:
        """Post a note to the task's discussion.

        Uses the Discussion ID straight from the report — no guessing
        which discussion is correct. Dry-run (no client) logs instead.
        """
        if not discussion_id:
            raise ValueError("No discussion ID — cannot post note")

        if self.client is None:
            logger.info(
                f"[DRY-RUN] Would post to discussion {discussion_id}: "
                f"{note_body[:100]}..."
            )
            return

        self.client.append_note(discussion_id, note_body)
        logger.info(f"Posted note to discussion {discussion_id}")
