"""Wiring: validated Gmail CSV ingestion -> the four ROBIE verification workers.

Process truth: ~/workspace/robie-manual-ops/worker-sops.md (SOPs) and
call-pack.md (Carlo's approved call scripts, 2026-09-13). Identity rules:
gmail_report_ingestion.identity_value (Policy Number per policy for
4247/4246/4372; per request for 4359 — Carlo 2026-09-19).

Pipeline per worker run:
  1. INGEST — today's validated CSV via gmail_report_ingestion
     (fail-closed; --csv injects bytes for tests).
  2. WORK ITEMS — built from validated rows, grouped by department.
  3. 4246 AUDIT QUEUE IS INCREMENTAL (Carlo 2026-09-14): a persistent JSON
     working queue. Each run adds only NEW policies entering the window
     (dedupe key: Policy Number + renewal/effective date). Open items carry
     forward with follow-up state; items close only when papers are
     received, reconciled, and filed.
  4. NEXT ACTION — per-SOP contact ladder: PORTAL first, then EMAIL, then
     CALL. Carriers/MGAs/mortgage companies ONLY — never clients.
     Business hours only (weekdays 9 AM-5 PM ET). Never bind/quote/cancel.
     4372 runs mortgagee_enrichment (DocumentApi + structured fields,
     dry-run by default) before planning. Portal/Bland stay out of that
     scaffold. HITL on source conflict; skip only on proven-zero.
  5. EVIDENCE — every action records destination evidence; in dry-run the
     planned action is recorded as evidence of intent.
  6. DIGEST — done / not done / pending + reason, per policy, grouped by
     department.

MODES:
  dry_run (default): NO emails, NO calls, NO EZLynx writes. Outreach steps
      are recorded as planned actions.
  live: performs outreach through the integration adapters. Still bounded
      by the standing grant (never delete, never contact clients, business
      hours, no bind/quote/cancel, every action in the morning report).
      Requires --live AND ROBIE_LIVE_OUTREACH=1 in the environment.

Branch: ralph/gmail-scheduled-report-ingestion (PR #495).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gmail_report_ingestion as ing  # noqa: E402
try:
    from . import mortgagee_enrichment as menc  # noqa: E402
except ImportError:  # script-style: python robie_job_engine/verification_workers.py
    import mortgagee_enrichment as menc  # noqa: E402

WORKERS = {
    "4247": {
        "name": "Manual Renewals",
        "queue": "Manual Renewal Queue - ROBIE",
        "timing": "work each policy 30-45 days before its renewal date",
    },
    "4246": {
        "name": "Audit Verifications",
        "queue": "Audit Verification Queue - ROBIE",
        "timing": "work each workers-comp policy 30-45 days AFTER it renews",
    },
    "4372": {
        "name": "Mortgagee Verifications",
        "queue": "Mortgagee Verification Queue - ROBIE",
        "timing": "verify + deliver dec package 30-45 days BEFORE expiration; "
                  "7-day payment sweep after delivery",
    },
    "4359": {
        "name": "Policy Change Checks",
        "queue": "Policy Change Request Confirmation Queue - ROBIE",
        "timing": "follow up continuously; never badger inside 24-48 business hours",
    },
}

DEPARTMENT_ORDER = [
    "Commercial Lines",
    "Personal Lines",
    "Trucking and Transportation",
    "Operations",
    "Accounting",
]

LIVE_ENV_VAR = "ROBIE_LIVE_OUTREACH"


# --- dates -----------------------------------------------------------------


def parse_csv_date(value: str) -> date | None:
    """Parse the date shapes EZLynx exports use. None = unparseable."""
    text = (value or "").strip()
    if not text:
        return None
    for fmt in (
        "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
        "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # "MM/DD/YYYY - MM/DD/YYYY" policy-term ranges: take the END date.
    m = re.findall(r"(\d{1,2}/\d{1,2}/\d{2,4})", text)
    if len(m) >= 2:
        return parse_csv_date(m[-1])
    if len(m) == 1:
        return parse_csv_date(m[0])
    return None


def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def business_days_between(a: date, b: date) -> int:
    """Business days from a (exclusive) to b (inclusive). Negative if b < a."""
    if a == b:
        return 0
    step = 1 if b > a else -1
    count, d = 0, a
    while d != b:
        d += timedelta(days=step)
        if is_business_day(d):
            count += step
    return count


def in_business_hours(now: datetime | None = None) -> bool:
    """Weekdays 9 AM-5 PM Eastern — the only window for outreach."""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        et = now
    return et.weekday() < 5 and 9 <= et.hour < 17


# --- work items ------------------------------------------------------------


@dataclass
class WorkItem:
    key: str  # identity_value: per policy, or per request for 4359
    report_id: str
    policy_number: str
    account_name: str
    department: str
    carrier: str
    producer: str
    csr: str
    effective_date: str = ""
    expiration_date: str = ""
    row: dict = field(default_factory=dict)


def _cell(row: dict, *names: str) -> str:
    for name in names:
        if name in row:
            return (row.get(name) or "").strip()
    return ""


def build_work_items(report_id: str, ingested: ing.IngestedReport) -> list[WorkItem]:
    """One work item per policy (4247/4246/4372) or per request (4359).

    Duplicate rows for one policy on the per-policy reports are linked:
    the first row wins and the dup count is noted — the worker tracks one
    item per policy.
    """
    items: list[WorkItem] = []
    seen: dict[str, WorkItem] = {}
    dup_counts: dict[str, int] = {}
    for row in ingested.rows:
        key = ing.identity_value(report_id, row)
        policy = _cell(row, "Policy Number")
        if report_id != "4359" and key in seen:
            dup_counts[key] = dup_counts.get(key, 1) + 1
            continue
        item = WorkItem(
            key=key,
            report_id=report_id,
            policy_number=policy,
            account_name=_cell(row, "Account Name"),
            department=_cell(row, "Department", "Branch") or "Unassigned",
            carrier=_cell(row, "Master Company"),
            producer=_cell(row, "Assigned Producer"),
            csr=_cell(row, "CSR"),
            effective_date=_cell(row, "Policy Effective Date", "Effective Date"),
            expiration_date=_cell(row, "Policy Expiration Date"),
            row=dict(row),
        )
        # 4246's daily feed (4360 format) carries Effective Date directly;
        # the Policy Term range fallback below covers only feeds without it.
        if report_id == "4246" and not item.effective_date:
            term = _cell(row, "Policy Term")
            m = re.findall(r"(\d{1,2}/\d{1,2}/\d{2,4})", term)
            if m:
                item.effective_date = m[0]
        seen[key] = item
        items.append(item)
    for key, count in dup_counts.items():
        seen[key].row["_linked_duplicate_rows"] = str(count)
    return items


def _4372_closed_reason(item: WorkItem) -> str | None:
    """Return the exclusion reason when a 4372 task is closed; None = keep.

    Added 2026-09-19: the 4372 daily CSV can deliver CLOSED tasks (the
    report filter does not exclude them — proven on the 2026-09-19
    delivery, all 4 rows Closed). A closed task is finished work and must
    never become a work item, regardless of its due date. Only an exact
    "Closed" status (case-insensitive) excludes; any other status (Open,
    blank, unknown) keeps the item so missing data never silently drops
    work.
    """
    status = _cell(item.row, "Task Status").strip().casefold()
    if status == "closed":
        closed_date = _cell(item.row, "Task Closed Date")
        closed_by = _cell(item.row, "Task Closed By")
        detail = f"closed {closed_date}" if closed_date else "closed"
        if closed_by:
            detail += f" by {closed_by}"
        return f"task already {detail} — no work remaining"
    return None


# --- 4246 incremental audit queue ------------------------------------------


@dataclass
class AuditQueueEntry:
    key: str  # Policy Number + renewal date
    policy_number: str
    account_name: str
    department: str
    carrier: str
    renewal_date: str  # ISO date of the renewed term start
    first_seen: str  # ISO date entered the queue
    last_follow_up: str = ""  # ISO date
    next_due: str = ""  # ISO date
    follow_ups: int = 0
    escalated: bool = False
    status: str = "open"  # open | closed
    history: list = field(default_factory=list)


class AuditQueue:
    """Persistent incremental audit queue (Carlo 2026-09-14).

    Each run adds only NEW policies entering the 30-day window. Open items
    carry forward with their follow-up state. Items close only when papers
    are received, reconciled, and filed — never by aging out.
    """

    def __init__(self, path: str):
        self.path = path
        self.entries: dict[str, AuditQueueEntry] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for key, raw in (data.get("entries") or {}).items():
            self.entries[key] = AuditQueueEntry(**raw)

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"entries": {k: asdict(v) for k, v in self.entries.items()}},
                fh, indent=2, sort_keys=True,
            )
        os.replace(tmp, self.path)

    @staticmethod
    def entry_key(policy_number: str, renewal_iso: str) -> str:
        return f"{policy_number} | {renewal_iso}"

    def sync(self, items: list[WorkItem], today: date) -> tuple[list[str], list[str]]:
        """Add only new policies; return (added_keys, carried_keys)."""
        added, carried = [], []
        for item in items:
            renewal = parse_csv_date(item.effective_date)
            renewal_iso = renewal.isoformat() if renewal else ""
            key = self.entry_key(item.policy_number, renewal_iso)
            existing = self.entries.get(key)
            if existing is None:
                self.entries[key] = AuditQueueEntry(
                    key=key,
                    policy_number=item.policy_number,
                    account_name=item.account_name,
                    department=item.department,
                    carrier=item.carrier,
                    renewal_date=renewal_iso,
                    first_seen=today.isoformat(),
                    next_due=today.isoformat(),
                    history=[f"{today.isoformat()}: entered queue"],
                )
                added.append(key)
            elif existing.status == "open":
                carried.append(key)
        return added, carried

    def open_entries(self) -> list[AuditQueueEntry]:
        return [e for e in self.entries.values() if e.status == "open"]

    def record_follow_up(self, key: str, today: date, action: str,
                         next_due: date, escalated: bool = False) -> None:
        entry = self.entries[key]
        entry.last_follow_up = today.isoformat()
        entry.next_due = next_due.isoformat()
        entry.follow_ups += 1
        entry.escalated = entry.escalated or escalated
        entry.history.append(f"{today.isoformat()}: {action}")

    def close(self, key: str, today: date, reason: str) -> None:
        entry = self.entries[key]
        entry.status = "closed"
        entry.history.append(f"{today.isoformat()}: CLOSED — {reason}")


# --- next-action planning ---------------------------------------------------


@dataclass
class ActionPlan:
    kind: str  # portal | email | call | verify | file | sweep | wait | escalate
    detail: str  # plain-English what and why
    target: str = ""  # carrier / lender / desk
    due: str = ""  # ISO date


@dataclass
class PlannedAction:
    item_key: str
    policy_number: str
    account_name: str
    department: str
    worker: str
    action: ActionPlan
    status: str  # due_now | waiting | blocked
    reason: str  # plain-English reason for the digest
    mode: str  # dry_run | live


def _dept(item: WorkItem) -> str:
    return item.department or "Unassigned"


def plan_4247(item: WorkItem, today: date) -> tuple[ActionPlan, str, str]:
    """Manual renewal: most urgent first; Progressive BOR download check."""
    exp = parse_csv_date(item.expiration_date)
    if exp is None:
        return (ActionPlan("verify",
                           "Expiration date unreadable — confirm renewal date in EZLynx before working",
                           target=item.carrier),
                "blocked", "expiration date unreadable")
    days = (exp - today).days
    if days < 0:
        return (ActionPlan("verify",
                           f"Renewal expired {abs(days)} days ago — confirm status in EZLynx",
                           target=item.carrier),
                "blocked", "already expired — needs review")
    carrier = item.carrier or "carrier"
    if "progressive" in carrier.casefold():
        # Carlo 2026-09-14: Progressive manuals are BOR takeovers — the
        # renewal should download automatically.
        return (ActionPlan("verify",
                           "Progressive BOR takeover — check whether the renewal downloaded into "
                           "EZLynx. If downloaded: done via download. If not: chase it.",
                           target=carrier, due=today.isoformat()),
                "due_now", f"expires in {days}d — check EZLynx download first")
    return (ActionPlan("portal",
                       f"Pull renewal packet from {carrier} portal; if missing, email then call. "
                       f"Expires in {days} days.",
                       target=carrier, due=today.isoformat()),
            "due_now", f"expires in {days}d — renewal packet needed")


def plan_4246(entry: AuditQueueEntry, today: date) -> tuple[ActionPlan, str, str]:
    """Audit: day-30 entry, follow-ups every 3-5 business days, day-45 enforce."""
    renewal = parse_csv_date(entry.renewal_date)
    carrier = entry.carrier or "carrier"
    if renewal is None:
        return (ActionPlan("verify",
                           "Renewal date unreadable — confirm the renewed term in EZLynx",
                           target=carrier),
                "blocked", "renewal date unreadable")
    days_since = (today - renewal).days
    if days_since >= 45 and not entry.escalated:
        return (ActionPlan("escalate",
                           f"Day {days_since}: ENFORCE — call the carrier audit desk directly, cite "
                           "non-compliance surcharge risk, log the escalation",
                           target=carrier, due=today.isoformat()),
                "due_now", f"day {days_since} — escalation due")
    if entry.last_follow_up:
        last = parse_csv_date(entry.last_follow_up) or today
        idle = business_days_between(last, today)
        if idle < 3:
            return (ActionPlan("wait",
                               f"Last follow-up {idle} business days ago — next touch due "
                               f"{entry.next_due or 'per cadence'}",
                               target=carrier, due=entry.next_due),
                    "waiting", f"waiting — next follow-up {entry.next_due or 'due'}")
    pie = "pie" in carrier.casefold()
    if pie:
        return (ActionPlan("call",
                           "Pie Insurance Partner Support 855-965-1840 — ask for the final payroll "
                           "audit statement; have it emailed to robie@streetsmart.insurance",
                           target="Pie Insurance 855-965-1840", due=today.isoformat()),
                "due_now", f"day {days_since} — audit papers outstanding")
    return (ActionPlan("portal",
                       f"Check {carrier} portal for the final payroll audit statement; "
                       "then email, then call. Request it at robie@streetsmart.insurance",
                       target=carrier, due=today.isoformat()),
            "due_now", f"day {days_since} — audit papers outstanding")


def plan_4372(
    item: WorkItem,
    today: date,
    enrichment: menc.EnrichmentResult | None = None,
    *,
    lender_checks: list[menc.MortgageLenderCheck] | None = None,
    verify_lender_fn=None,
    verify_of_record_fn=None,
    producer_gate_fn=None,
) -> tuple[ActionPlan, str, str]:
    """Mortgagee: structured enrichment first, then lender input checks.

    Closed tasks never reach this planner (see ``_4372_closed_reason``).
    ``ready`` runs ``verify_lender`` per mortgage (not blocked forever for
    missing lender/loan). Producer gate still blocks delivery. Portal /
    Bland stay out of this scaffold. HITL on source conflict. Proven-zero
    is an explicit empty result, never a silent skip.
    """
    due = parse_csv_date(_cell(item.row, "Task Due Date"))
    due_txt = f"task due {due.isoformat()}" if due else "no task due date"
    result = enrichment
    if result is None:
        applicant_id = _cell(item.row, "Applicant ID")
        result = menc.enrich_work_item(
            policy_number=item.policy_number,
            applicant_id=applicant_id,
            row=item.row,
            ports=menc.EnrichmentPorts(),
            dry_run=True,
        )
    kind, detail, target, status, reason = menc.plan_from_enrichment(
        result,
        due_txt=due_txt,
        property_zip=menc.property_zip_from_row(item.row) or (
            result.property_zip if result is not None else ""
        ),
        portal_lookup=item.row.get("portal_lender_lookup")
        if isinstance(item.row.get("portal_lender_lookup"), dict)
        else None,
        producer_state=item.row,
        lender_checks=lender_checks,
        verify_lender_fn=verify_lender_fn,
        verify_of_record_fn=verify_of_record_fn,
        producer_gate_fn=producer_gate_fn,
    )
    return (ActionPlan(kind, detail, target=target, due=today.isoformat()),
            status, reason)


def plan_4359(item: WorkItem, today: date) -> tuple[ActionPlan, str, str]:
    """Policy change: respect 24-48 business-hour turnaround; 3-way match to close."""
    created = parse_csv_date(_cell(item.row, "Change Request Created Date"))
    carrier = item.carrier or "carrier"
    if created is None:
        return (ActionPlan("verify",
                           "Request created date unreadable — confirm request age before outreach",
                           target=carrier),
                "blocked", "request age unknown")
    age_bd = business_days_between(created, today)
    if age_bd < 2:
        return (ActionPlan("wait",
                           f"Request is {age_bd} business days old — inside the 24-48h carrier "
                           "turnaround window; do not badger",
                           target=carrier),
                "waiting", "inside carrier turnaround window")
    return (ActionPlan("portal",
                       f"Check {carrier} portal for the issued endorsement/revised dec; "
                       "if missing, email the underwriter, then call. Close only on 3-way match "
                       "(requested change vs carrier doc vs EZLynx).",
                       target=carrier, due=today.isoformat()),
            "due_now", f"open {age_bd} business days — follow-up due")


PLANNERS = {"4247": None, "4246": None, "4372": None, "4359": None}  # wired in run_worker


# --- run --------------------------------------------------------------------


@dataclass
class WorkerRun:
    report_id: str
    worker: str
    day: str
    mode: str
    ingested_rows: int
    work_items: int
    actions: list = field(default_factory=list)  # PlannedAction
    audit_added: list = field(default_factory=list)
    audit_carried: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    excluded_stale: list = field(default_factory=list)
    enrichment: list = field(default_factory=list)


def _execute_live_action(pa: PlannedAction, item: WorkItem | None) -> str:
    """Execute one planned action via real adapters. Returns evidence note.

    Raises AdapterError if the action cannot be performed — the caller
    catches it and leaves the action pending with the error as reason.
    Only real destination evidence (message ID, call ID, note ID) counts
    as done. Nothing here ever claims work it didn't do.
    """
    from worker_adapters import (
        AdapterError,
        BlandCallAdapter,
        EZLynxNoteAdapter,
        ROBIE_EMAIL,
        RobieEmailAdapter,
    )
    kind = pa.action.kind
    if kind == "email":
        # Target email must come from the work item row (carrier desk).
        to = (item.row.get("Carrier Email") or item.row.get("Underwriter Email")
              or "").strip() if item else ""
        if not to:
            raise AdapterError(
                "no carrier desk email on file for this item — "
                "cannot send")
        adapter = RobieEmailAdapter()
        ev = adapter.send(
            to=to,
            subject=f"[{pa.worker}] Policy {pa.policy_number}",
            body=f"Hello,\n\n{pa.action.detail}\n\n"
                 f"— Robie, producer assistant with StreetSmart Insurance\n"
                 f"{ROBIE_EMAIL}",
        )
        return f"EMAIL sent: {ev.detail} (id {ev.destination_id})"
    if kind == "call":
        # Calls go to carriers/MGAs/lenders ONLY — never clients.
        # The number must be on file; we never dial the insured.
        to = (item.row.get("Carrier Phone") or item.row.get("Lender Phone")
              or "").strip() if item else ""
        if not to:
            raise AdapterError(
                "no carrier/lender phone on file for this item — "
                "cannot call")
        adapter = BlandCallAdapter()
        ev = adapter.call(to=to, task=pa.action.detail)
        return f"CALL placed: {ev.detail}"
    if kind in ("note", "file"):
        applicant_id = (item.row.get("Applicant ID") or "").strip() if item else ""
        if not applicant_id:
            raise AdapterError("no Applicant ID on file — cannot file note")
        adapter = EZLynxNoteAdapter()
        ev = adapter.file_note(
            applicant_id=applicant_id,
            discussion_title=f"{pa.worker} — {pa.policy_number}",
            body=pa.action.detail,
        )
        return f"NOTE filed: {ev.detail} (id {ev.destination_id})"
    # portal / verify / wait / sweep / escalate are human or scheduled
    # steps — live mode records them as pending, never as done.
    raise AdapterError(
        f"action kind {kind!r} has no live adapter — held for manual step")


def _record_evidence(run: WorkerRun, action: PlannedAction, note: str) -> None:
    run.evidence.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "worker": action.worker,
        "item_key": action.item_key,
        "policy_number": action.policy_number,
        "action": action.action.kind,
        "detail": action.action.detail,
        "mode": run.mode,
        "note": note,
    })


def run_worker(report_id: str, *, day: date, mode: str = "dry_run",
               queue_dir: str = ".", csv_bytes: bytes | None = None,
               gmail_service=None, allow_unverified: bool = False,
               enrichment_ports: menc.EnrichmentPorts | None = None,
               test_enrichment: bool = False) -> WorkerRun:
    """Run one verification worker for one day. Fail-closed throughout."""
    worker = WORKERS[report_id]["name"]
    run = WorkerRun(report_id=report_id, worker=worker,
                    day=day.isoformat(), mode=mode,
                    ingested_rows=0, work_items=0)

    # 4359 stays gated until its schema is verified (Carlo's rule).
    ing.check_report_gate(report_id, allow_unverified=allow_unverified)

    # 1. INGEST
    if csv_bytes is not None:
        rows, skipped = ing.parse_and_validate_csv(report_id, csv_bytes,
                                                   source_label="injected")
        ingested = ing.IngestedReport(
            report_id=report_id,
            display_name=ing.REPORT_DISPLAY_NAMES[report_id],
            received_at=datetime.now(timezone.utc).isoformat(),
            message_id_sha256="", filename_sha256="", attachment_sha256="",
            row_count=len(rows), skipped_rows=skipped, rows=rows,
        )
    elif gmail_service is not None:
        allow_unverified = report_id == "4359"
        got = ing.ingest_daily_reports(gmail_service, day=day,
                                       report_ids=[report_id],
                                       allow_unverified=allow_unverified)
        ingested = got[report_id]
    else:
        raise ing.GmailReportIngestionError(
            "run_worker needs csv_bytes or gmail_service")
    run.ingested_rows = ingested.row_count

    # 2. WORK ITEMS
    items = build_work_items(report_id, ingested)

    # 2b. 4372 closed/stale exclusion. A closed task is finished work and
    # never becomes a work item; an old task due date is a prior cycle's
    # work (active policy status alone does NOT make it current).
    # Excluded items are recorded with a reason, never silently dropped.
    if report_id == "4372":
        kept: list[WorkItem] = []
        for item in items:
            reason = _4372_closed_reason(item)
            if reason:
                run.excluded_stale.append({
                    "item_key": item.key,
                    "policy_number": item.policy_number,
                    "account_name": item.account_name,
                    "reason": reason,
                })
            else:
                kept.append(item)
        items = kept
    run.work_items = len(items)

    # 3/4. PLAN per item
    live = mode == "live"
    if live and os.environ.get(LIVE_ENV_VAR) != "1":
        raise ing.GmailReportIngestionError(
            f"live mode requires {LIVE_ENV_VAR}=1 in the environment")
    if live and not in_business_hours():
        run.errors.append("live mode requested outside business hours — "
                          "outreach held; run in dry_run or wait for weekday 9-5 ET")

    if report_id == "4246":
        queue = AuditQueue(os.path.join(queue_dir, "audit-working-queue.json"))
        added, carried = queue.sync(items, day)
        run.audit_added = added
        run.audit_carried = carried
        for entry in queue.open_entries():
            action, status, reason = plan_4246(entry, day)
            pa = PlannedAction(entry.key, entry.policy_number, entry.account_name,
                               entry.department, worker, action, status, reason, mode)
            run.actions.append(pa)
            _record_evidence(run, pa, "audit queue state evaluated")
        queue.save()
    else:
        planner = {"4247": plan_4247, "4372": plan_4372, "4359": plan_4359}[report_id]
        # 4247: most urgent first.
        if report_id == "4247":
            items.sort(key=lambda it: (parse_csv_date(it.expiration_date)
                                       or date.max))
        for item in items:
            enrichment = None
            if report_id == "4372":
                ports = menc.resolve_enrichment_ports(
                    enrichment_ports, live_test=test_enrichment,
                )
                enrichment = menc.enrich_work_item(
                    policy_number=item.policy_number,
                    applicant_id=_cell(item.row, "Applicant ID"),
                    row=item.row,
                    ports=ports,
                    dry_run=(mode != "live"),
                )
                payload = enrichment.to_dict()
                lender_checks = None
                if enrichment.status == menc.STATUS_READY:
                    zip_code = (
                        menc.property_zip_from_row(item.row) or enrichment.property_zip
                    )
                    lender_checks = menc.check_ready_mortgages(
                        enrichment.mortgages,
                        property_zip=zip_code,
                        portal_lookup=item.row.get("portal_lender_lookup")
                        if isinstance(item.row.get("portal_lender_lookup"), dict)
                        else None,
                    )
                    payload["lender_checks"] = [c.to_dict() for c in lender_checks]
                    clear, gate_reason = menc.producer_gate_from_row(item.row)
                    payload["producer"] = {
                        "clear": clear,
                        "reason": gate_reason,
                    }
                item.row["_mortgagee_enrichment"] = payload
                run.enrichment.append(payload)
                action, status, reason = plan_4372(
                    item, day, enrichment=enrichment, lender_checks=lender_checks,
                )
            else:
                action, status, reason = planner(item, day)
            pa = PlannedAction(item.key, item.policy_number, item.account_name,
                               _dept(item), worker, action, status, reason, mode)
            run.actions.append(pa)
            if live and status == "in_progress":
                # Live: actually execute via adapters. Only real destination
                # evidence marks an action done; any adapter failure leaves
                # it pending with the error as the reason. Nothing here
                # ever claims work it didn't do.
                try:
                    evidence_note = _execute_live_action(pa, item)
                    pa.status = "done"
                    pa.reason = evidence_note
                    _record_evidence(run, pa, evidence_note)
                except Exception as exc:  # AdapterError and friends
                    pa.status = "pending"
                    pa.reason = (f"live execution failed: {exc} — "
                                 f"original plan: {reason}")
                    _record_evidence(run, pa, pa.reason)
            else:
                _record_evidence(run, pa,
                                 "planned — dry_run, no outreach performed"
                                 if not live else
                                 f"held: {reason}")
    return run


# --- digest ------------------------------------------------------------------


def build_digest(runs: list[WorkerRun]) -> str:
    """Morning digest: done / not done / pending + reason, by department.

    Plain English for agency staff. Sections follow Carlo's format.
    """
    lines = []
    for run in runs:
        lines.append(f"## {run.worker} ({run.report_id}) — {run.day} [{run.mode}]")
        lines.append(f"Rows ingested: {run.ingested_rows}; work items: {run.work_items}.")
        if run.report_id == "4246":
            lines.append(f"Audit queue: {len(run.audit_added)} new, "
                         f"{len(run.audit_carried)} carried forward.")
        by_dept: dict[str, dict[str, list]] = {}
        for pa in run.actions:
            bucket = by_dept.setdefault(pa.department,
                                        {"due_now": [], "waiting": [], "blocked": []})
            bucket[pa.status].append(pa)
        for dept in DEPARTMENT_ORDER + sorted(
                d for d in by_dept if d not in DEPARTMENT_ORDER):
            bucket = by_dept.get(dept)
            if not bucket:
                continue
            lines.append(f"### {dept}")
            for label, key in (("Needs action", "due_now"),
                               ("Waiting", "waiting"),
                               ("Blocked", "blocked")):
                for pa in bucket[key]:
                    lines.append(
                        f"- [{label}] {pa.policy_number} — {pa.account_name}: "
                        f"{pa.action.detail} (Reason: {pa.reason})"
                    )
        if run.errors:
            for err in run.errors:
                lines.append(f"! ERROR: {err}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# --- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ROBIE verification workers")
    parser.add_argument("--report", required=True,
                        choices=sorted(WORKERS),
                        help="4247 renewals, 4246 audits, 4372 mortgagee, 4359 changes")
    parser.add_argument("--day", default=date.today().isoformat(),
                        help="YYYY-MM-DD (default: today)")
    parser.add_argument("--mode", default="dry_run", choices=["dry_run", "live"])
    parser.add_argument("--queue-dir", default=".",
                        help="state dir for the audit working queue")
    parser.add_argument("--csv", default=None,
                        help="inject CSV bytes instead of reading Gmail")
    parser.add_argument("--out", default=None,
                        help="write digest markdown here (default: stdout)")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="bypass the schema gate (4359 until verified)")
    parser.add_argument("--test-enrichment", action="store_true",
                        help="bind Test-only EzlynxApiClient (ROBIE_ENV=TEST required; "
                             "never Production)")
    args = parser.parse_args(argv)

    day = datetime.strptime(args.day, "%Y-%m-%d").date()
    csv_bytes = None
    if args.csv:
        with open(args.csv, "rb") as fh:
            csv_bytes = fh.read()
    run = run_worker(args.report, day=day, mode=args.mode,
                     queue_dir=args.queue_dir, csv_bytes=csv_bytes,
                     allow_unverified=args.allow_unverified,
                     test_enrichment=args.test_enrichment)
    digest = build_digest([run])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(digest)
        print(f"digest written to {args.out}")
    else:
        print(digest)
    print(f"items={run.work_items} actions={len(run.actions)} "
          f"evidence={len(run.evidence)} errors={len(run.errors)}",
          file=sys.stderr)
    return 0 if not run.errors else 2


if __name__ == "__main__":
    sys.exit(main())
