#!/usr/bin/env python3
"""Self-test for verification_workers.py — no network, no Gmail.

Builds realistic synthetic CSVs per report and runs each worker dry-run.
Asserts work-item counts, identity rules, audit incremental behavior,
planner outputs, and digest shape.
"""
import csv
import io
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "robie_job_engine"))
import verification_workers as vw  # noqa: E402
import gmail_report_ingestion as ing  # noqa: E402

TODAY = date(2026, 9, 19)  # a Saturday; planners don't depend on weekday here
passes, failures = [], []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        failures.append(f"{name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{name}: unexpected {type(exc).__name__}: {exc}")
    else:
        passes.append(name)


def make_csv(report_id, rowspecs):
    """rowspecs: list of dicts overriding per-column values."""
    headers = ing.expected_headers(report_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for n, spec in enumerate(rowspecs, start=1):
        row = []
        for col in headers:
            if col in spec:
                row.append(spec[col])
            elif col == "Policy Number":
                row.append(f"POL-{report_id}-{n:03d}")
            elif col == "Account Name":
                row.append(f"Test Account {n}")
            elif col == "Department":
                row.append("Commercial Lines")
            elif col == "Master Company":
                row.append("Test Carrier")
            else:
                row.append("")
        w.writerow(row)
    return buf.getvalue().encode("utf-8")


def test_4247_counts_and_plans():
    exp_soon = (TODAY + timedelta(days=35)).strftime("%m/%d/%Y")
    exp_far = (TODAY + timedelta(days=120)).strftime("%m/%d/%Y")
    eff = (TODAY - timedelta(days=330)).strftime("%m/%d/%Y")
    data = make_csv("4247", [
        {"Policy Effective Date": eff, "Policy Expiration Date": exp_soon},
        {"Policy Effective Date": eff, "Policy Expiration Date": exp_far,
         "Master Company": "Progressive"},
        {"Policy Effective Date": eff, "Policy Expiration Date": "not-a-date"},
    ])
    run = vw.run_worker("4247", day=TODAY, mode="dry_run",
                        queue_dir=tempfile.mkdtemp(), csv_bytes=data)
    assert run.work_items == 3, f"items {run.work_items} != 3"
    kinds = [a.action.kind for a in run.actions]
    assert kinds[0] == "portal", f"first item kind {kinds[0]}"
    assert kinds[1] == "verify" and "Progressive" in run.actions[1].action.detail, \
        "Progressive BOR download check missing"
    assert run.actions[2].status == "blocked", "bad expiration should block"
    # items sorted most-urgent-first
    assert run.actions[0].policy_number == "POL-4247-001"
    digest = vw.build_digest([run])
    assert "Manual Renewals" in digest and "Commercial Lines" in digest


def test_4246_incremental_queue():
    qdir = tempfile.mkdtemp()
    term = "08/05/2026 - 08/05/2027"  # renewal 2026-08-05 -> day 45 on 2026-09-19
    data = make_csv("4246", [
        {"Policy Term": term},
        {"Policy Term": "09/10/2026 - 09/10/2027"},  # renewed 9 days ago
    ])
    run1 = vw.run_worker("4246", day=TODAY, mode="dry_run",
                         queue_dir=qdir, csv_bytes=data)
    assert run1.work_items == 2, f"items {run1.work_items} != 2"
    assert len(run1.audit_added) == 2, f"added {len(run1.audit_added)} != 2"
    assert len(run1.audit_carried) == 0
    # day-45 entry must escalate
    esc = [a for a in run1.actions if a.action.kind == "escalate"]
    assert len(esc) == 1, f"expected 1 escalation, got {len(esc)}"
    # second run same day: nothing new, both carried
    run2 = vw.run_worker("4246", day=TODAY, mode="dry_run",
                         queue_dir=qdir, csv_bytes=data)
    assert len(run2.audit_added) == 0, "second run must add nothing"
    assert len(run2.audit_carried) == 2, "second run must carry both"
    # queue file persisted
    import json
    with open(os.path.join(qdir, "audit-working-queue.json")) as fh:
        saved = json.load(fh)
    assert len(saved["entries"]) == 2


def test_4372_blocked_on_lender():
    data = make_csv("4372", [{}, {}, {}, {}])
    run = vw.run_worker("4372", day=TODAY, mode="dry_run",
                        queue_dir=tempfile.mkdtemp(), csv_bytes=data)
    assert run.work_items == 4, f"items {run.work_items} != 4"
    assert all(a.status == "blocked" for a in run.actions), \
        "mortgagee items need lender enrichment"
    assert all("lender" in a.reason.casefold() for a in run.actions)


def test_4359_per_request_and_turnaround():
    old = (TODAY - timedelta(days=10)).strftime("%m/%d/%Y")
    new = (TODAY - timedelta(days=1)).strftime("%m/%d/%Y")
    eff = TODAY.strftime("%m/%d/%Y")
    data = make_csv("4359", [
        {"Policy Number": "POL-DUP", "Change Request Created Date": old,
         "Effective Date": eff},
        {"Policy Number": "POL-DUP", "Change Request Created Date": new,
         "Effective Date": eff},
    ])
    run = vw.run_worker("4359", day=TODAY, mode="dry_run",
                        queue_dir=tempfile.mkdtemp(), csv_bytes=data,
                        allow_unverified=True)
    assert run.work_items == 2, f"4359 must work both requests, got {run.work_items}"
    statuses = sorted(a.status for a in run.actions)
    assert statuses == ["due_now", "waiting"], f"statuses {statuses}"
    keys = {a.item_key for a in run.actions}
    assert len(keys) == 2, "work-item keys must differ per request"


def test_4359_gate_blocks_without_override():
    data = make_csv("4359", [{}])
    try:
        vw.run_worker("4359", day=TODAY, mode="dry_run",
                      queue_dir=tempfile.mkdtemp(), csv_bytes=data)
    except ing.GmailReportIngestionError:
        passes.append("4359 gate blocks worker without override")
    else:
        failures.append("4359 gate blocks worker without override: no error raised")


def test_digest_shape():
    data = make_csv("4247", [{"Policy Expiration Date":
                              (TODAY + timedelta(days=40)).strftime("%m/%d/%Y"),
                              "Department": "Personal Lines"}])
    run = vw.run_worker("4247", day=TODAY, mode="dry_run",
                        queue_dir=tempfile.mkdtemp(), csv_bytes=data)
    digest = vw.build_digest([run])
    assert "### Personal Lines" in digest
    assert "done / not done / pending" not in digest  # labels are Needs action/Waiting/Blocked
    assert "[Needs action]" in digest


check("4247 counts, urgency sort, Progressive BOR check, bad-date block",
      test_4247_counts_and_plans)
check("4246 incremental queue: add-new-only, carry-forward, day-45 escalate",
      test_4246_incremental_queue)
check("4372 blocked pending lender/loan enrichment", test_4372_blocked_on_lender)
check("4359 per-request items + turnaround window", test_4359_per_request_and_turnaround)
test_4359_gate_blocks_without_override()
check("digest groups by department with plain-English labels", test_digest_shape)

print(f"WORKER SELF-TEST passes={len(passes)} failures={len(failures)}")
for name in passes:
    print(f"  PASS {name}")
for name in failures:
    print(f"  FAIL {name}")
sys.exit(1 if failures else 0)
