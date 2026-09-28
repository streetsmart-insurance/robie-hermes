"""Regression tests for robie_job_engine.readonly_worker_preview.

Pins the reliability gates Carlo ordered for the read-only verification
preview (the "what the AI would do" grading workbook):
- dead-policy gate (reports carry no status column; a live PolicyApi
  status map must drive DO NOT WORK / verify-first actions)
- strict contact-type knowledge matching (a Policy Changes fact must
  never appear on a Renewals/Audits/Mortgagee action)
- policy-number conflict detection (same number, multiple rows)
- exact urgency buckets and day counts
"""
from __future__ import annotations

import unittest
from datetime import date

from robie_job_engine.readonly_worker_preview import (
    apply_knowledge,
    build_preview_workbook,
    compose_4246_action,
    compose_4247_action,
    compose_4744_action,
    detect_policy_conflicts,
    knowledge_for,
    load_knowledge_entries,
    parse_date,
)

TODAY = date(2026, 9, 28)


def renewal_row(**kw):
    base = {
        "Account Name": "Test Account",
        "Policy Number": "POL-1",
        "Master Company": "Progressive",
        "Line Of Business": "Commercial Auto",
        "Policy Expiration Date": "2026-12-20",
        "Assigned Producer": "P",
        "CSR": "C",
    }
    base.update(kw)
    return base


def audit_row(**kw):
    base = {
        "Account Name": "Test Account",
        "Policy Number": "WC-1",
        "Master Company": "AmTrust",
        "Effective Date": "2026-08-17",
        "Current Policy Status": "Active",
    }
    base.update(kw)
    return base


def mortgagee_row(**kw):
    base = {
        "Account Name": "Test Account",
        "Policy Number": "HO-1",
        "Master Company": "Safeco",
        "Line Of Business": "Homeowners",
        "Policy Expiration Date": "2026-10-22",
    }
    base.update(kw)
    return base


KNOWLEDGE = [
    ("Progressive", "Policy Changes", "do it on foragentsonly.com, not by email", "Carlo"),
    ("Progressive", "Renewals", "renewals go through the agent portal queue", "Carlo"),
    ("AmTrust", "General", "general agency note for AmTrust", "Carlo"),
]


class UrgencyTests(unittest.TestCase):
    def test_4247_urgent_under_15_days(self):
        action, why = compose_4247_action(
            renewal_row(**{"Policy Expiration Date": "2026-10-05"}), TODAY)
        self.assertIn("URGENT", why)

    def test_4247_expired(self):
        action, why = compose_4247_action(
            renewal_row(**{"Policy Expiration Date": "2026-09-01"}), TODAY)
        self.assertIn("EXPIRED 27 days ago", why)

    def test_4247_early_over_45_days(self):
        action, why = compose_4247_action(
            renewal_row(**{"Policy Expiration Date": "2026-12-20"}), TODAY)
        self.assertIn("Early", why)
        self.assertIn("83 days to expiration", why)

    def test_4246_in_audit_window(self):
        action, why = compose_4246_action(
            audit_row(**{"Effective Date": "2026-08-17"}), TODAY)
        self.assertIn("In audit window", why)
        self.assertIn("42 days since renewal", why)

    def test_4246_overdue(self):
        action, why = compose_4246_action(
            audit_row(**{"Effective Date": "2026-06-01"}), TODAY)
        self.assertIn("OVERDUE", why)

    def test_4744_urgent_under_30_days(self):
        action, why = compose_4744_action(
            mortgagee_row(**{"Policy Expiration Date": "2026-10-22"}), TODAY)
        self.assertIn("URGENT", why)
        self.assertIn("24 days to expiration", why)

    def test_4744_in_worker_window(self):
        action, why = compose_4744_action(
            mortgagee_row(**{"Policy Expiration Date": "2026-11-07"}), TODAY)
        self.assertIn("In worker window", why)


class DeadPolicyGateTests(unittest.TestCase):
    STATUS = {
        "TPM4520882-01 GL": {"status": "Inactive", "cancellation_date": "2026-04-01"},
        "HO-9": {"status": "Inactive", "cancellation_date": "2026-03-10"},
        "WC-9": {"status": "Inactive", "cancellation_date": "2026-05-02"},
        "POL-PENDING": {"status": "Pending", "cancellation_date": ""},
        "POL-ACTIVE": {"status": "Active", "cancellation_date": ""},
    }

    def test_4247_inactive_gets_do_not_work_not_renewal_steps(self):
        action, why = compose_4247_action(
            renewal_row(**{"Policy Number": "TPM4520882-01 GL",
                           "Master Company": "Cover Whale"}),
            TODAY, status_map=self.STATUS)
        self.assertIn("DO NOT WORK", action)
        self.assertIn("2026-04-01", action)
        self.assertNotIn("Pull renewal docs", action)

    def test_4744_inactive_gets_do_not_work(self):
        action, why = compose_4744_action(
            mortgagee_row(**{"Policy Number": "HO-9"}), TODAY, status_map=self.STATUS)
        self.assertIn("DO NOT WORK", action)
        self.assertIn("2026-03-10", action)

    def test_4246_inactive_gets_final_audit_flag_not_suppression(self):
        action, why = compose_4246_action(
            audit_row(**{"Policy Number": "WC-9"}), TODAY, status_map=self.STATUS)
        self.assertIn("final audit", action)
        self.assertNotIn("DO NOT WORK", action)

    def test_unknown_status_verifies_first(self):
        action, why = compose_4247_action(
            renewal_row(**{"Policy Number": "POL-PENDING"}), TODAY,
            status_map=self.STATUS)
        self.assertIn("Verify policy status", action)
        self.assertNotIn("DO NOT WORK", action)

    def test_active_policy_works_normally(self):
        action, why = compose_4247_action(
            renewal_row(**{"Policy Number": "POL-ACTIVE"}), TODAY,
            status_map=self.STATUS)
        self.assertNotIn("DO NOT WORK", action)
        self.assertNotIn("Verify policy status", action)

    def test_no_status_map_means_gate_stays_off(self):
        action, why = compose_4247_action(
            renewal_row(**{"Policy Number": "TPM4520882-01 GL",
                           "Master Company": "Cover Whale"}),
            TODAY, status_map=None)
        self.assertNotIn("DO NOT WORK", action)


class StrictKnowledgeTests(unittest.TestCase):
    def test_policy_changes_fact_absent_from_renewals_action(self):
        action, _ = compose_4247_action(renewal_row(), TODAY, knowledge=KNOWLEDGE)
        self.assertNotIn("foragentsonly.com", action)
        self.assertNotIn("Policy Changes", action)

    def test_exact_contact_type_match_applies(self):
        action, _ = compose_4247_action(renewal_row(), TODAY, knowledge=KNOWLEDGE)
        self.assertIn("agent portal queue", action)

    def test_general_entry_applies_across_categories(self):
        steps = []
        apply_knowledge(steps, KNOWLEDGE, "AmTrust", "Audits")
        self.assertTrue(any("general agency note" in s for s in steps))

    def test_unrelated_carrier_gets_nothing(self):
        steps = []
        apply_knowledge(steps, KNOWLEDGE, "Hartford", "Renewals")
        self.assertEqual(steps, [])

    def test_knowledge_for_requires_exact_contact_or_general(self):
        hits = knowledge_for(KNOWLEDGE, "Progressive", "Renewals")
        contacts = [c for c, _, _ in hits]
        self.assertIn("Renewals", contacts)
        self.assertNotIn("Policy Changes", contacts)

    def test_knowledge_file_parser(self):
        import tempfile, os
        text = ("# Carrier contact-type knowledge\n\n"
                "- **Progressive** \u2014 Policy Changes \u2014 do it on foragentsonly.com, "
                "not by email. (Carlo, 2026-09-27)\n"
                "Some other line\n")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            entries = load_knowledge_entries(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(entries), 1)
        carrier, contact, instr, src = entries[0]
        self.assertEqual(carrier, "Progressive")
        self.assertEqual(contact, "Policy Changes")
        self.assertIn("foragentsonly.com", instr)
        self.assertIn("Carlo", src)


class ProgressiveBorRuleTests(unittest.TestCase):
    def test_progressive_manual_row_leads_with_bor_check(self):
        action, _ = compose_4247_action(renewal_row(), TODAY)
        self.assertTrue(action.startswith("BOR takeover"),
                        f"expected BOR-takeover first step, got: {action[:80]}")

    def test_non_progressive_row_has_no_bor_step(self):
        action, _ = compose_4247_action(
            renewal_row(**{"Master Company": "Hartford"}), TODAY)
        self.assertNotIn("BOR takeover", action)


class ConflictDetectionTests(unittest.TestCase):
    ROWS = [
        {"Policy Number": "WC5-33S-375240-025", "Account Name": "Rengifo Drywall, LLC"},
        {"Policy Number": "WC5-33S-375240-025", "Account Name": "Reliable Drywall & Painting LLC"},
        {"Policy Number": "UNIQUE-1", "Account Name": "Solo Account"},
    ]

    def test_duplicate_policy_number_detected(self):
        conflicts = detect_policy_conflicts(self.ROWS)
        self.assertIn("WC5-33S-375240-025", conflicts)
        self.assertNotIn("UNIQUE-1", conflicts)
        self.assertEqual(conflicts["WC5-33S-375240-025"],
                         ["Reliable Drywall & Painting LLC", "Rengifo Drywall, LLC"])

    def test_workbook_flags_conflict_rows(self):
        rows = [dict(r, **{"Master Company": "NJCRIB",
                           "Line Of Business": "Workers comp",
                           "Policy Expiration Date": "2026-11-17"})
                for r in self.ROWS]
        wb = build_preview_workbook(
            [{"sheet": "4247 Manual Renewal",
              "columns": ["Account Name", "Policy Number", "Master Company"],
              "rows": rows,
              "composer": compose_4247_action}],
            TODAY)
        ws = wb["4247 Manual Renewal"]
        actions = [ws.cell(row=r, column=4).value
                   for r in range(6, 6 + len(rows))]
        self.assertIn("DATA CONFLICT", actions[0])
        self.assertIn("DATA CONFLICT", actions[1])
        self.assertNotIn("DATA CONFLICT", actions[2])

    def test_every_row_has_action_and_why(self):
        rows = [renewal_row(**{"Policy Number": f"P-{i}"}) for i in range(5)]
        wb = build_preview_workbook(
            [{"sheet": "S", "columns": ["Policy Number"], "rows": rows,
              "composer": compose_4247_action}],
            TODAY)
        ws = wb["S"]
        for r in range(6, 11):
            self.assertTrue(ws.cell(row=r, column=2).value)
            self.assertTrue(ws.cell(row=r, column=3).value)


class ParseDateTests(unittest.TestCase):
    def test_iso_and_blank(self):
        self.assertEqual(parse_date("2026-08-17"), date(2026, 8, 17))
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date("not-a-date"))


if __name__ == "__main__":
    unittest.main()
