"""Automated pipeline for RingCentral + EZLynx productivity monitoring.

Can run in two modes:
1. Watchdog mode (runs every 15-30m): Checks for unreturned missed calls > 30 mins and fires real-time critical alerts.
2. EOD Scorecard mode (runs daily at 5:00 PM): Generates and posts the comprehensive agency scorecard.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from robie_job_engine.chat_app_post import post_as_chat_app
from robie_job_engine.ezlynx_productivity_sync import EZLynxProductivityParser
from robie_job_engine.productivity import (
    EZLynxActivityMetric,
    EZLynxTaskMetric,
    ProductivityAuditor,
    RingCentralCall,
)
from robie_job_engine.ringcentral_client import RingCentralClient

logger = logging.getLogger(__name__)


class ProductivityPipeline:
    """Orchestrates ingestion, auditing, and Google Chat delivery."""

    def __init__(
        self,
        rc_client: Optional[RingCentralClient] = None,
        auditor: Optional[ProductivityAuditor] = None,
        chat_space: Optional[str] = None,
    ):
        self.rc_client = rc_client
        self.auditor = auditor or ProductivityAuditor(sla_warning_minutes=30)
        self.chat_space = chat_space or os.environ.get("ROBIE_PRODUCTIVITY_CHAT_SPACE")

    def run(
        self,
        calls: Optional[List[RingCentralCall]] = None,
        tasks_csv_path: Optional[str] = None,
        activities_csv_path: Optional[str] = None,
        watchdog_only: bool = False,
        post_to_chat: bool = True,
    ) -> Dict[str, Any]:
        """Executes the productivity audit and optionally posts results to Google Chat."""
        # 1. Ingest RingCentral Calls
        if calls is None:
            if self.rc_client:
                calls = self.rc_client.fetch_call_logs(
                    date_from=datetime.now(timezone.utc) - timedelta(hours=24)
                )
            else:
                calls = []

        # 2. Ingest EZLynx Tasks & Activities
        tasks: List[EZLynxTaskMetric] = []
        if tasks_csv_path and os.path.exists(tasks_csv_path):
            tasks = EZLynxProductivityParser.parse_tasks_csv(tasks_csv_path)

        activities: List[EZLynxActivityMetric] = []
        if activities_csv_path and os.path.exists(activities_csv_path):
            activities = EZLynxProductivityParser.parse_activities_csv(activities_csv_path)

        # 3. Generate Audit
        audit = self.auditor.generate_audit(calls, tasks=tasks, activities=activities)

        # 4. Handle Google Chat Delivery
        if post_to_chat and self.chat_space:
            critical_alerts = audit.get("critical_alerts", [])
            if watchdog_only:
                if critical_alerts:
                    alert_msg = "🚨 **ACCOUNT RISK ALERT: Unreturned Missed Call(s)**\n" + "\n".join(critical_alerts)
                    post_as_chat_app(self.chat_space, alert_msg)
            else:
                scorecard_msg = self.auditor.format_google_chat_card(audit)
                post_as_chat_app(self.chat_space, scorecard_msg)

        return audit
