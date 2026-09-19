"""Regression tests for Gmail-backed verification workers.

Ported from Ralph's 2026-09-19 4246/4372 fixes. No network, no Gmail.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date

import pytest

from robie_job_engine import gmail_report_ingestion as ing
from robie_job_engine import verification_workers as vw

DAY = date(2026, 9, 19)


def _csv_bytes(report_id: str, rows: list[dict]) -> bytes:
    headers = ing.expected_headers(report_id)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: row.get(header, "") for header in headers})
    return buffer.getvalue().encode("utf-8")


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


def _row4372(policy, account, due, **overrides):
    row = {
        "Applicant ID": "220250093",
        "Account Name": account,
        "Policy Number": policy,
        "Task Due Date": due,
        "Task Status": "Open",
        "Department": "Personal Lines",
    }
    row.update(overrides)
    return row


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
