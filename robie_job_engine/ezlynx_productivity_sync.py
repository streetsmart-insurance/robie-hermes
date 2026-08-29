"""EZLynx Productivity Report Ingestion and Normalization.

Parses EZLynx Task Aging, Open Tasks, and Activity/Discussion Reports
from CSV, Excel, or JSON exports.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from robie_job_engine.productivity import EZLynxActivityMetric, EZLynxTaskMetric


class EZLynxProductivityParser:
    """Parses EZLynx task and activity reports into normalized employee productivity metrics."""

    @classmethod
    def parse_tasks_csv(cls, csv_content_or_path: Union[str, Path]) -> List[EZLynxTaskMetric]:
        """Parses EZLynx Task Aging / Task List CSV export."""
        if isinstance(csv_content_or_path, Path) or (isinstance(csv_content_or_path, str) and "\n" not in csv_content_or_path and Path(csv_content_or_path).exists()):
            with open(csv_content_or_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
        else:
            content = str(csv_content_or_path)

        reader = csv.DictReader(io.StringIO(content))
        
        # Group stats by user
        completed_counts = defaultdict(int)
        overdue_counts = defaultdict(int)
        open_counts = defaultdict(int)
        high_pri_overdue = defaultdict(int)

        now = datetime.now(timezone.utc)

        for row in reader:
            # Case-insensitive column matching
            row_lower = {k.strip().lower(): v.strip() for k, v in row.items() if k}

            user = (
                row_lower.get("assigned to")
                or row_lower.get("assigned_to")
                or row_lower.get("user")
                or row_lower.get("csr")
                or row_lower.get("producer")
                or "Unassigned"
            )

            status = row_lower.get("status", "").lower()
            due_date_str = row_lower.get("due date") or row_lower.get("due_date") or row_lower.get("date due", "")
            priority = row_lower.get("priority", "").lower()

            is_completed = "complete" in status or "closed" in status or "done" in status
            is_overdue = "overdue" in status or "past due" in status

            # If status doesn't explicitly say overdue, check due date
            if not is_completed and due_date_str and not is_overdue:
                for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
                    try:
                        due_dt = datetime.strptime(due_date_str, fmt).replace(tzinfo=timezone.utc)
                        if due_dt < now:
                            is_overdue = True
                        break
                    except ValueError:
                        continue

            if is_completed:
                completed_counts[user] += 1
            else:
                open_counts[user] += 1
                if is_overdue:
                    overdue_counts[user] += 1
                    if "high" in priority or "urgent" in priority:
                        high_pri_overdue[user] += 1

        all_users = set(completed_counts.keys()) | set(open_counts.keys()) | set(overdue_counts.keys())
        metrics: List[EZLynxTaskMetric] = []

        for user in sorted(all_users):
            if not user or user == "Unassigned":
                continue
            metrics.append(
                EZLynxTaskMetric(
                    employee_name=user,
                    completed_today=completed_counts[user],
                    overdue=overdue_counts[user],
                    open_total=open_counts[user],
                    high_priority_overdue=high_pri_overdue[user],
                )
            )

        return metrics

    @classmethod
    def parse_activities_csv(cls, csv_content_or_path: Union[str, Path]) -> List[EZLynxActivityMetric]:
        """Parses EZLynx Activity Log / Discussion Notes CSV export."""
        if isinstance(csv_content_or_path, Path) or (isinstance(csv_content_or_path, str) and "\n" not in csv_content_or_path and Path(csv_content_or_path).exists()):
            with open(csv_content_or_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
        else:
            content = str(csv_content_or_path)

        reader = csv.DictReader(io.StringIO(content))
        
        notes_count = defaultdict(int)
        quotes_count = defaultdict(int)
        policy_changes = defaultdict(int)

        for row in reader:
            row_lower = {k.strip().lower(): v.strip() for k, v in row.items() if k}

            user = (
                row_lower.get("created by")
                or row_lower.get("user")
                or row_lower.get("author")
                or row_lower.get("csr")
                or "Unassigned"
            )

            act_type = str(
                row_lower.get("activity type")
                or row_lower.get("type")
                or row_lower.get("action")
                or ""
            ).lower()

            if "quote" in act_type:
                quotes_count[user] += 1
                notes_count[user] += 1
            elif "policy" in act_type or "endorsement" in act_type or "change" in act_type:
                policy_changes[user] += 1
                notes_count[user] += 1
            else:
                notes_count[user] += 1

        all_users = set(notes_count.keys()) | set(quotes_count.keys())
        metrics: List[EZLynxActivityMetric] = []

        for user in sorted(all_users):
            if not user or user == "Unassigned":
                continue
            metrics.append(
                EZLynxActivityMetric(
                    employee_name=user,
                    notes_count=notes_count[user],
                    quotes_created=quotes_count[user],
                    policy_changes=policy_changes[user],
                )
            )

        return metrics
