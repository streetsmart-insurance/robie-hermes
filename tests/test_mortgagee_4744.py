"""Tests for the corrected mortgagee worker: report 4744 (policy-expiration).

Covers the 2026-09-27 redesign — 8-column Homeowners/Flood expiration
report, 30-45 day worker narrowing, and the read-only lender/mortgagee
enrichment. No network, no Gmail, no browser.
"""
from __future__ import annotations

import csv
import io
import tempfile
import unittest
from datetime import date, timedelta

from robie_job_engine import carrier_channel_routing as routing
from robie_job_engine import gmail_report_ingestion as ing
from robie_job_engine import mortgagee_enrichment as menc
from robie_job_engine import verification_workers as vw

DAY = date(2026, 9, 27)


def _row4744(policy, account, expiration_iso, **overrides):
    # Real export headers (verified 2026-09-27 against the 05:00 EDT
    # scheduled delivery): base field names, no "Policy Data" prefix.
    row = {
        "Account Name": account,
        "Policy Number": policy,
        "Master Company": "Test Carrier",
        "Line Of Business": "Homeowners",
        "Premium - Annualized": "1200",
        "Assigned Producer": "Jane Producer",
        "CSR": "CSR Name",
        "Policy Expiration Date": expiration_iso,
    }
    row.update(overrides)
    return row


def _csv4744(rows):
    headers = ing.expected_headers("4744")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in headers})
    return buffer.getvalue().encode("utf-8")


class Ingestion4744Tests(unittest.TestCase):
    def test_4744_headers_validate_and_fingerprint(self):
        content = _csv4744([_row4744("HOP1", "Acct 1", "2026-11-01")])
        rows, skipped = ing.parse_and_validate_csv("4744", content)
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped, 0)
        self.assertEqual(ing.identity_value("4744", rows[0]), "HOP1")
        header_line = content.decode("utf-8-sig").splitlines()[0]
        headers = next(csv.reader(io.StringIO(header_line)))
        self.assertEqual(ing.fingerprint_report_id(headers), "4744")

    def test_4744_schema_verified_gate_open(self):
        ing.check_report_gate("4744")

    def test_4744_work_items_map_prefixed_columns(self):
        content = _csv4744([_row4744("HOP1", "Acct 1", "2026-11-01",
                                      **{"Line Of Business": "Flood"})])
        rows, _ = ing.parse_and_validate_csv("4744", content)
        ingested = ing.IngestedReport(
            report_id="4744", display_name="x", received_at="x",
            message_id_sha256="", filename_sha256="", attachment_sha256="",
            row_count=1, skipped_rows=0, rows=rows)
        items = vw.build_work_items("4744", ingested)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.policy_number, "HOP1")
        self.assertEqual(item.account_name, "Acct 1")
        self.assertEqual(item.carrier, "Test Carrier")
        self.assertEqual(item.producer, "Jane Producer")
        self.assertEqual(item.csr, "CSR Name")
        self.assertEqual(item.expiration_date, "2026-11-01")
        self.assertEqual(item.row["Line Of Business"], "Flood")


class Window4744Tests(unittest.TestCase):
    def _run(self, days_list):
        rows = [_row4744(f"POL{i}", f"Acct {i}",
                         (DAY + timedelta(days=d)).isoformat())
                for i, d in enumerate(days_list)]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker("4744", day=DAY, mode="dry_run",
                                queue_dir=tmp, csv_bytes=_csv4744(rows))
        return run

    def test_window_narrows_to_30_45_days(self):
        run = self._run([20, 29, 30, 35, 45, 46, 50])
        kept = sorted(a.policy_number for a in run.actions)
        self.assertEqual(kept, ["POL2", "POL3", "POL4"])  # 30, 35, 45
        excluded = {e["policy_number"]: e["reason"] for e in run.excluded_stale}
        self.assertEqual(set(excluded), {"POL0", "POL1", "POL5", "POL6"})
        self.assertIn("inside 30d", excluded["POL0"])
        self.assertIn("beyond 45d", excluded["POL6"])
        self.assertEqual(run.work_items, 3)

    def test_window_boundaries_inclusive(self):
        self.assertIsNone(vw._4744_window_reason(
            vw.WorkItem(key="k", report_id="4744", policy_number="p",
                        account_name="a", department="d", carrier="c",
                        producer="p", csr="c",
                        expiration_date=(DAY + timedelta(days=30)).isoformat()),
            DAY))
        self.assertIsNone(vw._4744_window_reason(
            vw.WorkItem(key="k", report_id="4744", policy_number="p",
                        account_name="a", department="d", carrier="c",
                        producer="p", csr="c",
                        expiration_date=(DAY + timedelta(days=45)).isoformat()),
            DAY))

    def test_unparseable_expiration_never_silently_dropped(self):
        rows = [_row4744("BADDATE", "Acct", "not-a-date")]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker("4744", day=DAY, mode="dry_run",
                                queue_dir=tmp, csv_bytes=_csv4744(rows))
        self.assertEqual(run.work_items, 1)
        self.assertFalse(run.excluded_stale)


class DryRun4744Tests(unittest.TestCase):
    def test_dry_run_actions_blocked_pending_lender(self):
        rows = [_row4744(f"HOP{n}", f"Account {n}",
                         (DAY + timedelta(days=35)).isoformat())
                for n in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            run = vw.run_worker("4744", day=DAY, mode="dry_run",
                                queue_dir=tmp, csv_bytes=_csv4744(rows))
        self.assertEqual(run.work_items, 3)
        self.assertTrue(all(a.status == "blocked" for a in run.actions))
        self.assertTrue(all("lender" in a.reason.casefold()
                            for a in run.actions))
        # Expiration context (not task due date) must be in the plan.
        self.assertTrue(all("expires" in a.action.detail
                            for a in run.actions))


class EnrichmentTests(unittest.TestCase):
    def test_dry_run_without_adapters_is_hold(self):
        result = menc.enrich_work_item(policy_number="HOP1", dry_run=True)
        self.assertEqual(result.status, menc.STATUS_HOLD)
        self.assertIn("HOP1", result.reason)

    def test_blank_policy_number_is_hold(self):
        result = menc.enrich_work_item(policy_number="  ", dry_run=True)
        self.assertEqual(result.status, menc.STATUS_HOLD)

    def test_injected_adapters_resolve_ready(self):
        ports = menc.EnrichmentPorts(
            policy_search_fn=lambda pn: {"applicant_id": "A123"},
            additional_interests_fn=lambda aid: [
                {"lender_name": "First Bank", "loan_number": "LN-9"}],
        )
        result = menc.enrich_work_item(policy_number="HOP1", ports=ports,
                                       dry_run=False)
        self.assertEqual(result.status, menc.STATUS_READY)
        self.assertEqual(result.applicant_id, "A123")
        self.assertEqual(result.mortgages[0].lender_name, "First Bank")
        self.assertEqual(result.mortgages[0].loan_number, "LN-9")

    def test_conflicting_lenders_are_conflict(self):
        ports = menc.EnrichmentPorts(
            additional_interests_fn=lambda aid: [
                {"lender_name": "First Bank", "loan_number": "1"},
                {"lender_name": "Second Bank", "loan_number": "2"}],
        )
        result = menc.enrich_work_item(policy_number="HOP1",
                                       applicant_id="A123", ports=ports,
                                       dry_run=False)
        self.assertEqual(result.status, menc.STATUS_CONFLICT)
        self.assertIn("human review", result.reason)

    def test_no_mortgagee_is_hold_not_silent(self):
        ports = menc.EnrichmentPorts(
            additional_interests_fn=lambda aid: [],
        )
        result = menc.enrich_work_item(policy_number="HOP1",
                                       applicant_id="A123", ports=ports,
                                       dry_run=False)
        self.assertEqual(result.status, menc.STATUS_HOLD)
        self.assertIn("nothing to verify", result.reason)

    def test_adapter_failure_is_hold(self):
        def boom(aid):
            raise RuntimeError("ezlynx down")
        ports = menc.EnrichmentPorts(additional_interests_fn=boom)
        result = menc.enrich_work_item(policy_number="HOP1",
                                       applicant_id="A123", ports=ports,
                                       dry_run=False)
        self.assertEqual(result.status, menc.STATUS_HOLD)

    def test_plan_hold_is_blocked_with_lender_reason(self):
        result = menc.enrich_work_item(policy_number="HOP1", dry_run=True)
        kind, detail, target, status, reason = menc.plan_from_enrichment(
            result, due_txt="expires 2026-11-01 (35 days out)")
        self.assertEqual(status, "blocked")
        self.assertIn("lender", reason.casefold())
        self.assertIn("expires 2026-11-01", detail)

    def test_plan_ready_passes_producer_gate_to_due_now(self):
        ports = menc.EnrichmentPorts(
            additional_interests_fn=lambda aid: [
                {"lender_name": "First Bank", "loan_number": "LN-9"}],
        )
        result = menc.enrich_work_item(policy_number="HOP1",
                                       applicant_id="A123", ports=ports,
                                       dry_run=False)
        checks = menc.check_ready_mortgages(result.mortgages)
        kind, detail, target, status, reason = menc.plan_from_enrichment(
            result, due_txt="expires 2026-11-01 (35 days out)",
            producer_state={"Assigned Producer": "Jane Producer"},
            lender_checks=checks)
        self.assertEqual(status, "due_now")
        self.assertEqual(kind, "email")  # no portal on file -> email ladder
        self.assertIn("First Bank", detail)

    def test_producer_gate_blocks_without_producer(self):
        ports = menc.EnrichmentPorts(
            additional_interests_fn=lambda aid: [
                {"lender_name": "First Bank", "loan_number": "LN-9"}],
        )
        result = menc.enrich_work_item(policy_number="HOP1",
                                       applicant_id="A123", ports=ports,
                                       dry_run=False)
        checks = menc.check_ready_mortgages(result.mortgages)
        kind, detail, target, status, reason = menc.plan_from_enrichment(
            result, producer_state={"Assigned Producer": ""},
            lender_checks=checks)
        self.assertEqual(status, "blocked")
        self.assertIn("producer", reason.casefold())

    def test_check_prefers_portal_when_known(self):
        ports = menc.EnrichmentPorts(
            additional_interests_fn=lambda aid: [
                {"lender_name": "First Bank", "loan_number": "LN-9"}],
        )
        result = menc.enrich_work_item(policy_number="HOP1",
                                       applicant_id="A123", ports=ports,
                                       dry_run=False)
        checks = menc.check_ready_mortgages(
            result.mortgages,
            portal_lookup={"First Bank": "https://lender.example/agent"})
        self.assertEqual(checks[0].channel, "portal")
        self.assertEqual(checks[0].target, "https://lender.example/agent")

    def test_property_zip_from_row(self):
        self.assertEqual(
            menc.property_zip_from_row({"Property ZIP": " 07030 "}), "07030")
        self.assertEqual(menc.property_zip_from_row({}), "")


class CarrierRoutingTests(unittest.TestCase):
    def test_known_carrier_routes_portal(self):
        route = routing.route_carrier("Coterie")
        self.assertEqual(route["channel"], "PORTAL")
        self.assertIn("coterie", route["portal_url"].casefold())

    def test_unknown_carrier_fails_closed_to_hold(self):
        route = routing.route_carrier("Nonexistent Mutual of Nowhere")
        self.assertEqual(route["channel"], "HOLD")
        self.assertIn("reason", route)

    def test_token_match_finds_njcrib(self):
        route = routing.route_carrier("NJCRIB")
        self.assertEqual(route["channel"], "EMAIL_ASK_PORTAL")

    def test_case_insensitive(self):
        self.assertEqual(routing.route_carrier("coterie")["channel"], "PORTAL")


class LivePortsTests(unittest.TestCase):
    """mortgagee_live_ports: the live adapter factory.

    Uses a fake OAuth client (no network, no credentials). The real
    client's shape was verified 2026-09-27 against Production
    (search?PolicyNumber= -> rows with accountId/policyId).
    """

    class FakeOAuth:
        def __init__(self, rows=None, error=None):
            self._rows = rows or []
            self._error = error
            self.seen = []

        def search_policy_by_number(self, number):
            self.seen.append(number)
            if self._error is not None:
                raise self._error
            return {"status": "success",
                    "data": {"results": self._rows}}

    def test_policy_search_maps_account_id(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        fake = self.FakeOAuth(rows=[{"accountId": 22769703,
                                     "policyId": 54248667,
                                     "policyNumber": "0296536326"}])
        out = mlp.policy_search_via_oauth("0296536326", client=fake)
        self.assertEqual(out, {"applicant_id": "22769703",
                               "policy_id": "54248667"})
        self.assertEqual(fake.seen, ["0296536326"])

    def test_policy_search_no_rows_returns_none(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        fake = self.FakeOAuth(rows=[])
        self.assertIsNone(mlp.policy_search_via_oauth("NOPE", client=fake))

    def test_policy_search_blank_number_returns_none(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        fake = self.FakeOAuth(rows=[{"accountId": 1}])
        self.assertIsNone(mlp.policy_search_via_oauth("  ", client=fake))
        self.assertEqual(fake.seen, [])

    def test_policy_search_error_propagates_for_enrichment_hold(self):
        # The adapter does not swallow transport errors; enrich_work_item
        # converts an adapter exception into HOLD (fail-closed).
        from robie_job_engine import mortgagee_live_ports as mlp
        fake = self.FakeOAuth(error=RuntimeError("transport failed"))
        with self.assertRaises(RuntimeError):
            mlp.policy_search_via_oauth("0296536326", client=fake)

    def test_build_live_ports_wiring(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        fake = self.FakeOAuth(rows=[{"accountId": 9, "policyId": 8}])
        browser_reader = lambda aid: [{"lender_name": "Bank",
                                       "loan_number": "L1"}]
        ports = mlp.build_live_ports(policy_api_client=fake,
                                     browser_interests_reader=browser_reader)
        self.assertIsNotNone(ports.policy_search_fn)
        # No API exposes Additional Interests (proven 2026-09-27) —
        # the port stays None so enrichment HOLDs instead of guessing.
        self.assertIsNone(ports.additional_interests_fn)
        self.assertIs(ports.browser_interests_fn, browser_reader)

    def test_live_ports_end_to_end_hold_without_browser(self):
        # Live policy search resolves the applicant, but with no
        # additional-interests source the item HOLDs with a clear reason.
        from robie_job_engine import mortgagee_live_ports as mlp
        fake = self.FakeOAuth(rows=[{"accountId": 22769703,
                                     "policyId": 54248667}])
        ports = mlp.build_live_ports(policy_api_client=fake)
        result = menc.enrich_work_item(policy_number="0296536326",
                                       ports=ports, dry_run=False)
        self.assertEqual(result.status, menc.STATUS_HOLD)
        self.assertEqual(result.applicant_id, "22769703")
        # HOLD names the gap: applicant resolved, but no interests source.
        self.assertIn("no source", result.reason)

    def test_live_ports_browser_reader_resolves_ready(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        fake = self.FakeOAuth(rows=[{"accountId": 22769703,
                                     "policyId": 54248667}])
        ports = mlp.build_live_ports(
            policy_api_client=fake,
            browser_interests_reader=lambda aid: [
                {"lender_name": "First Bank", "loan_number": "LN-9"}])
        result = menc.enrich_work_item(policy_number="0296536326",
                                       ports=ports, dry_run=False)
        self.assertEqual(result.status, menc.STATUS_READY)
        self.assertEqual(result.mortgages[0].lender_name, "First Bank")
        self.assertEqual(result.mortgages[0].source, "browser")


class BrowserAdditionalInterestsReaderTests(unittest.TestCase):
    # Deterministic reader for the classic policy summary's Additional
    # Interests section. Fixture mirrors the DOM observed live on
    # 2026-09-27 (BOP #4714413, policyId 57408580). No network, no
    # browser: the HTML fetch is injected as a fake.

    FIXTURE_HTML = """<html><body>
<h5> Additional Interests </h5>
<table>
<tr><th>Name</th><th>Address</th><th>City / State / Zip</th><th>Type</th><th>Account #</th><th>Location #</th><th>Building #</th></tr>
<tr><td><span>Valley National Bank Its Succ &amp;/Or Assigns Atim</span></td><td><a href="http://maps.google.com/maps?q=PO Box 3409+Coppell+TX+75019-6403">PO Box 3409</a></td><td>Coppell, TX 75019-6403</td><td>Mortgagee</td><td>---</td><td>2</td><td>1</td></tr>
<tr><td><span>Somebody Additional Insured LLC</span></td><td>123 Main St</td><td>Anytown, NJ 08001</td><td>Additional Insured</td><td>---</td><td>1</td><td>1</td></tr>
</table>
</body></html>"""

    def test_parse_extracts_observed_row(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        entries = mlp.parse_additional_interests_html(self.FIXTURE_HTML)
        self.assertEqual(len(entries), 2)
        first = entries[0]
        self.assertEqual(first["lender_name"],
                         "Valley National Bank Its Succ &/Or Assigns Atim")
        self.assertEqual(first["address"], "PO Box 3409")
        self.assertEqual(first["city_state_zip"], "Coppell, TX 75019-6403")
        self.assertEqual(first["interest_type"], "Mortgagee")
        # Observed page has no "Loan Number" field; "Account #" renders
        # "---" when empty -> "".
        self.assertEqual(first["loan_number"], "")
        self.assertEqual(first["location_number"], "2")
        self.assertEqual(first["building_number"], "1")

    def test_parse_no_section_returns_empty(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        html = ("<html><body><h5> Coverages </h5><table>"
                "<tr><th>X</th></tr><tr><td>y</td></tr></table></body></html>")
        self.assertEqual(mlp.parse_additional_interests_html(html), [])

    def test_parse_empty_string_returns_empty(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        self.assertEqual(mlp.parse_additional_interests_html(""), [])

    def test_reader_builds_summary_url_from_policy_search(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        seen_urls = []

        def fake_fetch(url):
            seen_urls.append(url)
            return self.FIXTURE_HTML

        reader = mlp.build_browser_interests_reader(
            lambda number: {"applicant_id": "22769703",
                            "policy_id": "57408580"},
            fake_fetch)
        entries = reader("4714413")
        self.assertEqual(
            seen_urls,
            ["https://app.ezlynx.com/applicantportal/policy/"
             "57408580/summary/index"])
        # Only the Mortgagee row survives; the Additional Insured row
        # must not become a "mortgage".
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["lender_name"],
                         "Valley National Bank Its Succ &/Or Assigns Atim")

    def test_reader_unresolved_policy_id_raises_loud(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        reader = mlp.build_browser_interests_reader(
            lambda number: None, lambda url: self.FIXTURE_HTML)
        with self.assertRaises(RuntimeError):
            reader("4714413")

    def test_reader_blank_policy_number_raises(self):
        from robie_job_engine import mortgagee_live_ports as mlp
        reader = mlp.build_browser_interests_reader(
            lambda number: {"policy_id": "1"}, lambda url: "")
        with self.assertRaises(ValueError):
            reader("  ")

    def test_build_live_ports_prefers_fetch_html_over_legacy_reader(self):
        from robie_job_engine import mortgagee_live_ports as mlp

        class FakeOAuth:
            def search_policy_by_number(self, number):
                return {"data": {"results": [
                    {"accountId": "22769703", "policyId": "57408580"}]}}

        legacy = lambda number: [{"lender_name": "Legacy Bank"}]
        ports = mlp.build_live_ports(
            policy_api_client=FakeOAuth(),
            browser_interests_reader=legacy,
            fetch_html_fn=lambda url: self.FIXTURE_HTML)
        self.assertIsNot(ports.browser_interests_fn, legacy)
        entries = ports.browser_interests_fn("4714413")
        self.assertEqual(entries[0]["lender_name"],
                         "Valley National Bank Its Succ &/Or Assigns Atim")

    def test_enrichment_end_to_end_browser_reader_ready(self):
        from robie_job_engine import mortgagee_live_ports as mlp

        class FakeOAuth:
            def search_policy_by_number(self, number):
                return {"data": {"results": [
                    {"accountId": "22769703", "policyId": "57408580"}]}}

        ports = mlp.build_live_ports(
            policy_api_client=FakeOAuth(),
            fetch_html_fn=lambda url: self.FIXTURE_HTML)
        result = menc.enrich_work_item(policy_number="4714413",
                                       ports=ports, dry_run=False)
        self.assertEqual(result.status, menc.STATUS_READY)
        self.assertEqual(len(result.mortgages), 1)
        self.assertEqual(result.mortgages[0].lender_name,
                         "Valley National Bank Its Succ &/Or Assigns Atim")
        self.assertEqual(result.mortgages[0].loan_number, "")
        self.assertEqual(result.mortgages[0].source, "browser")

    def test_enrichment_browser_fetch_failure_holds_loud(self):
        from robie_job_engine import mortgagee_live_ports as mlp

        class FakeOAuth:
            def search_policy_by_number(self, number):
                return {"data": {"results": [
                    {"accountId": "22769703", "policyId": "57408580"}]}}

        def boom(url):
            raise ConnectionError("portal session expired")

        ports = mlp.build_live_ports(policy_api_client=FakeOAuth(),
                                     fetch_html_fn=boom)
        result = menc.enrich_work_item(policy_number="4714413",
                                       ports=ports, dry_run=False)
        self.assertEqual(result.status, menc.STATUS_HOLD)
        self.assertIn("browser additional-interests read failed",
                      result.reason)


if __name__ == "__main__":
    unittest.main()
