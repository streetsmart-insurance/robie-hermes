"""Regression tests for Gmail-backed verification workers.

Unittest (not pytest-only) so the ROBIE verification-gate battery
(`unittest discover -s tests`) actually collects these cases.

No network, no Gmail. Covers the 2026-09-19 4246/4360 and 4372 closed-task
fixes plus the original worker self-test contract.
"""
from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from robie_job_engine import gmail_report_ingestion as ing
from robie_job_engine import verification_workers as vw

DAY = date(2026, 9, 19)
REPO_ROOT = Path(__file__).resolve().parents[1]


def _csv_bytes(report_id: str, rows: list[dict]) -> bytes:
    headers = ing.expected_headers(report_id)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: row.get(header, "") for header in headers})
    return buffer.getvalue().encode("utf-8")


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
    return _row4372(
        policy,
        account,
        "12/20/2026",
        **{"Task Status": "Closed",
           "Task Closed Date": closed_date,
           "Task Closed By": closed_by},
    )


class RootTestShadowingTests(unittest.TestCase):
    """CI 2026-09-19: repo-root test_*.py shadowed tests/ and called sys.exit."""

    def test_no_repo_root_test_modules_shadow_tests_package(self):
        collisions = sorted(path.name for path in REPO_ROOT.glob("test_*.py"))
        self.assertEqual(
            collisions,
            [],
            "repo-root test_*.py shadows `unittest discover -s tests` because "
            f"PYTHONPATH includes the repo root: {collisions}",
        )


class Audit4246WorkerTests(unittest.TestCase):
    def test_4246_ingests_4360_format_daily_csv(self):
        rows = [_row4360("WC999", "Test Co", "08/15/2026")]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker(
                "4246",
                day=DAY,
                mode="dry_run",
                queue_dir=tmp,
                csv_bytes=_csv_bytes("4246", rows),
            )
            self.assertEqual(run.ingested_rows, 1)
            self.assertEqual(run.work_items, 1)
            self.assertFalse(run.errors)
            self.assertEqual(len(run.audit_added), 1)
            self.assertEqual(run.actions[0].status, "due_now")
            self.assertIn("day 35", run.actions[0].reason)
            saved = json.loads(
                Path(tmp, "audit-working-queue.json").read_text(encoding="utf-8")
            )
            entry = saved["entries"][run.audit_added[0]]
            self.assertEqual(entry["renewal_date"], "2026-08-15")
            self.assertEqual(entry["carrier"], "Test Carrier")
            self.assertEqual(entry["account_name"], "Test Co")
            self.assertEqual(entry["department"], "Commercial Lines")

    def test_4246_rejects_old_19col_format(self):
        old_headers = ["Account Name", "Applicant ID", "Policy Number"]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=old_headers)
        writer.writeheader()
        writer.writerow(
            {"Account Name": "X", "Applicant ID": "1", "Policy Number": "P1"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception):
                vw.run_worker(
                    "4246",
                    day=DAY,
                    mode="dry_run",
                    queue_dir=tmp,
                    csv_bytes=buffer.getvalue().encode("utf-8"),
                )

    def test_4246_incremental_queue_add_then_carry(self):
        rows = [
            _row4360("WC-DAY45", "Day45 Co", "08/05/2026"),
            _row4360("WC-DAY9", "Day9 Co", "09/10/2026"),
        ]
        data = _csv_bytes("4246", rows)
        with tempfile.TemporaryDirectory() as tmp:
            run1 = vw.run_worker(
                "4246", day=DAY, mode="dry_run", queue_dir=tmp, csv_bytes=data
            )
            self.assertEqual(run1.work_items, 2)
            self.assertEqual(len(run1.audit_added), 2)
            self.assertEqual(len(run1.audit_carried), 0)
            escalations = [a for a in run1.actions if a.action.kind == "escalate"]
            self.assertEqual(len(escalations), 1, escalations)
            self.assertEqual(escalations[0].policy_number, "WC-DAY45")

            run2 = vw.run_worker(
                "4246", day=DAY, mode="dry_run", queue_dir=tmp, csv_bytes=data
            )
            self.assertEqual(len(run2.audit_added), 0)
            self.assertEqual(len(run2.audit_carried), 2)
            saved = json.loads(
                Path(tmp, "audit-working-queue.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(saved["entries"]), 2)


class Mortgagee4372WorkerTests(unittest.TestCase):
    def test_4372_closed_tasks_excluded_not_worked(self):
        rows = [
            _row4372_closed(
                "SAHO581361", "Saeed Abbaszadeh", "2025-12-12", "Daniela Aguilar"
            ),
            _row4372("NEW123", "Current Person", "10/15/2026", **{"Task Status": "Open"}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker(
                "4372",
                day=DAY,
                mode="dry_run",
                queue_dir=tmp,
                csv_bytes=_csv_bytes("4372", rows),
            )
            self.assertEqual(run.work_items, 1)
            self.assertEqual(len(run.excluded_stale), 1)
            ex = run.excluded_stale[0]
            self.assertEqual(ex["policy_number"], "SAHO581361")
            self.assertIn("closed 2025-12-12 by Daniela Aguilar", ex["reason"])
            self.assertEqual(run.actions[0].policy_number, "NEW123")

    def test_4372_non_closed_status_never_excluded_by_this_rule(self):
        rows = [_row4372("OPEN1", "Open Person", "10/15/2026", **{"Task Status": ""})]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker(
                "4372",
                day=DAY,
                mode="dry_run",
                queue_dir=tmp,
                csv_bytes=_csv_bytes("4372", rows),
            )
            self.assertEqual(run.work_items, 1)
            self.assertFalse(run.excluded_stale)

    def test_4372_open_tasks_blocked_pending_lender_enrichment(self):
        rows = [_row4372(f"OPEN{n}", f"Account {n}", "10/15/2026") for n in range(1, 5)]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker(
                "4372",
                day=DAY,
                mode="dry_run",
                queue_dir=tmp,
                csv_bytes=_csv_bytes("4372", rows),
            )
            self.assertEqual(run.work_items, 4)
            self.assertTrue(all(a.status == "blocked" for a in run.actions))
            self.assertTrue(all("lender" in a.reason.casefold() for a in run.actions))


class ManualRenewal4247WorkerTests(unittest.TestCase):
    def test_4247_counts_urgency_sort_progressive_and_bad_date(self):
        exp_soon = (DAY + timedelta(days=35)).strftime("%m/%d/%Y")
        exp_far = (DAY + timedelta(days=120)).strftime("%m/%d/%Y")
        eff = (DAY - timedelta(days=330)).strftime("%m/%d/%Y")
        rows = [
            {
                "Policy Number": "POL-4247-001",
                "Account Name": "Soon Account",
                "Department": "Commercial Lines",
                "Policy Effective Date": eff,
                "Policy Expiration Date": exp_soon,
                "Master Company": "Travelers",
            },
            {
                "Policy Number": "POL-4247-002",
                "Account Name": "Progressive Account",
                "Department": "Commercial Lines",
                "Policy Effective Date": eff,
                "Policy Expiration Date": exp_far,
                "Master Company": "Progressive",
            },
            {
                "Policy Number": "POL-4247-003",
                "Account Name": "Bad Date Account",
                "Department": "Commercial Lines",
                "Policy Effective Date": eff,
                "Policy Expiration Date": "not-a-date",
                "Master Company": "Test Carrier",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker(
                "4247",
                day=DAY,
                mode="dry_run",
                queue_dir=tmp,
                csv_bytes=_csv_bytes("4247", rows),
            )
            self.assertEqual(run.work_items, 3)
            kinds = [a.action.kind for a in run.actions]
            self.assertEqual(kinds[0], "portal")
            self.assertEqual(kinds[1], "verify")
            self.assertIn("Progressive", run.actions[1].action.detail)
            self.assertEqual(run.actions[2].status, "blocked")
            self.assertEqual(run.actions[0].policy_number, "POL-4247-001")
            digest = vw.build_digest([run])
            self.assertIn("Manual Renewals", digest)
            self.assertIn("Commercial Lines", digest)


class PolicyChange4359WorkerTests(unittest.TestCase):
    def test_4359_per_request_items_and_turnaround_window(self):
        old = (DAY - timedelta(days=10)).strftime("%m/%d/%Y")
        new = (DAY - timedelta(days=1)).strftime("%m/%d/%Y")
        eff = DAY.strftime("%m/%d/%Y")
        rows = [
            {
                "Policy Number": "POL-DUP",
                "Change Request Created Date": old,
                "Effective Date": eff,
                "Department": "Commercial Lines",
                "Master Company": "Test Carrier",
            },
            {
                "Policy Number": "POL-DUP",
                "Change Request Created Date": new,
                "Effective Date": eff,
                "Department": "Commercial Lines",
                "Master Company": "Test Carrier",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker(
                "4359",
                day=DAY,
                mode="dry_run",
                queue_dir=tmp,
                csv_bytes=_csv_bytes("4359", rows),
            )
            self.assertEqual(run.work_items, 2)
            self.assertEqual(sorted(a.status for a in run.actions), ["due_now", "waiting"])
            self.assertEqual(len({a.item_key for a in run.actions}), 2)

    def test_4359_gate_open_after_verification(self):
        rows = [{
            "Policy Number": "TEST-001",
            "Change Request Created Date": "09/19/2026",
            "Department": "Commercial Lines",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker(
                "4359",
                day=DAY,
                mode="dry_run",
                queue_dir=tmp,
                csv_bytes=_csv_bytes("4359", rows),
            )
            self.assertEqual(run.work_items, 1)
            self.assertFalse(run.errors)


class DigestShapeTests(unittest.TestCase):
    def test_digest_groups_by_department_with_plain_english_labels(self):
        rows = [{
            "Policy Number": "POL-PL",
            "Account Name": "Personal Account",
            "Department": "Personal Lines",
            "Policy Expiration Date": (DAY + timedelta(days=40)).strftime("%m/%d/%Y"),
            "Master Company": "Test Carrier",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker(
                "4247",
                day=DAY,
                mode="dry_run",
                queue_dir=tmp,
                csv_bytes=_csv_bytes("4247", rows),
            )
            digest = vw.build_digest([run])
            self.assertIn("### Personal Lines", digest)
            self.assertNotIn("done / not done / pending", digest)
            self.assertIn("[Needs action]", digest)


if __name__ == "__main__":
    unittest.main()


class PlanAPlusRegressionTests(unittest.TestCase):
    """2026-09-26: plans must use carrier_channel_routing (never hardcode
    portal), name the policy number, and use the phone directory."""

    def test_4247_njcrib_routes_to_email_not_portal(self):
        item = vw.WorkItem(
            key="k", report_id="4247", policy_number="6S60UB-A442372-6-26",
            account_name="EZ Slide Garage Doors", department="Commercial Lines",
            carrier="NJCRIB - Hartford Assigned Risk",
            producer="", csr="",
            expiration_date=(DAY + timedelta(days=45)).strftime("%m/%d/%Y"),
        )
        action, status, reason = vw.plan_4247(item, DAY)
        self.assertEqual(action.kind, "email")
        self.assertIn("assignedrisk@hartford.com", action.detail)
        self.assertIn("6S60UB-A442372-6-26", action.detail)
        self.assertIn("EZ Slide Garage Doors", action.detail)
        self.assertIn("portal is available", action.detail)

    def test_4247_portal_carrier_names_policy_and_url(self):
        item = vw.WorkItem(
            key="k", report_id="4247", policy_number="TRAV-123",
            account_name="Acme Corp", department="Commercial Lines",
            carrier="Travelers", producer="", csr="",
            expiration_date=(DAY + timedelta(days=35)).strftime("%m/%d/%Y"),
        )
        action, status, reason = vw.plan_4247(item, DAY)
        self.assertEqual(action.kind, "portal")
        self.assertIn("TRAV-123", action.detail)
        self.assertIn("Acme Corp", action.detail)
        self.assertIn("travelers.com", action.detail)

    def test_4246_pie_uses_phone_directory_and_policy_number(self):
        entry = vw.AuditQueueEntry(
            key="k", policy_number="WC PI 2905651-001",
            account_name="V & I Pro Group LLC", department="Commercial Lines",
            carrier="Pie Insurance",
            renewal_date=(DAY - timedelta(days=35)).strftime("%Y-%m-%d"),
            first_seen=DAY.isoformat(),
        )
        action, status, reason = vw.plan_4246(entry, DAY)
        self.assertEqual(action.kind, "call")
        self.assertIn("855-965-1840", action.detail)
        self.assertIn("WC PI 2905651-001", action.detail)
        self.assertIn("V & I Pro Group LLC", action.detail)

    def test_4246_njcrib_routes_to_email_not_portal(self):
        entry = vw.AuditQueueEntry(
            key="k", policy_number="WC5-33S-381868-015",
            account_name="Central Jersey Tree Services LLC",
            department="Commercial Lines",
            carrier="NJCRIB - Liberty Assigned Risk",
            renewal_date=(DAY - timedelta(days=33)).strftime("%Y-%m-%d"),
            first_seen=DAY.isoformat(),
        )
        action, status, reason = vw.plan_4246(entry, DAY)
        self.assertEqual(action.kind, "email")
        self.assertIn("assignedrisk@libertymutual.com", action.detail)
        self.assertIn("WC5-33S-381868-015", action.detail)
