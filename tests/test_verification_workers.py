"""Regression tests for the four ROBIE verification workers.

Covers (2026-09-19):
  - 4372 stale/prior-cycle exclusion (Fix 1): active policy status alone
    does not make an old mortgagee task current; excluded items are
    recorded with a reason, never silently dropped.
  - 4246 live contact/context propagation (Fix 2): the 4246 branch builds
    a real WorkItem from the audit entry (never None); call actions prefer
    an explicit carrier desk number from the plan over failing; genuinely
    missing contact info fails closed via AdapterError.
  - DWD fetcher (Fix 3): pagination, strict delivery-date enforcement,
    duplicate rejection, atomic writes.
  - 4359 registry sync, full JSON status_counts, due_now live execution,
    note adapter exact-title success + API-only failure.
"""

import base64
import csv
import io
import json
import os
import sys
import types
from datetime import date, datetime, timezone

import pytest

import robie_job_engine.gmail_report_ingestion as ing
import robie_job_engine.verification_workers as vw
import robie_job_engine.fetch_worker_csvs_dwd as dwd
from robie_job_engine import report_registry

DAY = date(2026, 9, 19)


def _csv_bytes(report_id: str, rows: list[dict]) -> bytes:
    headers = ing.expected_headers(report_id)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in headers})
    return buffer.getvalue().encode("utf-8")


def _row4372(policy, account, due, **extra):
    row = {
        "Applicant ID": "1",
        "Account Name": account,
        "Task Due Date": due,
        "Policy Number": policy,
        "Department": "Personal Lines",
        "Task Status": "Open",
    }
    row.update(extra)
    return row


# --- Fix 1: 4372 stale/prior-cycle exclusion -------------------------------


def _work_item_4372(policy, due, exp=""):
    return vw.WorkItem(
        key=policy, report_id="4372", policy_number=policy,
        account_name="Test", department="Personal Lines", carrier="X",
        producer="", csr="",
        row={"Task Due Date": due, "Policy Expiration Date": exp})


def test_4372_stale_expired_policy_excluded():
    # SAHO581361: Deleted, exp 2025-12-20 — prior cycle on both axes.
    reason = vw._4372_stale_reason(
        _work_item_4372("SAHO581361", "11/05/2025", "12/20/2025"), DAY)
    assert reason is not None
    assert "2025-11-05" in reason
    assert "policy expired 2025-12-20" in reason


def test_4372_stale_active_policy_still_excluded():
    # Ruth Cruz 4217318: Active, exp 2026-12-20 — but the task itself is a
    # prior-cycle task (due 2026-06-01, 110 days ago). Active policy status
    # alone must NOT make it current.
    reason = vw._4372_stale_reason(_work_item_4372("4217318", "06/01/2026"), DAY)
    assert reason is not None
    assert "active policy status alone does not make an old mortgagee task current" in reason


def test_4372_recent_task_kept():
    assert vw._4372_stale_reason(_work_item_4372("FLD272903", "09/15/2026"), DAY) is None


def test_4372_missing_due_date_never_excluded():
    # Missing data must not silently drop work.
    assert vw._4372_stale_reason(_work_item_4372("X", ""), DAY) is None


def test_4372_run_excludes_stale_and_records_reason(tmp_path):
    rows = [
        _row4372("SAHO581361", "Saeed Abbaszadeh", "11/05/2025",
                 **{"Policy Expiration Date": "12/20/2025"}),
        _row4372("4217318", "Ruth Cruz", "06/01/2026"),
        _row4372("HONJ046535", "Claudia Salgado & Paul Still", "06/15/2026"),
        _row4372("FLD272903", "Angela & Sean Marchak", "09/15/2026"),
    ]
    run = vw.run_worker("4372", day=DAY, mode="dry_run",
                        queue_dir=str(tmp_path),
                        csv_bytes=_csv_bytes("4372", rows))
    assert run.work_items == 1
    assert len(run.excluded_stale) == 3
    excluded_policies = {e["policy_number"] for e in run.excluded_stale}
    assert excluded_policies == {"SAHO581361", "4217318", "HONJ046535"}
    for entry in run.excluded_stale:
        assert entry["reason"]  # every exclusion carries a reason
    action_policies = {a.policy_number for a in run.actions}
    assert action_policies == {"FLD272903"}  # stale items never become actions
    # Digest shows the exclusions instead of dropping them silently.
    digest = vw.build_digest([run])
    assert "Excluded" in digest
    assert "SAHO581361" in digest


# --- Fix 2: 4246 live contact/context propagation --------------------------


def _row4246(policy, term, carrier):
    return {
        "Account Name": "Test Co",
        "Applicant ID": "220250093",
        "Policy Number": policy,
        "Policy Type": "Workers Comp",
        "Master Company": carrier,
        "Line Of Business": "Workers Comp",
        "Policy Term": term,
        "Department": "Commercial Lines",
        "Assigned Producer": "P",
        "CSR": "C",
    }


def _live_env(monkeypatch):
    monkeypatch.setenv(vw.LIVE_ENV_VAR, "1")
    monkeypatch.setattr(vw, "in_business_hours", lambda *a, **k: True)


def test_4246_live_builds_real_work_item_not_none(tmp_path, monkeypatch):
    _live_env(monkeypatch)
    seen = []

    def spy(pa, item):
        seen.append((pa, item))
        return "CALL placed: stub evidence"

    monkeypatch.setattr(vw, "_execute_live_action", spy)
    rows = [_row4246("WC123", "08/15/2026 - 08/15/2027", "Pie Insurance")]
    run = vw.run_worker("4246", day=DAY, mode="live",
                        queue_dir=str(tmp_path),
                        csv_bytes=_csv_bytes("4246", rows))
    assert len(seen) == 1  # the due_now Pie call was attempted
    pa, item = seen[0]
    assert item is not None, "4246 live path must pass a WorkItem, never None"
    assert isinstance(item, vw.WorkItem)
    assert item.policy_number == "WC123"
    assert item.carrier == "Pie Insurance"
    assert item.report_id == "4246"
    assert pa.status == "done"
    assert run.evidence and "stub evidence" in run.evidence[0]["note"]
    # Digest must survive the live statuses (done/pending) without KeyError.
    digest = vw.build_digest([run])
    assert "WC123" in digest


def test_4246_entry_without_source_row_still_builds(tmp_path):
    # Entries written before source_row existed load with an empty row.
    entry = vw.AuditQueueEntry(
        key="K", policy_number="WC9", account_name="A", department="D",
        carrier="Pie Insurance", renewal_date="2026-08-15",
        first_seen="2026-09-19")
    assert entry.source_row == {}
    item = vw._work_item_from_audit_entry(entry)
    assert item.policy_number == "WC9"
    assert item.carrier == "Pie Insurance"
    assert item.row["Policy Number"] == "WC9"


def _fake_worker_adapters(monkeypatch, **behaviors):
    """Install a fake `worker_adapters` module for _execute_live_action."""
    calls = {}
    fake = types.ModuleType("worker_adapters")

    class AdapterError(RuntimeError):
        pass

    class RobieEmailAdapter:
        def send(self, *, to, subject, body):
            calls["email"] = {"to": to, "subject": subject}
            if behaviors.get("email_fail"):
                raise AdapterError("email failed")
            dest = behaviors.get("email_id", "MSG1")
            return types.SimpleNamespace(
                channel="email", destination_id=dest,
                detail=f"email to {to} sent (id {dest})")

    class BlandCallAdapter:
        def call(self, *, to, task, transfer_to=None):
            calls["call"] = {"to": to, "task": task}
            if behaviors.get("call_fail"):
                raise AdapterError("call failed")
            cid = behaviors.get("call_id", "CALL1")
            return types.SimpleNamespace(
                channel="call", destination_id=cid,
                detail=f"call to {to} placed (call_id {cid})")

    class EZLynxNoteAdapter:
        def file_note(self, *, applicant_id, discussion_title, body):
            calls["note"] = {"applicant_id": applicant_id,
                             "title": discussion_title}
            if behaviors.get("note_fail"):
                raise AdapterError("note failed")
            nid = behaviors.get("note_id", "N1")
            return types.SimpleNamespace(
                channel="note", destination_id=nid,
                detail=f"note {nid} filed to '{discussion_title}'")

    fake.AdapterError = AdapterError
    fake.RobieEmailAdapter = RobieEmailAdapter
    fake.BlandCallAdapter = BlandCallAdapter
    fake.EZLynxNoteAdapter = EZLynxNoteAdapter
    fake.ROBIE_EMAIL = "robie@streetsmart.insurance"
    monkeypatch.setitem(sys.modules, "worker_adapters", fake)
    return calls, AdapterError


def test_4246_call_prefers_explicit_plan_phone(monkeypatch):
    calls, _ = _fake_worker_adapters(monkeypatch)
    # The 4246 Pie plan hardcodes the desk number; the export has no phone
    # column. The call must go to the explicit plan number, not fail.
    action, status, _ = vw.plan_4246(
        vw.AuditQueueEntry(key="K", policy_number="WC1", account_name="A",
                           department="D", carrier="Pie Insurance",
                           renewal_date="2026-08-15", first_seen="2026-09-19"),
        DAY)
    assert status == "due_now" and action.kind == "call"
    item = vw._work_item_from_audit_entry(
        vw.AuditQueueEntry(key="K", policy_number="WC1", account_name="A",
                           department="D", carrier="Pie Insurance",
                           renewal_date="2026-08-15", first_seen="2026-09-19"))
    pa = vw.PlannedAction("K", "WC1", "A", "D", "Audit Verifications",
                          action, status, "r", "live")
    note = vw._execute_live_action(pa, item)
    assert calls["call"]["to"] == "855-965-1840"
    assert "CALL placed" in note


def test_4246_call_fails_closed_without_any_phone(monkeypatch):
    _, AdapterError = _fake_worker_adapters(monkeypatch)
    # Non-Pie carrier: plan has no hardcoded number, row has no phone.
    entry = vw.AuditQueueEntry(key="K", policy_number="WC2", account_name="A",
                               department="D", carrier="Travelers",
                               renewal_date="2026-08-15", first_seen="2026-09-19")
    action, status, _ = vw.plan_4246(entry, DAY)
    assert action.kind == "portal"  # Travelers goes portal first
    # Force a call plan with no number anywhere to check fail-closed.
    action = vw.ActionPlan("call", "Call the carrier audit desk", target="Travelers")
    item = vw._work_item_from_audit_entry(entry)
    pa = vw.PlannedAction("K", "WC2", "A", "D", "Audit Verifications",
                          action, "due_now", "r", "live")
    with pytest.raises(AdapterError, match="no carrier/lender phone"):
        vw._execute_live_action(pa, item)


def test_4246_live_failure_leaves_action_pending(tmp_path, monkeypatch):
    _live_env(monkeypatch)
    _fake_worker_adapters(monkeypatch, call_fail=True)
    rows = [_row4246("WC123", "08/15/2026 - 08/15/2027", "Pie Insurance")]
    run = vw.run_worker("4246", day=DAY, mode="live",
                        queue_dir=str(tmp_path),
                        csv_bytes=_csv_bytes("4246", rows))
    pa = run.actions[0]
    assert pa.status == "pending"
    assert "live execution failed" in pa.reason
    assert run.evidence  # the failure itself is recorded as evidence


# --- due_now triggers live execution ----------------------------------------


def test_due_now_triggers_live_execution_not_just_planning(tmp_path, monkeypatch):
    _live_env(monkeypatch)
    executed = []

    def spy(pa, item):
        executed.append(pa.item_key)
        return "EMAIL sent: stub (id MSG1)"

    monkeypatch.setattr(vw, "_execute_live_action", spy)
    rows = [{
        "Account Name": "Acme",
        "Applicant ID": "1",
        "Policy Number": "POL1",
        "Policy Effective Date": "09/19/2025",
        "Policy Expiration Date": "10/09/2026",  # 20 days out -> due_now
        "Master Company": "Travelers",
        "Department": "Commercial Lines",
    }]
    run = vw.run_worker("4247", day=DAY, mode="live",
                        queue_dir=str(tmp_path),
                        csv_bytes=_csv_bytes("4247", rows))
    assert executed == ["POL1"], "due_now must invoke the live executor"
    assert run.actions[0].status == "done"


def test_waiting_actions_are_held_in_live_mode(tmp_path, monkeypatch):
    _live_env(monkeypatch)
    executed = []
    monkeypatch.setattr(vw, "_execute_live_action",
                        lambda pa, item: executed.append(pa.item_key) or "x")
    rows = [{
        "Account Name": "Acme",
        "Applicant ID": "1",
        "Policy Number": "POL9",
        "Policy Effective Date": "09/19/2025",
        "Policy Expiration Date": "10/09/2026",
        "Master Company": "Progressive",  # BOR path is due_now...
        "Department": "Commercial Lines",
    }]
    # 4359 inside the 24-48h turnaround window -> waiting, held by design.
    rows4359 = [{
        "Account Name": "Acme",
        "Applicant ID": "1",
        "Policy Number": "CHG1",
        "Master Company": "Travelers",
        "Department": "Commercial Lines",
        "Change Request Created Date": "09/18/2026",
    }]
    run = vw.run_worker("4359", day=DAY, mode="live",
                        queue_dir=str(tmp_path),
                        csv_bytes=_csv_bytes("4359", rows4359))
    assert run.actions[0].status == "waiting"
    assert executed == [], "waiting actions must not be executed"


# --- 4359 registry sync + status_counts -------------------------------------


def test_4359_registry_in_sync_with_ingestion_gate():
    spec = report_registry.VERIFIED_REPORTS["4359"]
    assert spec.schema_verified is True
    # The gate run_worker enforces must not block 4359 without a bypass.
    ing.check_report_gate("4359", allow_unverified=False)


def _row4247(policy, exp, carrier="Travelers", dept="Commercial Lines"):
    return {
        "Account Name": "Acme", "Applicant ID": "1", "Policy Number": policy,
        "Policy Effective Date": "09/19/2025", "Policy Expiration Date": exp,
        "Master Company": carrier, "Department": dept,
    }


def test_status_counts_cover_every_observed_status(tmp_path, monkeypatch):
    from robie_job_engine import run_daily_workers as rdw

    csv_dir = tmp_path / "csvs"
    csv_dir.mkdir()
    (csv_dir / "report_4247_2026-09-19.csv").write_bytes(_csv_bytes("4247", [
        _row4247("DUE1", "10/09/2026"),            # due_now
        _row4247("BLK1", "not-a-date"),            # blocked
        _row4247("PIE1", "10/09/2026", carrier="Pie Insurance"),  # due_now
    ]))
    (csv_dir / "report_4246_2026-09-19.csv").write_bytes(_csv_bytes("4246", [
        _row4246("AUD1", "08/15/2026 - 08/15/2027", "Pie Insurance"),
    ]))
    (csv_dir / "report_4372_2026-09-19.csv").write_bytes(_csv_bytes("4372", [
        _row4372("M1", "Lender Co", "09/15/2026"),  # blocked (enrichment)
    ]))
    (csv_dir / "report_4359_2026-09-19.csv").write_bytes(_csv_bytes("4359", [
        {"Account Name": "Acme", "Applicant ID": "1", "Policy Number": "CHG1",
         "Master Company": "Travelers", "Department": "Commercial Lines",
         "Change Request Created Date": "09/18/2026"},   # waiting
        {"Account Name": "Acme", "Applicant ID": "1", "Policy Number": "CHG2",
         "Master Company": "Travelers", "Department": "Commercial Lines",
         "Change Request Created Date": "09/01/2026"},   # due_now
    ]))
    queue_dir = tmp_path / "queue"
    out_dir = tmp_path / "out"
    rc = rdw.main(["--csv-dir", str(csv_dir), "--queue-dir", str(queue_dir),
                   "--out", str(out_dir), "--date", "2026-09-19"])
    assert rc == 0
    summary = json.loads((out_dir / "runs_2026-09-19.json").read_text())
    observed: set[str] = set()
    for entry in summary:
        observed |= set(entry["status_counts"])
    # Every observed status is counted — none silently dropped.
    assert observed == {"due_now", "blocked", "waiting"}, observed
    for entry in summary:
        assert sum(entry["status_counts"].values()) == (
            entry["done"] + entry["pending"]
            + sum(v for k, v in entry["status_counts"].items()
                  if k not in ("done", "pending")))


# --- Fix 3: DWD fetcher -----------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


class _FakeAttachments:
    def __init__(self, store):
        self.store = store

    def get(self, userId, messageId, id):
        store = self.store

        class Req:
            def execute(self):
                return {"data": _b64(store[messageId][id])}
        return Req()


class _FakeMessages:
    def __init__(self, pages, full, attachments):
        self.pages = pages
        self.full = full
        self.attachments_store = attachments

    def list(self, **kwargs):
        pages = self.pages

        class Req:
            def execute(self):
                token = kwargs.get("pageToken")
                idx = int(token) if token else 0
                page = pages[idx]
                out = {"messages": [{"id": mid} for mid in page["ids"]]}
                if idx + 1 < len(pages):
                    out["nextPageToken"] = str(idx + 1)
                return out
        return Req()

    def get(self, userId, id, format):
        full = self.full

        class Req:
            def execute(self):
                return full[id]
        return Req()

    def attachments(self):
        return _FakeAttachments(self.attachments_store)


class _FakeService:
    def __init__(self, pages, full, attachments):
        self._messages = _FakeMessages(pages, full, attachments)

    def users(self):
        return self

    def messages(self):
        return self._messages


def _make_message(mid, internal_ms, csv_bytes, filename="report.csv",
                  sender="donotreply@appliedsystems.com",
                  subject="ROBIE daily CSV delivery"):
    return {
        "id": mid,
        "internalDate": str(internal_ms),
        "payload": {
            "headers": [
                {"name": "From", "value": f"Reports <{sender}>"},
                {"name": "Subject", "value": subject},
            ],
            "parts": [{
                "filename": filename,
                "mimeType": "text/csv",
                "body": {"attachmentId": f"att-{mid}"},
            }],
        },
    }


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_dwd_fetcher_pagination_dates_dedup_atomic(tmp_path):
    day = date(2026, 9, 19)
    csv_4247 = _csv_bytes("4247", [_row4247("P1", "10/09/2026")])
    csv_4246 = _csv_bytes("4246", [_row4246("A1", "08/15/2026 - 08/15/2027", "Pie")])
    t_noon = _ms(datetime(2026, 9, 19, 12, 0))   # 8 AM ET on the day
    t_yesterday = _ms(datetime(2026, 9, 18, 12, 0))

    full = {
        "m1": _make_message("m1", t_noon, csv_4247),
        "m2": _make_message("m2", t_noon, csv_4247),      # duplicate bytes
        "m3": _make_message("m3", t_yesterday, csv_4247),  # wrong day
        "m4": _make_message("m4", t_noon, csv_4246),
        "m5": _make_message("m5", t_noon, _csv_bytes("4247", [_row4247("P2", "10/10/2026")])),
    }
    attachments = {mid: {f"att-{mid}": data}
                   for mid, data in
                   [("m1", csv_4247), ("m2", csv_4247), ("m3", csv_4247),
                    ("m4", csv_4246),
                    ("m5", _csv_bytes("4247", [_row4247("P2", "10/10/2026")]))]}
    # Two pages: pagination must reach page 2 to find m4/m5.
    pages = [{"ids": ["m1", "m2"]}, {"ids": ["m3", "m4", "m5"]}]
    service = _FakeService(pages, full, attachments)

    out_dir = tmp_path / "csvs"
    summary = dwd.fetch_worker_csvs(
        service, day=day, out_dir=str(out_dir),
        report_ids=("4247", "4246"))

    # Pagination worked: 4246 from page 2 was found.
    assert set(summary["saved"]) == {"4247", "4246"}
    saved_4247 = summary["saved"]["4247"]
    assert saved_4247["message_id"] == "m1"
    assert open(saved_4247["path"], "rb").read() == csv_4247

    skipped = "\n".join(summary["skipped"])
    assert "duplicate attachment" in skipped          # m2 deduped by hash
    assert "delivered 2026-09-18" in skipped         # m3 strict date reject
    assert "already saved 4247" in skipped           # m5 same report kept first

    # Atomic: no tmp files left behind, output is the complete CSV.
    leftovers = [p for p in os.listdir(out_dir) if p.endswith(".tmp")]
    assert leftovers == []


def test_dwd_fetcher_missing_report_raises():
    day = date(2026, 9, 19)
    csv_4247 = _csv_bytes("4247", [_row4247("P1", "10/09/2026")])
    t_noon = _ms(datetime(2026, 9, 19, 12, 0))
    full = {"m1": _make_message("m1", t_noon, csv_4247)}
    attachments = {"m1": {"att-m1": csv_4247}}
    service = _FakeService([{"ids": ["m1"]}], full, attachments)
    # NOTE: the fetcher imports gmail_report_ingestion as a top-level
    # module (sys.path trick), so its exception class is dwd.ing's —
    # not robie_job_engine.gmail_report_ingestion's.
    with pytest.raises(dwd.ing.GmailReportIngestionError,
                       match="no delivery found for 4246"):
        dwd.fetch_worker_csvs(service, day=day, out_dir="/tmp/dwd-never",
                              report_ids=("4247", "4246"))


# --- note adapter: exact-title success + API-only failure -------------------


def _fake_ezlynx_client(monkeypatch, **behaviors):
    calls = {}
    module = types.ModuleType("src.ezlynx.api_client")

    class EZLynxApiClient:
        def add_note_to_discussion(self, **kwargs):
            calls.update(kwargs)
            calls["use_playwright_fallback"] = kwargs.get("use_playwright_fallback")
            if behaviors.get("raise"):
                raise RuntimeError("Classic REST exploded")
            return dict(behaviors.get("result", {
                "status": "success",
                "note_id": "1128931520",
                "discussion_title": kwargs.get("discussion_title"),
                "method": "api",
            }))

    module.EZLynxApiClient = EZLynxApiClient
    monkeypatch.setitem(sys.modules, "src", types.ModuleType("src"))
    monkeypatch.setitem(sys.modules, "src.ezlynx", types.ModuleType("src.ezlynx"))
    monkeypatch.setitem(sys.modules, "src.ezlynx.api_client", module)
    return calls


def test_note_adapter_exact_title_success(monkeypatch):
    from robie_job_engine import worker_adapters as wa
    calls = _fake_ezlynx_client(monkeypatch)
    adapter = wa.EZLynxNoteAdapter()
    ev = adapter.file_note(
        applicant_id="220250093",
        discussion_title="ROBIE worker adapter test 2",
        body="hello")
    assert ev.channel == "note"
    assert ev.destination_id == "1128931520"
    # Exact title wins: passed through verbatim, never fuzzy-matched.
    assert calls["discussion_title"] == "ROBIE worker adapter test 2"
    assert calls["applicant_id"] == "220250093"
    # API-only: no browser fallback, ever.
    assert calls["use_playwright_fallback"] is False
    assert "ROBIE worker adapter test 2" in ev.detail


def test_note_adapter_api_failure_fails_closed(monkeypatch):
    from robie_job_engine import worker_adapters as wa
    _fake_ezlynx_client(monkeypatch, result={"status": "error", "note_id": None})
    adapter = wa.EZLynxNoteAdapter()
    with pytest.raises(wa.AdapterError, match="note POST failed"):
        adapter.file_note(applicant_id="220250093",
                          discussion_title="Some Title", body="x")


def test_note_adapter_api_exception_fails_closed(monkeypatch):
    from robie_job_engine import worker_adapters as wa
    _fake_ezlynx_client(monkeypatch, **{"raise": True})
    adapter = wa.EZLynxNoteAdapter()
    with pytest.raises(wa.AdapterError):
        adapter.file_note(applicant_id="220250093",
                          discussion_title="Some Title", body="x")


# --- Fix 4: 4246 ingests the 4360-format daily CSV ---------------------------
# The daily audit email delivers the transaction-level CSV from scheduled
# report 4360 (24 cols), not the 19-col policy-level saved report 4246.
# Regression test: the 4360 format must validate, route, and build work
# items with Effective Date as the renewal date.


def _row4360(policy, account, effective, carrier="Test Carrier"):
    return {
        "Applicant ID": "220250093",
        "Branch": "Commercial Lines",
        "Account Name": account,
        "Account Type": "Commercial",
        "Assigned Producer": "P",
        "CSR": "C",
        "Policy Number": policy,
        "Policy ID": "PID-1",
        "Policy Transaction ID": "PTX-1",
        "Transaction Type": "Renewal",
        "Transaction Date": "08/15/2026",
        "Line of Business": "Workers Comp",
        "Master Company": carrier,
        "Download Date": "09/19/2026",
        "Effective Date": effective,
        "Expiration Date": "08/15/2027",
        "Current Policy Status": "Active",
        "Policy Term": f"{effective} - 08/15/2027",
        "Policy Type": "Workers Comp",
        "Transaction Deleted": "No",
        "Service Team": "Commercial",
        "Total Written Premium": "1000",
        "Total Customers": "1",
        "Total Transactions": "1",
    }


def test_4246_ingests_4360_format_daily_csv(tmp_path):
    rows = [_row4360("WC999", "Test Co", "08/15/2026")]
    run = vw.run_worker("4246", day=DAY, mode="dry_run",
                        queue_dir=str(tmp_path),
                        csv_bytes=_csv_bytes("4246", rows))
    assert run.ingested_rows == 1
    assert run.work_items == 1
    assert not run.errors
    assert len(run.audit_added) == 1
    # Effective Date (2026-08-15) is the renewal date: 2026-09-19 is day 35.
    assert run.actions[0].status == "due_now"
    assert "day 35" in run.actions[0].reason
    # The queue entry carries carrier + department from the 4360 columns.
    queue_path = tmp_path / "audit-working-queue.json"
    saved = json.loads(queue_path.read_text(encoding="utf-8"))
    entry = saved["entries"][run.audit_added[0]]
    assert entry["renewal_date"] == "2026-08-15"
    assert entry["carrier"] == "Test Carrier"
    assert entry["account_name"] == "Test Co"
    # 4360 format has Branch, not Department — it must not be Unassigned.
    assert entry["department"] == "Commercial Lines"


def test_4246_rejects_old_19col_format(tmp_path):
    # The old saved-4246 19-col format is no longer the daily feed and
    # must be rejected, not silently ingested.
    old_headers = ["Account Name", "Applicant ID", "Policy Number"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=old_headers)
    writer.writeheader()
    writer.writerow({"Account Name": "X", "Applicant ID": "1",
                     "Policy Number": "P1"})
    with pytest.raises(Exception):
        vw.run_worker("4246", day=DAY, mode="dry_run",
                      queue_dir=str(tmp_path),
                      csv_bytes=buffer.getvalue().encode("utf-8"))


# --- Fix 5: 4372 excludes closed tasks --------------------------------------
# The 4372 daily CSV can deliver CLOSED tasks (report filter does not
# exclude them). A closed task is finished work and must never become a
# work item, regardless of due date.


def _row4372_closed(policy, account, closed_date, closed_by):
    return _row4372(policy, account, "12/20/2026",
                    **{"Task Status": "Closed",
                       "Task Closed Date": closed_date,
                       "Task Closed By": closed_by})


def test_4372_closed_tasks_excluded_not_worked(tmp_path):
    rows = [
        _row4372_closed("SAHO581361", "Saeed Abbaszadeh",
                        "2025-12-12", "Daniela Aguilar"),
        _row4372("NEW123", "Current Person", "10/15/2026",
                 **{"Task Status": "Open"}),
    ]
    run = vw.run_worker("4372", day=DAY, mode="dry_run",
                        queue_dir=str(tmp_path),
                        csv_bytes=_csv_bytes("4372", rows))
    assert run.work_items == 1
    assert len(run.excluded_stale) == 1
    ex = run.excluded_stale[0]
    assert ex["policy_number"] == "SAHO581361"
    assert "closed 2025-12-12 by Daniela Aguilar" in ex["reason"]
    assert run.actions[0].policy_number == "NEW123"


def test_4372_non_closed_status_never_excluded_by_this_rule(tmp_path):
    # Blank or Open status must not be treated as closed — missing data
    # never silently drops work.
    rows = [_row4372("OPEN1", "Open Person", "10/15/2026",
                     **{"Task Status": ""})]
    run = vw.run_worker("4372", day=DAY, mode="dry_run",
                        queue_dir=str(tmp_path),
                        csv_bytes=_csv_bytes("4372", rows))
    assert run.work_items == 1
    assert not run.excluded_stale


def test_4372_test_ho_note_fallback_extracts_policy():
    """Regression (2026-09-20): 4372 with empty Policy Number column but
    TEST-HO policy in the Note field must extract the policy number for
    the canary test. Production rows always carry the column; this
    fallback is scoped to the TEST-HO prefix only."""
    row = {
        "Policy Number": "",
        "Note": "Test Mortgagee Verification - TEST-HO-08312026-01\n\nTest task.",
    }
    assert ing.identity_value("4372", row) == "TEST-HO-08312026-01"


def test_4372_test_ho_fallback_ignores_non_test_notes():
    """The TEST-HO fallback must not fire on real policy notes."""
    row = {
        "Policy Number": "",
        "Note": "Monitoring if payment is received for policy SAHO581361",
    }
    assert ing.identity_value("4372", row) == ""


def test_4372_policy_column_takes_precedence_over_note():
    """When the Policy Number column is populated, it wins — the Note
    fallback never overrides real data."""
    row = {
        "Policy Number": "SAHO581361",
        "Note": "Test note with TEST-HO-99999999 inside",
    }
    assert ing.identity_value("4372", row) == "SAHO581361"
