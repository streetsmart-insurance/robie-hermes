"""StreetSmart Productivity & Accountability Engine.

Measures and reconciles:
1. RingCentral inbound/outbound call metrics and duration.
2. Inbound missed calls & voicemails reconciled against outbound callback attempts
   (preventing orphaned missed calls and lost accounts).
3. EZLynx task velocity, completions, and aging/overdue backlogs.
4. EZLynx activity notes and documentation density.
5. Composite Follow-Through & Accountability score per individual.
6. Real-time SLA breach alerts and daily executive scorecards for Google Chat.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class RingCentralCall:
    """Represents a normalized RingCentral call log entry."""
    call_id: str
    direction: str  # "Inbound" or "Outbound"
    from_number: str
    to_number: str
    result: str  # "Call connected", "Missed", "Voicemail", "Busy", "Rejected", etc.
    duration_seconds: int
    start_time: datetime
    extension: str
    employee_name: str
    queue_name: str = ""
    answered_by: str = ""
    queue_wait_seconds: int = 0
    handle_seconds: int = 0
    hold_seconds: int = 0
    duration_source: str = "Call Length"

    @classmethod
    def from_dict(cls, data: Dict[str, Any], employee_mapping: Optional[Dict[str, str]] = None) -> RingCentralCall:
        """Parse from RingCentral REST API format or flattened dictionary."""
        mapping = employee_mapping or {}
        
        # Determine extension / rep
        ext = str(data.get("extension", data.get("extensionId", ""))).strip()
        emp_name = (
            data.get("employee_name")
            or mapping.get(ext)
            or mapping.get(data.get("to_number", ""))
            or mapping.get(data.get("from_number", ""))
            or (f"Ext {ext}" if ext else "Unassigned")
        )

        start_raw = data.get("start_time") or data.get("startTime")
        if isinstance(start_raw, str):
            # Parse ISO or standard timestamps
            clean_ts = start_raw.replace("Z", "+00:00")
            try:
                start_dt = datetime.fromisoformat(clean_ts)
            except ValueError:
                start_dt = datetime.strptime(clean_ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        elif isinstance(start_raw, datetime):
            start_dt = start_raw
        else:
            start_dt = datetime.now(timezone.utc)

        # Standardize direction
        raw_direction = str(data.get("direction", "")).strip().capitalize()
        direction = "Inbound" if raw_direction.startswith("In") else "Outbound"

        # Standardize result
        raw_result = str(data.get("result", data.get("action", ""))).strip().capitalize()
        if "Miss" in raw_result:
            result = "Missed"
        elif "Voice" in raw_result:
            result = "Voicemail"
        elif "Connect" in raw_result or "Answer" in raw_result or "Success" in raw_result:
            result = "Call connected"
        elif "Busy" in raw_result:
            result = "Busy"
        else:
            result = raw_result or ("Call connected" if int(data.get("duration_seconds", data.get("duration", 0))) > 0 else "Missed")

        # Standardize phone numbers (strip non-digits for clean matching)
        from_num = normalize_phone(str(data.get("from_number", data.get("from", {}).get("phoneNumber", ""))))
        to_num = normalize_phone(str(data.get("to_number", data.get("to", {}).get("phoneNumber", ""))))

        return cls(
            call_id=str(data.get("call_id", data.get("id", ""))),
            direction=direction,
            from_number=from_num,
            to_number=to_num,
            result=result,
            duration_seconds=int(data.get("duration_seconds", data.get("duration", 0))),
            start_time=start_dt,
            extension=ext,
            employee_name=emp_name,
            queue_name=str(data.get("queue_name") or "").strip(),
            answered_by=str(data.get("answered_by") or "").strip(),
            queue_wait_seconds=int(data.get("queue_wait_seconds") or 0),
            handle_seconds=int(data.get("handle_seconds") or 0),
            hold_seconds=int(data.get("hold_seconds") or 0),
            duration_source=str(data.get("duration_source") or "Call Length").strip(),
        )


def normalize_phone(raw: str) -> str:
    """Strips phone number to standard 10 or 11 digits for robust matching."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


@dataclass
class EZLynxTaskMetric:
    """Task volume and aging metrics for an employee."""
    employee_name: str
    completed_today: int = 0
    overdue: int = 0
    open_total: int = 0
    high_priority_overdue: int = 0


@dataclass
class EZLynxActivityMetric:
    """Documentation and activity note volume for an employee."""
    employee_name: str
    notes_count: int = 0
    quotes_created: int = 0
    policy_changes: int = 0


@dataclass
class CallerProfile:
    """Classifies a caller as Active Client, Prospect / Non-Client, or Vendor/Underwriter."""
    phone_number: str
    caller_name: str = ""
    client_type: str = "UNKNOWN"  # "ACTIVE_CLIENT", "PROSPECT_NON_CLIENT", "VENDOR_UNDERWRITER"
    ezlynx_applicant_id: Optional[str] = None
    assigned_producer: Optional[str] = None
    assigned_csr: Optional[str] = None
    sentiment_flag: Optional[str] = None  # "POSITIVE", "NEUTRAL", "NEGATIVE", "FRUSTRATED"


@dataclass
class NonClientLeadAudit:
    """Tracks whether new prospects and non-clients are being called back."""
    phone_number: str
    caller_name: str
    first_call_time: datetime
    calls_count: int
    was_returned: bool
    returned_by: Optional[str] = None
    response_time_minutes: Optional[float] = None
    magellan_sentiment: Optional[str] = None


@dataclass
class MissedCallIncident:
    """Tracks the lifecycle of an inbound missed call and its callback SLA."""
    call_id: str
    caller_phone: str
    employee_name: str
    extension: str
    missed_at: datetime
    is_voicemail: bool
    status: str  # "RESOLVED", "ORPHANED_ALERT", "PENDING_SLA"
    returned_at: Optional[datetime] = None
    returned_by: Optional[str] = None
    response_time_minutes: Optional[float] = None
    callback_call_id: Optional[str] = None


@dataclass
class EmployeeProductivityReport:
    """Consolidated productivity scorecard for a single employee."""
    employee_name: str
    inbound_total: int = 0
    inbound_answered: int = 0
    inbound_missed: int = 0
    inbound_voicemails: int = 0
    outbound_total: int = 0
    total_talk_time_seconds: int = 0
    missed_calls_resolved: int = 0
    missed_calls_orphaned: int = 0
    missed_calls_pending: int = 0
    avg_callback_time_minutes: Optional[float] = None
    ezlynx_tasks_completed: Optional[int] = None
    ezlynx_tasks_overdue: Optional[int] = None
    ezlynx_activities_logged: Optional[int] = None
    productivity_score: Optional[float] = None  # None when required evidence is absent
    score_status: str = "UNVERIFIED"
    data_limitations: List[str] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)

    @property
    def total_talk_time_minutes(self) -> float:
        return round(self.total_talk_time_seconds / 60.0, 1)

    @property
    def answer_rate_percent(self) -> Optional[float]:
        if self.inbound_total == 0:
            return None
        return round((self.inbound_answered / self.inbound_total) * 100.0, 1)

    @property
    def callback_resolution_rate_percent(self) -> Optional[float]:
        total_missed = self.inbound_missed + self.inbound_voicemails
        if total_missed == 0:
            return None
        return round((self.missed_calls_resolved / total_missed) * 100.0, 1)


class ProductivityAuditor:
    """Reconciles phone and agency management data into actionable accountability audits."""

    def __init__(
        self,
        sla_warning_minutes: int = 30,
        sla_critical_minutes: int = 120,
        employee_mapping: Optional[Dict[str, str]] = None,
        qualifying_callback_results: Optional[set[str]] = None,
    ):
        self.sla_warning_minutes = sla_warning_minutes
        self.sla_critical_minutes = sla_critical_minutes
        self.employee_mapping = employee_mapping or {}
        self.qualifying_callback_results = qualifying_callback_results or {"Call connected"}

    def reconcile_missed_calls(
        self, calls: List[RingCentralCall], reference_time: Optional[datetime] = None
    ) -> List[MissedCallIncident]:
        """Matches every inbound missed call/voicemail with subsequent outbound calls to the same caller."""
        ref_time = reference_time or datetime.now(timezone.utc)
        sorted_calls = sorted(calls, key=lambda c: c.start_time)
        
        incidents: List[MissedCallIncident] = []
        outbound_calls = [
            c
            for c in sorted_calls
            if c.direction == "Outbound"
            and c.to_number
            and c.result in self.qualifying_callback_results
        ]

        for call in sorted_calls:
            if call.direction == "Inbound" and call.result in ("Missed", "Voicemail"):
                caller = call.from_number
                if not caller:
                    continue

                # Search for an outbound return call to this caller phone placed AFTER the missed call
                subsequent_callbacks = [
                    c for c in outbound_calls
                    if c.to_number == caller and c.start_time >= call.start_time
                ]

                is_vm = call.result == "Voicemail"

                if subsequent_callbacks:
                    # Matched first return call
                    first_cb = subsequent_callbacks[0]
                    diff_mins = (first_cb.start_time - call.start_time).total_seconds() / 60.0
                    incidents.append(
                        MissedCallIncident(
                            call_id=call.call_id,
                            caller_phone=caller,
                            employee_name=call.employee_name,
                            extension=call.extension,
                            missed_at=call.start_time,
                            is_voicemail=is_vm,
                            status="RESOLVED",
                            returned_at=first_cb.start_time,
                            returned_by=first_cb.employee_name,
                            response_time_minutes=round(diff_mins, 1),
                            callback_call_id=first_cb.call_id,
                        )
                    )
                else:
                    # No return call found yet
                    elapsed_mins = (ref_time - call.start_time).total_seconds() / 60.0
                    status = "ORPHANED_ALERT" if elapsed_mins >= self.sla_warning_minutes else "PENDING_SLA"
                    incidents.append(
                        MissedCallIncident(
                            call_id=call.call_id,
                            caller_phone=caller,
                            employee_name=call.employee_name,
                            extension=call.extension,
                            missed_at=call.start_time,
                            is_voicemail=is_vm,
                            status=status,
                            returned_at=None,
                            returned_by=None,
                            response_time_minutes=None,
                            callback_call_id=None,
                        )
                    )

        return incidents

    def calculate_score(
        self,
        inbound_total: int,
        inbound_answered: int,
        missed_total: int,
        missed_resolved: int,
        orphaned_count: int,
        outbound_total: int,
        talk_time_secs: int,
        tasks_completed: int,
        tasks_overdue: int,
        activities_count: int,
    ) -> float:
        """Calculates a composite Follow-Through & Productivity Score (0 to 100)."""
        score = 100.0

        # Heavy penalty for orphaned missed calls (the prime risk for lost clients)
        score -= orphaned_count * 25.0

        # Penalty for overdue tasks
        score -= tasks_overdue * 5.0

        # Phone answer rate component
        if inbound_total > 0:
            answer_ratio = inbound_answered / inbound_total
            if answer_ratio < 0.85:
                score -= (0.85 - answer_ratio) * 30.0

        # Activity note bonus / documentation compliance
        if activities_count == 0 and (outbound_total > 5 or tasks_completed > 0):
            score -= 15.0  # Phone calls placed but zero notes logged

        # Bound score between 0 and 100
        return max(0.0, min(100.0, round(score, 1)))

    def generate_audit(
        self,
        calls: List[RingCentralCall],
        tasks: Optional[List[EZLynxTaskMetric]] = None,
        activities: Optional[List[EZLynxActivityMetric]] = None,
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Generates comprehensive agency productivity reports per individual and overall."""
        ref_time = reference_time or datetime.now(timezone.utc)
        incidents = self.reconcile_missed_calls(calls, reference_time=ref_time)

        tasks_provided = tasks is not None
        activities_provided = activities is not None

        task_map: Dict[str, EZLynxTaskMetric] = {t.employee_name: t for t in (tasks or [])}
        activity_map: Dict[str, EZLynxActivityMetric] = {a.employee_name: a for a in (activities or [])}

        # Unique employees
        all_employees = sorted(
            set(
                [c.employee_name for c in calls if c.employee_name != "Unassigned"]
                + list(task_map.keys())
                + list(activity_map.keys())
            )
        )

        employee_reports: Dict[str, EmployeeProductivityReport] = {}
        critical_alerts: List[str] = []

        for emp in all_employees:
            emp_calls = [c for c in calls if c.employee_name == emp]
            emp_incidents = [i for i in incidents if i.employee_name == emp]

            inbound = [c for c in emp_calls if c.direction == "Inbound"]
            inbound_answered = [c for c in inbound if c.result == "Call connected"]
            inbound_missed = [c for c in inbound if c.result == "Missed"]
            inbound_vms = [c for c in inbound if c.result == "Voicemail"]
            outbound = [c for c in emp_calls if c.direction == "Outbound"]

            total_talk = sum(c.duration_seconds for c in emp_calls)

            resolved_inc = [i for i in emp_incidents if i.status == "RESOLVED"]
            orphaned_inc = [i for i in emp_incidents if i.status == "ORPHANED_ALERT"]
            pending_inc = [i for i in emp_incidents if i.status == "PENDING_SLA"]

            resp_times = [i.response_time_minutes for i in resolved_inc if i.response_time_minutes is not None]
            avg_resp = round(sum(resp_times) / len(resp_times), 1) if resp_times else None

            emp_tasks = task_map.get(emp)
            emp_act = activity_map.get(emp)
            limitations: List[str] = []
            if not tasks_provided:
                limitations.append("EZLynx task source not supplied")
            if not activities_provided:
                limitations.append("EZLynx activity source not supplied")

            score: Optional[float] = None
            score_status = "UNVERIFIED"
            if not limitations:
                safe_tasks = emp_tasks or EZLynxTaskMetric(employee_name=emp)
                safe_act = emp_act or EZLynxActivityMetric(employee_name=emp)
                score = self.calculate_score(
                    inbound_total=len(inbound),
                    inbound_answered=len(inbound_answered),
                    missed_total=len(inbound_missed) + len(inbound_vms),
                    missed_resolved=len(resolved_inc),
                    orphaned_count=len(orphaned_inc),
                    outbound_total=len(outbound),
                    talk_time_secs=total_talk,
                    tasks_completed=safe_tasks.completed_today,
                    tasks_overdue=safe_tasks.overdue,
                    activities_count=safe_act.notes_count,
                )
                score_status = "NOT_EVALUABLE" if not inbound and not outbound and not emp_tasks and not emp_act else "EVALUATED"

            alerts: List[str] = []
            if orphaned_inc:
                alerts.append(f"🚨 {len(orphaned_inc)} missed call(s) unreturned > {self.sla_warning_minutes}m")
                for inc in orphaned_inc:
                    critical_alerts.append(
                        f"• {emp}: Unreturned call from {format_phone(inc.caller_phone)} at {inc.missed_at.strftime('%I:%M %p')}"
                    )
            if emp_tasks and emp_tasks.overdue > 0:
                alerts.append(f"⚠️ {emp_tasks.overdue} overdue EZLynx task(s)")

            employee_reports[emp] = EmployeeProductivityReport(
                employee_name=emp,
                inbound_total=len(inbound),
                inbound_answered=len(inbound_answered),
                inbound_missed=len(inbound_missed),
                inbound_voicemails=len(inbound_vms),
                outbound_total=len(outbound),
                total_talk_time_seconds=total_talk,
                missed_calls_resolved=len(resolved_inc),
                missed_calls_orphaned=len(orphaned_inc),
                missed_calls_pending=len(pending_inc),
                avg_callback_time_minutes=avg_resp,
                ezlynx_tasks_completed=emp_tasks.completed_today if emp_tasks else (0 if tasks_provided else None),
                ezlynx_tasks_overdue=emp_tasks.overdue if emp_tasks else (0 if tasks_provided else None),
                ezlynx_activities_logged=emp_act.notes_count if emp_act else (0 if activities_provided else None),
                productivity_score=score,
                score_status=score_status,
                data_limitations=limitations,
                alerts=alerts,
            )

        # Capture unassigned or general queue orphaned calls to prevent lost accounts
        unassigned_orphaned = [i for i in incidents if i.employee_name == "Unassigned" and i.status == "ORPHANED_ALERT"]
        for inc in unassigned_orphaned:
            critical_alerts.append(
                f"• Unassigned / General Queue: Unreturned call from {format_phone(inc.caller_phone)} at {inc.missed_at.strftime('%I:%M %p')}"
            )

        return {
            "timestamp": ref_time.isoformat(),
            "critical_alerts": critical_alerts,
            "incidents": [asdict(i) for i in incidents],
            "employee_reports": {k: asdict(v) for k, v in employee_reports.items()},
        }

    def audit_non_clients(
        self,
        calls: List[RingCentralCall],
        known_client_phones: Optional[set] = None,
    ) -> List[NonClientLeadAudit]:
        """Audits inbound calls from prospects / non-clients to verify callback SLA on new leads."""
        client_set = known_client_phones or set()
        sorted_calls = sorted(calls, key=lambda c: c.start_time)
        outbound_calls = [c for c in sorted_calls if c.direction == "Outbound" and c.to_number]
        
        inbound_non_clients: Dict[str, List[RingCentralCall]] = defaultdict(list)
        for c in sorted_calls:
            if c.direction == "Inbound" and c.from_number:
                if c.from_number not in client_set:
                    inbound_non_clients[c.from_number].append(c)

        audits: List[NonClientLeadAudit] = []
        for phone, call_list in inbound_non_clients.items():
            first_call = call_list[0]
            name = first_call.employee_name if first_call.employee_name != "Unassigned" else "Prospect / Unknown"
            
            # Check if any outbound call went to this lead
            matching_outbound = [
                c for c in outbound_calls
                if c.to_number == phone and c.start_time >= first_call.start_time
            ]
            
            if matching_outbound:
                first_cb = matching_outbound[0]
                diff_mins = (first_cb.start_time - first_call.start_time).total_seconds() / 60.0
                audits.append(
                    NonClientLeadAudit(
                        phone_number=phone,
                        caller_name=name,
                        first_call_time=first_call.start_time,
                        calls_count=len(call_list),
                        was_returned=True,
                        returned_by=first_cb.employee_name,
                        response_time_minutes=round(diff_mins, 1),
                    )
                )
            else:
                audits.append(
                    NonClientLeadAudit(
                        phone_number=phone,
                        caller_name=name,
                        first_call_time=first_call.start_time,
                        calls_count=len(call_list),
                        was_returned=False,
                        returned_by=None,
                        response_time_minutes=None,
                    )
                )

        return audits

    def analyze_magellan_sentiment(
        self,
        calls: List[RingCentralCall],
        hold_time_threshold_seconds: int = 180,
    ) -> List[Dict[str, Any]]:
        """Identifies calls with negative or frustrated sentiment and long wait/hold times in Magellan/AI Receptionist."""
        flagged = []
        for c in calls:
            # Detect long hold times or repetitive voicemails indicative of customer frustration
            is_long_hold = c.duration_seconds >= hold_time_threshold_seconds and c.result in ("Voicemail", "Missed")
            if is_long_hold:
                flagged.append({
                    "call_id": c.call_id,
                    "phone": c.from_number,
                    "caller_name": c.employee_name,
                    "duration_seconds": c.duration_seconds,
                    "timestamp": c.start_time.isoformat(),
                    "sentiment": "FRUSTRATED_HIGH_RISK",
                    "reason": f"Client abandoned after {round(c.duration_seconds / 60.0, 1)}m wait/voicemail",
                })
        return flagged

    def format_critical_alerts_card(self, audit: Dict[str, Any], title: str = "🚨 CRITICAL ACCOUNT RISK: Unreturned Missed Calls (>30m SLA)") -> Optional[str]:
        """Formats only the critical unreturned call alerts as a focused action message."""
        critical_alerts = audit.get("critical_alerts", [])
        if not critical_alerts:
            return None
        lines = [
            f"🚨 **{title}**\n",
            "⚠️ *Immediate Action Required:* The following client calls/voicemails have breached the 30-minute SLA without any outbound callback or contact:\n",
        ]
        lines.extend(critical_alerts)
        return "\n".join(lines)

    def format_google_chat_card(
        self,
        audit: Dict[str, Any],
        title: str = "Daily Agency Productivity & Account Risk Report",
        include_critical_section: bool = True,
    ) -> str:
        """Formats the audit into an executive-ready Google Chat message."""
        lines = [f"📊 **{title}**\n"]

        critical_alerts = audit.get("critical_alerts", [])
        if include_critical_section:
            if critical_alerts:
                lines.append("🚨 **CRITICAL ACCOUNT RISK (Unreturned Missed Calls):**")
                lines.extend(critical_alerts)
                lines.append("")
            else:
                lines.append("✅ **No orphaned missed calls. All client contacts handled.**\n")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        for emp, data in audit.get("employee_reports", {}).items():
            score = data.get("productivity_score")
            if score is None:
                score_label = data.get("score_status", "UNVERIFIED")
                lines.append(f"👤 **{emp}** — ⚪ **Score: {score_label}**")
            else:
                score_emoji = "🟢" if score >= 85 else ("🟡" if score >= 70 else "🔴")
                lines.append(f"👤 **{emp}** — {score_emoji} **Score: {score}/100**")
            
            # Call metrics
            in_tot = data.get("inbound_total", 0)
            in_ans = data.get("inbound_answered", 0)
            in_miss = data.get("inbound_missed", 0)
            in_vm = data.get("inbound_voicemails", 0)
            out_tot = data.get("outbound_total", 0)
            talk_mins = round(data.get("total_talk_time_seconds", 0) / 60.0, 1)

            lines.append(
                f"  • **RingCentral:** {in_tot} Inbound ({in_ans} Answered, {in_miss + in_vm} Missed) | {out_tot} Outbound | {talk_mins}m talk time"
            )

            # Callback reconciliation
            resolved = data.get("missed_calls_resolved", 0)
            orphaned = data.get("missed_calls_orphaned", 0)
            avg_resp = data.get("avg_callback_time_minutes")
            avg_resp_str = f"{avg_resp}m avg" if avg_resp is not None else "N/A"
            lines.append(f"  • **Missed Call Resolution:** {resolved} Returned ({avg_resp_str}) | {orphaned} Unreturned 🚨" if orphaned else f"  • **Missed Call Resolution:** {resolved} Returned ({avg_resp_str}) ✅")

            # EZLynx metrics
            tasks_comp = data.get("ezlynx_tasks_completed")
            tasks_od = data.get("ezlynx_tasks_overdue")
            act_notes = data.get("ezlynx_activities_logged")
            tasks_comp = "UNVERIFIED" if tasks_comp is None else tasks_comp
            tasks_od = "UNVERIFIED" if tasks_od is None else tasks_od
            act_notes = "UNVERIFIED" if act_notes is None else act_notes
            lines.append(f"  • **EZLynx:** {tasks_comp} Tasks Completed | {tasks_od} Overdue | {act_notes} Activity Notes Logged")

            for limitation in data.get("data_limitations", []):
                lines.append(f"  • ⚠️ **Data limitation:** {limitation}")

            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)


def format_phone(raw: str) -> str:
    """Formats 10 digits as (XXX) XXX-XXXX."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw
