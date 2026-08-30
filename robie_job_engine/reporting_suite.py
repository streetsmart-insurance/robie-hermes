#!/usr/bin/env python3
"""StreetSmart 3-Tier Reporting Suite: Daily, Weekly, and Monthly Reports
with Habitual Task Postponement / Snooze Detection.
"""

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _display_metric(data: Dict[str, Any], key: str, *, suffix: str = "") -> str:
    """Render absent evidence as UNVERIFIED instead of inventing a default."""

    value = data.get(key)
    if value is None or value == "":
        return "UNVERIFIED"
    return f"{value}{suffix}"


def _source_warning(name: str, data: Optional[Dict[str, Any]]) -> Optional[str]:
    if data is None:
        return f"{name}: source was not supplied"
    status = str(data.get("source_status", "available")).lower()
    if status not in {"available", "complete", "verified"}:
        return f"{name}: {data.get('source_status', 'UNVERIFIED')}"
    return None


@dataclass
class TaskPostponementIncident:
    """Represents a task being habitual snoozed/pushed forward."""
    task_id: str
    title: str
    assigned_user: str
    created_date: str
    due_date: str
    age_days: int
    postpone_count: int
    is_habitual: bool = False
    flag_reason: str = ""


class TaskAgingAuditor:
    """Detects tasks being kicked down the road instead of completed."""

    def __init__(self, age_warning_days: int = 14, snooze_threshold: int = 2):
        self.age_warning_days = age_warning_days
        self.snooze_threshold = snooze_threshold

    def audit_task_aging(self, tasks: List[Dict[str, Any]]) -> List[TaskPostponementIncident]:
        """Audits tasks for habitual postponement and excessive age."""
        incidents = []
        for t in tasks:
            task_id = str(t.get("id", t.get("TaskId", "")))
            title = t.get("title", t.get("Title", "Untitled Task"))
            user = t.get("assigned_user", t.get("AssignedUser", "Unassigned"))
            created = t.get("created_date", t.get("CreatedDate", ""))
            due = t.get("due_date", t.get("DueDate", ""))
            age = int(t.get("age_days", t.get("AgeDays", 0)))
            snooze_count = int(t.get("postpone_count", t.get("SnoozeCount", 0)))

            is_habitual = False
            reasons = []

            if age > self.age_warning_days:
                is_habitual = True
                reasons.append(f"Task age is {age} days (exceeds {self.age_warning_days}d SLA)")

            if snooze_count >= self.snooze_threshold:
                is_habitual = True
                reasons.append(f"Due date postponed {snooze_count} times without completion")

            if is_habitual:
                incidents.append(TaskPostponementIncident(
                    task_id=task_id,
                    title=title,
                    assigned_user=user,
                    created_date=created,
                    due_date=due,
                    age_days=age,
                    postpone_count=snooze_count,
                    is_habitual=True,
                    flag_reason=" | ".join(reasons)
                ))
        return incidents


class ReportingSuite:
    """Generates Daily, Weekly, and Monthly executive reports."""

    def build_daily_report(
        self,
        call_data: Dict[str, Any],
        task_data: Dict[str, Any],
        magellan_data: Optional[Dict[str, Any]] = None,
        sales_data: Optional[Dict[str, Any]] = None,
        role_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Daily: Phones + Overdue Tasks + Unreturned Calls SLA."""
        lines = [
            "📋 *STREETSMART DAILY SERVICE & PHONE WATCHDOG*",
            f"📅 Date: {datetime.now(timezone.utc).strftime('%A, %B %d, %Y')}",
            "",
            "🚨 *CRITICAL UNRETURNED CALLS (>30m SLA)*",
        ]
        unreturned = call_data.get("unreturned_calls", [])
        if not unreturned:
            lines.append("   🟢 All client voicemails and missed calls resolved!")
        else:
            for i, u in enumerate(unreturned[:10], 1):
                sentiment = u.get("magellan_sentiment")
                tags = u.get("magellan_tags") or []
                risk = f" | Magellan: {sentiment} ({', '.join(tags)})" if sentiment else ""
                lines.append(f"   {i}. 🔴 *{u.get('name', 'Client')}* ({u.get('phone')}) — Left on {u.get('rep', 'Queue')} at {u.get('time')}{risk}")

        lines.extend([
            "",
            "📊 *DAILY REP PHONE ANSWER RATES & TASK BACKLOGS*",
            "| Rep | Inbound | Answer Rate | Unreturned | Overdue Tasks |",
            "| :--- | :---: | :---: | :---: | :---: |",
        ])
        for rep, stats in call_data.get("rep_stats", {}).items():
            ans_rate = stats.get("answer_rate") or "NOT EVALUABLE"
            overdue = task_data.get("overdue_by_rep", {}).get(rep)
            overdue_display = "UNVERIFIED" if overdue is None else str(overdue)
            unret = stats.get("unreturned", 0)
            badge = "🔴" if unret > 0 else "🟢"
            lines.append(f"| {rep} | {stats.get('inbound', 0)} | {ans_rate} | {badge} {unret} | {overdue_display} |")

        queue_rows = call_data.get("queue_rows", [])
        if queue_rows:
            lines.extend([
                "",
                "☎️ *CALL QUEUE PICKUP & MEMBER LEGS*",
                "| Queue | Offered | Answered | Abandoned/VM | Answer Rate | Max Wait |",
                "| :--- | ---: | ---: | ---: | ---: | ---: |",
            ])
            for row in queue_rows:
                wait = "UNVERIFIED" if row.get("max_wait_seconds") is None else f"{row['max_wait_seconds']}s"
                lines.append(
                    f"| {row['queue']} | {row['offered']} | {row['answered']} | "
                    f"{row['abandoned_or_voicemail']} | {row['answer_rate']} | {wait} |"
                )
                pickups = ", ".join(f"{name}: {count}" for name, count in sorted(row.get("answered_by", {}).items())) or "none evidenced"
                missed = ", ".join(f"{name}: {count}" for name, count in sorted(row.get("missed_by", {}).items())) or "none evidenced"
                lines.append(f"• {row['queue']} pickups — {pickups}")
                lines.append(f"• {row['queue']} missed/refused member legs — {missed}")

        magellan_data = magellan_data or {"source_status": "not supplied"}
        sales_data = sales_data or {"source_status": "not supplied"}
        role_rows = role_rows or []
        sales_exceptions = sales_data.get("exceptions", [])
        if sales_exceptions:
            lines.extend(["", "📈 *SALES CENTER INACTIVE OPPORTUNITIES*"])
            for item in sales_exceptions[:20]:
                lines.append(
                    f"• {item.get('account_name', 'Unknown account')} — {item.get('producer', 'Unassigned')} | "
                    f"{item.get('stage', 'Unknown')} | last touch {item.get('days_since_touch', 'unknown')}d ago | "
                    f"{'; '.join(item.get('reasons', []))} | source row {item.get('source_row_number', '?')}"
                )
        if role_rows:
            lines.extend(["", "🧩 *TODAY'S WORK ↔ ROLE RESPONSIBILITIES*"])
            for row in role_rows:
                lines.append(f"*{row['employee']} — {row['role']}*")
                lines.extend(f"• {fact}" for fact in row.get("facts", []))
        warnings = [
            warning
            for warning in (
                _source_warning("RingCentral", call_data),
                _source_warning("EZLynx Tasks", task_data),
                _source_warning("Magellan", magellan_data),
                _source_warning("EZLynx Sales Center", sales_data),
            )
            if warning
        ]
        if warnings:
            lines.extend(["", "⚠️ *DATA LIMITATIONS*"] + [f"• {warning}" for warning in warnings])

        return "\n".join(lines)

    def build_weekly_report(
        self,
        call_data: Dict[str, Any],
        task_data: Dict[str, Any],
        sales_data: Dict[str, Any],
        retention_data: Dict[str, Any],
        email_data: Dict[str, Any],
        submission_data: Optional[Dict[str, Any]] = None,
        tracker_data: Optional[Dict[str, Any]] = None,
        appsheet_data: Optional[Dict[str, Any]] = None,
        magellan_data: Optional[Dict[str, Any]] = None,
        role_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Weekly: Phones + Tasks + Sales Center + Submission Center + Retention Center + Email."""
        submission_data = submission_data or {"source_status": "not supplied"}
        tracker_data = tracker_data or {"source_status": "not supplied"}
        appsheet_data = appsheet_data or {"source_status": "not supplied"}
        magellan_data = magellan_data or {"source_status": "not supplied"}
        role_rows = role_rows or []
        lines = [
            "🏆 *STREETSMART WEEKLY EXECUTIVE PERFORMANCE SCORECARD*",
            f"📅 Period: Past 7 Days ({datetime.now(timezone.utc).strftime('%B %d, %Y')})",
            "",
            "📈 *EXECUTIVE OVERVIEW*",
            f"• Inbound Answer Rate: {_display_metric(call_data, 'answer_rate')}",
            f"• Unreturned Voicemails: {_display_metric(call_data, 'unreturned_total')}",
            f"• Overdue Tasks: {_display_metric(task_data, 'total_overdue')}",
            f"• Sales Center Quotes Created: {_display_metric(sales_data, 'quotes_created')}",
            f"• Sales Opportunities With No Recent Touch: {_display_metric(sales_data, 'inactive_opportunity_count')}",
            f"• Retention Reviews Completed: {_display_metric(retention_data, 'reviews_completed')}",
            f"• Retention Accounts Requiring Review: {_display_metric(retention_data, 'exception_count')}",
            f"• Open Submissions Over 30 Days: {_display_metric(submission_data, 'open_over_30_count')}",
            f"• Email Threads >24h Awaiting Employee: {_display_metric(email_data, 'stalled_threads')}",
            f"• Department Tracker Exceptions: {_display_metric(tracker_data, 'exception_count')}",
            f"• AppSheet Accountability Rows: {_display_metric(appsheet_data, 'total_rows')}",
            f"• Magellan At-Risk Calls: {_display_metric(magellan_data, 'at_risk_calls')}",
            "",
            "🎯 *ROLE-BASED EMPLOYEE ACCOUNTABILITY*",
        ]

        employee_rows = call_data.get("employee_rows", [])
        if employee_rows:
            lines.extend(
                [
                    "| Employee | Calls Presented | Answered | Voicemail/Missed | Unreturned | Outbound | Status |",
                    "| :--- | ---: | ---: | ---: | ---: | ---: | :--- |",
                ]
            )
            for row in employee_rows:
                status = row.get("status") or ("NOT EVALUABLE" if not row.get("calls_presented") else "REVIEW")
                lines.append(
                    f"| {row.get('employee', 'Unknown')} | {row.get('calls_presented', 0)} | "
                    f"{row.get('answered', 0)} | {row.get('missed_or_voicemail', 0)} | "
                    f"{row.get('unreturned', 0)} | {row.get('outbound', 0)} | {status} |"
                )
        else:
            lines.append("• Employee comparison: UNVERIFIED — no normalized employee rows supplied")

        queue_rows = call_data.get("queue_rows", [])
        if queue_rows:
            lines.extend(["", "☎️ *WEEKLY QUEUE ACCOUNTABILITY*"])
            for row in queue_rows:
                pickups = ", ".join(f"{name}: {count}" for name, count in sorted(row.get("answered_by", {}).items())) or "none evidenced"
                missed = ", ".join(f"{name}: {count}" for name, count in sorted(row.get("missed_by", {}).items())) or "none evidenced"
                lines.append(
                    f"• *{row['queue']}* — {row['answered']}/{row['offered']} answered "
                    f"({row['answer_rate']}); abandoned/VM {row['abandoned_or_voicemail']}; pickups: {pickups}; "
                    f"missed/refused member legs: {missed}"
                )

        if role_rows:
            lines.extend(["", "🧩 *RESPONSIBILITIES ↔ OBSERVED WORK*"])
            for row in role_rows:
                lines.append(f"*{row['employee']} — {row['role']}*")
                lines.extend(f"• {fact}" for fact in row.get("facts", []))

        retention_exceptions = retention_data.get("exceptions", [])

        sales_exceptions = sales_data.get("exceptions", [])
        if sales_exceptions:
            lines.extend(["", "📈 *SALES CENTER INACTIVE OPPORTUNITIES*"])
            for item in sales_exceptions[:20]:
                lines.append(
                    f"• {item.get('account_name', 'Unknown account')} — {item.get('producer', 'Unassigned')} | "
                    f"{item.get('stage', 'Unknown')} | last touch {item.get('days_since_touch', 'unknown')}d ago | "
                    f"{'; '.join(item.get('reasons', []))} | source row {item.get('source_row_number', '?')}"
                )
        if retention_exceptions:
            lines.extend(["", "🛡️ *RETENTION CENTER EXCEPTIONS*"])
            for item in retention_exceptions[:20]:
                lines.append(
                    f"• {item.get('account_name', 'Unknown account')} — {item.get('owner', 'Unassigned')} | "
                    f"expires in {item.get('days_to_expiration', 'unknown')}d | "
                    f"{'; '.join(item.get('reasons', []))} | source row {item.get('source_row_number', '?')}"
                )

        submission_exceptions = submission_data.get("exceptions", [])
        if submission_exceptions:
            lines.extend(["", "📨 *SUBMISSION CENTER EXCEPTIONS (>30 DAYS OPEN)*"])
            for item in submission_exceptions[:20]:
                lines.append(
                    f"• {item.get('account_name', 'Unknown account')} — {item.get('owner', 'Unassigned')} | "
                    f"open {item.get('age_days', 'unknown')}d | {item.get('status', 'Unknown')} | "
                    f"{'; '.join(item.get('reasons', []))} | source row {item.get('source_row_number', '?')}"
                )

        tracker_exceptions = tracker_data.get("exceptions", [])
        if tracker_exceptions:
            lines.extend(["", "🧭 *DEPARTMENT TRACKER EXCEPTIONS*"])
            grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for item in tracker_exceptions:
                grouped[item.get("tracker_name", "Unknown tracker")].append(item)
            for tracker_name, items in grouped.items():
                lines.append(f"*{tracker_name}* — {len(items)} item(s) requiring review")
                for item in items[:10]:
                    lines.append(
                        f"• {item.get('account', 'Unknown item')} — {item.get('owner', 'Unassigned')} | "
                        f"{item.get('status', 'Unknown')} | {'; '.join(item.get('reasons', []))} | "
                        f"blocker: {item.get('blocker_party', 'unclear')} | "
                        f"next: {item.get('next_action', 'Review EZLynx')} | "
                        f"alert draft for: {item.get('notification_target', 'Unassigned')} | "
                        f"EZLynx: {item.get('ezlynx_reference', 'UNVERIFIED')} | source row {item.get('source_row_number', '?')}"
                    )

        missed_call_reconciliation = tracker_data.get("missed_call_reconciliation", [])
        if missed_call_reconciliation:
            lines.extend(["", "☎️ *MISSED CALL TRACKER ↔ RINGCENTRAL RECONCILIATION (NOT DOUBLE-COUNTED)*"])
            for item in missed_call_reconciliation[:20]:
                lines.append(
                    f"• {item.get('account', 'Unknown caller')} — {item.get('owner', 'Unassigned')} | "
                    f"tracker row {item.get('source_row_number', '?')} requires RingCentral callback match"
                )

        attestation_mismatches = tracker_data.get("attestation_mismatches", [])
        if attestation_mismatches:
            lines.extend(["", "⚠️ *VOICEMAIL/EMAIL TRACKER ATTESTATION MISMATCHES*"])
            for item in attestation_mismatches[:20]:
                lines.append(
                    f"• {item.get('employee', 'Unknown')} reported {item.get('reported_unresolved')} unresolved, "
                    f"but RingCentral/Gmail evidence shows {item.get('evidenced_unresolved')} | "
                    f"tracker row {item.get('source_row_number', '?')}"
                )

        warnings = [
            warning
            for warning in (
                _source_warning("RingCentral", call_data),
                _source_warning("EZLynx Tasks", task_data),
                _source_warning("EZLynx Sales Center", sales_data),
                _source_warning("EZLynx Retention Center", retention_data),
                _source_warning("EZLynx Submission Center", submission_data),
                _source_warning("Gmail", email_data),
                _source_warning("Department Trackers", tracker_data),
                _source_warning("AppSheet", appsheet_data),
                _source_warning("Magellan", magellan_data),
            )
            if warning
        ]
        if warnings:
            lines.extend(["", "⚠️ *DATA LIMITATIONS*"] + [f"• {warning}" for warning in warnings])
        return "\n".join(lines)

    def build_monthly_report(
        self,
        monthly_kpis: Dict[str, Any],
        churn_autopsies: List[Dict[str, Any]],
        sales_data: Optional[Dict[str, Any]] = None,
        role_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Monthly: Holistic 30-Day Agency Score + Lost Customer Churn Root Cause Autopsies."""
        grade = monthly_kpis.get("grade")
        score = monthly_kpis.get("holistic_score")
        approved_formula = monthly_kpis.get("approved_formula_version")
        if grade is None or score is None or not approved_formula:
            grade_line = "📊 *30-DAY HOLISTIC AGENCY GRADE: UNVERIFIED — approved weighting formula or evidence missing*"
        else:
            grade_line = f"📊 *30-DAY HOLISTIC AGENCY GRADE: {grade} ({score}/100)* — formula {approved_formula}"
        sales_data = sales_data or {"source_status": "not supplied"}
        role_rows = role_rows or []
        lines = [
            "🏛️ *STREETSMART MONTHLY EXECUTIVE AUDIT & CHURN AUTOPSY*",
            f"📅 Month: {datetime.now(timezone.utc).strftime('%B %Y')}",
            "",
            grade_line,
            f"• Total Calls Processed: {_display_metric(monthly_kpis, 'total_calls')}",
            f"• Active Policies Serviced: {_display_metric(monthly_kpis, 'policies_serviced')}",
            f"• Sales Opportunities With No Recent Touch: {_display_metric(sales_data, 'inactive_opportunity_count')}",
            f"• Confirmed Churn / Cancellation Cases Supplied: {len(churn_autopsies)}",
            "",
            "🔍 *LOST CUSTOMER ROOT CAUSE AUTOPSIES*",
        ]
        if not churn_autopsies:
            lines.append("• UNVERIFIED — no confirmed cancellation evidence bundle supplied")
        for c in churn_autopsies:
            evidence_ids = c.get("evidence_ids") or []
            confidence = c.get("confidence")
            root_cause = c.get("root_cause")
            if not evidence_ids or not confidence or not root_cause:
                verdict = "UNVERIFIED claim — evidence IDs, confidence, or root-cause analysis missing"
            else:
                verdict = f"{root_cause} | confidence: {confidence} | evidence: {', '.join(map(str, evidence_ids))}"
            lines.append(f"• *{c.get('client_name', 'Unknown client')}* ({c.get('policy_type', 'Unknown policy')}) — {verdict}")
        if role_rows:
            lines.extend(["", "🧩 *30-DAY EMPLOYEE ROLE ACCOUNTABILITY*"])
            for row in role_rows:
                lines.append(f"*{row['employee']} — {row['role']}*")
                lines.extend(f"• {fact}" for fact in row.get("facts", []))
        return "\n".join(lines)
