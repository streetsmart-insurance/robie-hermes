"""Unit tests for robie_job_engine.report_fetcher. Fakes only — no live browser."""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock
from pathlib import Path

from _sibling_fakes import ensure_real_module

# The verification worker test modules install fake `robie_job_engine.*`
# siblings in sys.modules (plus the parent package attribute) at their import
# time, and pytest imports every test module before running any test. Evict
# any such fakes so the imports below bind the REAL modules under test.
# (The worker test modules already bound their fakes into their worker
# namespaces at their own import time, so this does not disturb them.)
ensure_real_module("robie_job_engine.verification_common")
rf = ensure_real_module("robie_job_engine.report_fetcher")

from durable_temp import durable_temporary_directory

from robie_job_engine.report_registry import (
    LOOK_ID_BY_REPORT,
    MORTGAGEE_4372_SCOPE_MARKER,
    ReportRegistryError,
    get_report_spec,
)


class _NoTouch:
    """Sentinel session: any attribute access means the browser was touched."""

    def __getattr__(self, name):
        raise AssertionError(f"browser/session must not be touched: {name}")


class _FakeDownload:
    def __init__(self, csv_text):
        self.csv_text = csv_text

    def save_as(self, dest):
        Path(dest).write_text(self.csv_text, encoding="utf-8")


class _FakeDownloadCtx:
    def __init__(self, csv_text):
        self._download = _FakeDownload(csv_text)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def value(self):
        return self._download


class _FakeLocator:
    def __init__(self, count=1, text=""):
        self._count = count
        self._text = text
        self.clicks = 0

    def count(self):
        return self._count

    def click(self):
        self.clicks += 1

    @property
    def first(self):
        return self

    def wait_for(self, timeout=None):
        return None

    def inner_text(self, timeout=None):
        return self._text


class _FakePage:
    def __init__(self, csv_text, body_text="report body"):
        self.csv_text = csv_text
        self.body_text = body_text
        self.visited = []
        self.locators_seen = []
        self.url = ""

    def goto(self, url, **kwargs):
        self.visited.append(url)
        self.url = url

    def locator(self, selector):
        self.locators_seen.append(selector)
        if selector == "body":
            return _FakeLocator(text=self.body_text)
        return _FakeLocator(count=1)

    def get_by_text(self, text, exact=False):
        self.locators_seen.append(f"get_by_text:{text}")
        return self.locator(f"text={text}")

    def expect_download(self, timeout=None):
        return _FakeDownloadCtx(self.csv_text)

    def wait_for_load_state(self, *args, **kwargs):
        return None


class _HubWithZeroSavedReport4372(_FakePage):
    """Hub that has 0 saved-report links for 4372 — the live Test miss."""

    def locator(self, selector):
        self.locators_seen.append(selector)
        if selector == "body":
            return _FakeLocator(text=self.body_text)
        if "4372" in selector and "4601" not in selector:
            return _FakeLocator(count=0)
        return _FakeLocator(count=1)


class _ZeroLook4601(_FakePage):
    """Look 4601 / title is not uniquely present after navigation."""

    def locator(self, selector):
        self.locators_seen.append(selector)
        if selector == "body":
            return _FakeLocator(text=self.body_text)
        if "4601" in selector or MORTGAGEE_4372_SCOPE_MARKER in selector:
            return _FakeLocator(count=0)
        return _FakeLocator(count=1)

    def get_by_text(self, text, exact=False):
        self.locators_seen.append(f"get_by_text:{text}")
        if text == MORTGAGEE_4372_SCOPE_MARKER:
            return _FakeLocator(count=0)
        return self.locator(f"text={text}")


def _run(run_id="run-1", fields=None):
    return {"run_id": run_id, "fields": list(fields or [])}


class StartRunRefusalTests(unittest.TestCase):
    def test_4359_start_run_refuses_before_browser(self):
        with durable_temporary_directory() as tmp:
            with self.assertRaises(ReportRegistryError) as ctx:
                rf.fetch_report_rows(
                    report_id="4359",
                    db_path=f"{tmp}/jobs.db",
                    session=_NoTouch(),
                )
            self.assertIn("4359", str(ctx.exception))

    def test_unknown_report_id_refuses(self):
        with durable_temporary_directory() as tmp:
            with self.assertRaises(ReportRegistryError):
                rf.fetch_report_rows(
                    report_id="9999", db_path=f"{tmp}/jobs.db", session=_NoTouch()
                )

    def test_metadata_only_alias_refuses(self):
        with durable_temporary_directory() as tmp:
            with self.assertRaises(ReportRegistryError):
                rf.fetch_report_rows(
                    report_id="4244", db_path=f"{tmp}/jobs.db", session=_NoTouch()
                )

    def test_missing_db_path_fails_closed(self):
        env = {key: value for key, value in os.environ.items() if key != "ROBIE_JOB_DB"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                rf.fetch_report_rows(report_id="4247", session=_NoTouch())

    def test_runtime_filters_fail_closed(self):
        # Runtime filters are fingerprinted but never applied in the Looker UI;
        # silently ignoring them would return wrong-scope rows.
        with durable_temporary_directory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                rf.fetch_report_rows(
                    report_id="4247",
                    filters={"carrier": "Coterie"},
                    db_path=f"{tmp}/jobs.db",
                    session=_NoTouch(),
                )
        self.assertIn("filters", str(ctx.exception))


class ParseCsvTests(unittest.TestCase):
    def test_rows_keyed_and_deduped_by_identity(self):
        spec = get_report_spec("4247")
        csv_text = (
            "policy_number,insured_name,carrier\n"
            "P1,Acme LLC,Coterie\n"
            "P2,Globex Inc,Travelers\n"
            "P1,Acme LLC Duplicate,Coterie\n"
            "\n"
        )
        rows = rf._parse_report_csv(csv_text, spec=spec, fields=None)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["policy_number"] for row in rows], ["P1", "P2"])
        # first occurrence wins
        self.assertEqual(rows[0]["insured_name"], "Acme LLC")

    def test_field_projection(self):
        spec = get_report_spec("4247")
        csv_text = "policy_number,insured_name,carrier\nP1,Acme,Coterie\n"
        rows = rf._parse_report_csv(
            csv_text, spec=spec, fields=["policy_number", "carrier"]
        )
        self.assertEqual(rows, [{"policy_number": "P1", "carrier": "Coterie"}])

    def test_missing_identity_column_fails_closed(self):
        spec = get_report_spec("4247")
        csv_text = "policy_name,carrier\nP1,Coterie\n"
        with self.assertRaises(RuntimeError) as ctx:
            rf._parse_report_csv(csv_text, spec=spec, fields=None)
        self.assertIn("missing identity columns", str(ctx.exception))

    def test_missing_requested_field_fails_closed(self):
        spec = get_report_spec("4247")
        csv_text = "policy_number,carrier\nP1,Coterie\n"
        with self.assertRaises(RuntimeError) as ctx:
            rf._parse_report_csv(
                csv_text, spec=spec, fields=["policy_number", "nope"]
            )
        self.assertIn("missing requested fields", str(ctx.exception))

    def test_row_missing_identity_value_fails_closed(self):
        spec = get_report_spec("4247")
        csv_text = "policy_number,carrier\n,Coterie\n"
        with self.assertRaises(RuntimeError) as ctx:
            rf._parse_report_csv(csv_text, spec=spec, fields=None)
        self.assertIn("missing an identity value", str(ctx.exception))

    def test_policy_number_identity_for_mortgagee(self):
        spec = get_report_spec("4372")
        self.assertEqual(spec.identity_fields, ("policy_number",))
        csv_text = "loan_number,policy_number\nL1,P1\nL2,P1\n"
        rows = rf._parse_report_csv(csv_text, spec=spec, fields=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["policy_number"], "P1")

    def test_policy_number_identity_accepts_looker_display_header(self):
        spec = get_report_spec("4372")
        csv_text = (
            "Applicant ID,Account Name,Policy Number\n"
            "A1,Acme,P1\n"
            "A1,Acme Duplicate,P1\n"
        )
        rows = rf._parse_report_csv(csv_text, spec=spec, fields=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Policy Number"], "P1")
        self.assertEqual(rows[0]["policy_number"], "P1")

    def test_loan_number_only_csv_is_not_4372_identity(self):
        spec = get_report_spec("4372")
        csv_text = "loan_number,account_name\nL1,Acme\n"
        with self.assertRaises(RuntimeError) as ctx:
            rf._parse_report_csv(csv_text, spec=spec, fields=None)
        self.assertIn("missing identity columns", str(ctx.exception))
        self.assertIn("policy_number", str(ctx.exception))


class ExportFailClosedTests(unittest.TestCase):
    def test_export_with_missing_identity_columns_raises(self):
        spec = get_report_spec("4247")
        page = _FakePage(csv_text="policy_name,carrier\nP1,Coterie\n")
        with durable_temporary_directory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                rf._export_looker_report_csv(
                    page, spec=spec, run=_run(), download_dir=Path(tmp)
                )
        self.assertIn("missing identity columns", str(ctx.exception))
        self.assertTrue(page.visited)
        self.assertIn("looker-reports", page.visited[0])

    def test_look_title_not_visible_fails_closed(self):
        spec = get_report_spec("4372")
        self.assertEqual(spec.filter_name, MORTGAGEE_4372_SCOPE_MARKER)
        page = _FakePage(
            csv_text="policy_number\nP1\n",
            body_text="some report without the look title or ROBIE Intake",
        )
        with durable_temporary_directory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                rf._export_looker_report_csv(
                    page, spec=spec, run=_run(), download_dir=Path(tmp)
                )
        self.assertIn(MORTGAGEE_4372_SCOPE_MARKER, str(ctx.exception))
        self.assertIn("unfiltered", str(ctx.exception))

    def test_robie_intake_alone_is_not_look_4601_scope(self):
        spec = get_report_spec("4372")
        page = _FakePage(
            csv_text="policy_number\nP1\n",
            body_text="Custom Filter Set: ROBIE Intake",
        )
        with durable_temporary_directory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                rf._export_looker_report_csv(
                    page, spec=spec, run=_run(), download_dir=Path(tmp)
                )
        self.assertIn(MORTGAGEE_4372_SCOPE_MARKER, str(ctx.exception))

    def test_look_4601_title_visible_exports_rows(self):
        spec = get_report_spec("4372")
        page = _FakePage(
            csv_text="policy_number,account_name\nP1,Acme\n",
            body_text=f"Look 4601 — {MORTGAGEE_4372_SCOPE_MARKER}",
        )
        with durable_temporary_directory() as tmp:
            rows = rf._export_looker_report_csv(
                page,
                spec=spec,
                run=_run(fields=["policy_number"]),
                download_dir=Path(tmp),
                fields=["policy_number"],
            )
        self.assertEqual(rows, [{"policy_number": "P1"}])
        self.assertTrue(any("/report/4601" in url for url in page.visited))


class LookIdMapTests(unittest.TestCase):
    def test_4372_maps_to_look_4601(self):
        self.assertEqual(LOOK_ID_BY_REPORT["4372"], "4601")
        self.assertEqual(rf.LOOK_ID_BY_REPORT["4372"], "4601")
        self.assertEqual(get_report_spec("4372").look_id, "4601")
        self.assertEqual(rf.look_id_for_report("4372"), "4601")
        self.assertEqual(rf.looker_look_url("4601"), f"{rf.REPORTS_5_BASE_URL}/report/4601")
        self.assertIn("looker-reports", rf.looker_look_url("4601"))

    def test_unmapped_report_has_no_look_id(self):
        self.assertNotIn("4247", LOOK_ID_BY_REPORT)
        self.assertIsNone(get_report_spec("4247").look_id)
        self.assertIsNone(rf.look_id_for_report("4247"))

    def test_mapped_look_succeeds_when_saved_report_4372_links_are_zero(self):
        # Live Test miss after #515: hub has 0 href*=4372 links. The map
        # must open look 4601 instead of raising the saved-report error.
        spec = get_report_spec("4372")
        page = _HubWithZeroSavedReport4372(
            csv_text="policy_number\nP1\n",
            body_text=MORTGAGEE_4372_SCOPE_MARKER,
        )
        with durable_temporary_directory() as tmp:
            rows = rf._export_looker_report_csv(
                page, spec=spec, run=_run(), download_dir=Path(tmp), fields=None
            )
        self.assertEqual(rows, [{"policy_number": "P1"}])
        self.assertTrue(any("/report/4601" in url for url in page.visited))
        saved_report_queries = [
            selector
            for selector in page.locators_seen
            if "4372" in selector and "4601" not in selector
        ]
        self.assertEqual(
            saved_report_queries,
            [],
            "mapped 4372 must not search the hub for saved-report 4372 links",
        )

    def test_mapped_look_zero_title_matches_fails_closed(self):
        spec = get_report_spec("4372")
        page = _ZeroLook4601(
            csv_text="policy_number\nP1\n",
            body_text="some other Shared look",
        )
        with durable_temporary_directory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                rf._export_looker_report_csv(
                    page, spec=spec, run=_run(), download_dir=Path(tmp)
                )
        message = str(ctx.exception)
        self.assertIn("4601", message)
        self.assertIn("found 0", message)
        self.assertIn(MORTGAGEE_4372_SCOPE_MARKER, message)
        self.assertNotIn("saved-report link for 4372", message)

    def test_unmapped_report_still_fails_on_zero_saved_report_links(self):
        spec = get_report_spec("4247")

        class _ZeroSaved4247(_FakePage):
            def locator(self, selector):
                self.locators_seen.append(selector)
                if "4247" in selector:
                    return _FakeLocator(count=0)
                return super().locator(selector)

        page = _ZeroSaved4247(csv_text="policy_number\nP1\n")
        with durable_temporary_directory() as tmp:
            with self.assertRaises(RuntimeError) as ctx:
                rf._export_looker_report_csv(
                    page, spec=spec, run=_run(), download_dir=Path(tmp)
                )
        self.assertIn("saved-report link for 4247", str(ctx.exception))
        self.assertIn("found 0", str(ctx.exception))

    def test_fields_none_returns_all_exported_columns(self):
        # Regression: the parser must use the CALLER's fields, not start_run()'s
        # resolved fields (identity fields only when omitted); otherwise every
        # non-identity column would be silently dropped.
        spec = get_report_spec("4372")
        page = _FakePage(
            csv_text="loan_number,policy_number\nL1,P1\n",
            body_text=MORTGAGEE_4372_SCOPE_MARKER,
        )
        with durable_temporary_directory() as tmp:
            rows = rf._export_looker_report_csv(
                page, spec=spec, run=_run(), download_dir=Path(tmp), fields=None
            )
        self.assertEqual(rows, [{"loan_number": "L1", "policy_number": "P1"}])


if __name__ == "__main__":
    unittest.main()
