"""Regression tests for the 2026-10-08 live dry-run findings on hermes-test-01."""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from robie_job_engine import carrier_page_capture, carrier_qa_drive, guard_login, utica_login
from robie_job_engine import geico_pending_cancellation_noc as geico
from robie_job_engine import guard_pending_cancellation as guard
from robie_job_engine.intake_core import IntakeHold

PDF = b"%PDF-1.4\n" + b"x" * 64
LIVE_SCRIBE = (
    "/dotnet/mvc/Workflow/ASCScribe/Home/DownloadScribeItem?scribeItemId="
    "~enrDdWSJ68G_pc_2b3_pc_2bAbc_pc_3d~&Download=true"
)


class _Resp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def body(self) -> bytes:
        return self._body


class _Request:
    def __init__(self, body: bytes, status: int = 200):
        self.calls: list[tuple[str, dict | None]] = []
        self._body = body
        self._status = status

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return _Resp(self._body, self._status)


def _guard_page(body: bytes = PDF, status: int = 200):
    request = _Request(body, status)
    page = SimpleNamespace(
        url="https://gigezrate.guard.com/dotNet/mvc/workflow/PrintableDocuments?MGACODE=PRAU716089",
        context=SimpleNamespace(request=request),
    )
    return page, request


class GuardScribeIdTests(unittest.TestCase):
    def test_live_tilde_wrapped_id_is_kept(self):
        self.assertEqual(guard.scribe_item_id_from_href(LIVE_SCRIBE), "~enrDdWSJ68G_pc_2b3_pc_2bAbc_pc_3d~")

    def test_unsafe_ids_still_rejected(self):
        self.assertEqual(guard.scribe_item_id_from_href("/x?scribeItemId=a\"]b"), "")
        self.assertEqual(guard.scribe_item_id_from_href("/x?scribeItemId=~~"), "")


class GuardSessionDownloadTests(unittest.TestCase):
    def test_session_fetch_returns_pdf_with_referer(self):
        page, request = _guard_page()
        obs = guard.fetch_document_via_session(page, LIVE_SCRIBE)
        self.assertEqual(obs.downloads, (PDF,))
        url, headers = request.calls[0]
        self.assertTrue(url.startswith("https://gigezrate.guard.com/dotnet/mvc/Workflow/ASCScribe/"))
        self.assertEqual(headers["Referer"], page.url)

    def test_session_fetch_refuses_other_hosts(self):
        page, request = _guard_page()
        self.assertIsNone(guard.fetch_document_via_session(page, "https://evil.example/x.pdf"))
        self.assertEqual(request.calls, [])

    def test_session_fetch_non_pdf_holds(self):
        page, _ = _guard_page(b"<html>login</html>")
        with self.assertRaises(IntakeHold):
            guard.fetch_document_via_session(page, LIVE_SCRIBE)

    def test_session_fetch_http_error_falls_back(self):
        page, _ = _guard_page(PDF, status=403)
        self.assertIsNone(guard.fetch_document_via_session(page, LIVE_SCRIBE))

    def test_unreadable_artifact_is_refetched(self):
        page, request = _guard_page()

        class Download:
            url = "https://gigezrate.guard.com" + LIVE_SCRIBE

            def path(self):
                return "/tmp/playwright-artifacts-gone/does-not-exist"

        self.assertEqual(guard._download_bytes(page, Download()), PDF)
        self.assertEqual(len(request.calls), 1)


class GuardCancellationsRetryTests(unittest.TestCase):
    def test_retries_tab_until_grid_parses(self):
        browser = guard.PlaywrightGuardBrowser.__new__(guard.PlaywrightGuardBrowser)
        browser.page = SimpleNamespace(evaluate=mock.Mock(), wait_for_timeout=mock.Mock())
        rows = ("row",)
        browser._read_cancellations_grid = mock.Mock(
            side_effect=[IntakeHold("Guard Cancellations grid headers are missing or ambiguous"), rows]
        )
        self.assertEqual(browser.load_cancellations(), rows)
        self.assertEqual(browser.page.evaluate.call_count, 1)

    def test_holds_after_max_attempts(self):
        browser = guard.PlaywrightGuardBrowser.__new__(guard.PlaywrightGuardBrowser)
        browser.page = SimpleNamespace(evaluate=mock.Mock(), wait_for_timeout=mock.Mock())
        browser._read_cancellations_grid = mock.Mock(side_effect=IntakeHold("headers"))
        with self.assertRaises(IntakeHold):
            browser.load_cancellations()
        self.assertEqual(browser._read_cancellations_grid.call_count, guard.CANCELLATIONS_TAB_ATTEMPTS)


class _Loc:
    def __init__(self, count=1, text=""):
        self._count = count
        self.text = text
        self.typed: list[str] = []
        self.clicked = 0
        self.first = self

    def count(self):
        return self._count

    def click(self):
        self.clicked += 1

    def press_sequentially(self, value, delay=None):
        self.typed.append(value)

    def inner_text(self):
        return self.text


class _GuardLoginPage:
    def __init__(self, after_body: str, after_url: str):
        self.url = "https://gigezrate.guard.com/auth/"
        self.body = "User Code | Password | LOGIN"
        self.user = _Loc()
        self.pw = _Loc()
        self.login = _Loc()
        self._after = (after_body, after_url)

    def goto(self, url, wait_until=None):
        self.goto_url = url

    def wait_for_timeout(self, ms):
        if self.login.clicked:
            self.body, self.url = self._after

    def locator(self, selector):
        if selector == "body":
            return _Loc(text=self.body)
        return {"input[name='Username']": self.user, "input[name='Password']": self.pw}[selector]

    def get_by_role(self, role, name=None):
        return self.login if name == "LOGIN" else _Loc(count=0)


class GuardLoginTests(unittest.TestCase):
    def _secrets(self, name):
        return {"berkshire_guard_username": "Cuser", "berkshire_guard_password": "pw-value"}[name]

    def test_uses_berkshire_secrets_and_confirms_logout_visible(self):
        page = _GuardLoginPage("Search Policy | Logout | Home", "https://gigezrate.guard.com/portal")
        with mock.patch.object(guard_login, "_get_secret", side_effect=self._secrets) as getter:
            guard_login.login_guard(page)
        self.assertEqual([c.args[0] for c in getter.call_args_list], ["berkshire_guard_username", "berkshire_guard_password"])
        self.assertEqual(page.user.typed, ["Cuser"])
        self.assertEqual(page.pw.typed, ["pw-value"])

    def test_invalid_credentials_hold_without_retry_or_echo(self):
        page = _GuardLoginPage("Invalid User Code/Password combination.", "https://gigezrate.guard.com/auth/")
        with mock.patch.object(guard_login, "_get_secret", side_effect=self._secrets):
            with self.assertRaises(IntakeHold) as ctx:
                guard_login.login_guard(page)
        self.assertEqual(page.login.clicked, 1)
        self.assertNotIn("pw-value", str(ctx.exception))

    def test_dry_run_guard_spec_auto_logs_in(self):
        from robie_job_engine import carrier_dry_run

        spec = carrier_dry_run.SPECS["guard"]
        self.assertEqual((spec.select_fn_name, spec.select_takes), ("ensure_guard_page", "browser"))

    def test_ensure_guard_page_refuses_non_test_host_before_secrets(self):
        browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[], new_page=mock.Mock())])
        with mock.patch.object(guard, "require_hermes_test_host", side_effect=IntakeHold("not test")), \
                mock.patch.object(guard_login, "_get_secret") as getter, \
                mock.patch.dict("os.environ", {"ROBIE_ENV": "TEST"}):
            with self.assertRaises(IntakeHold):
                guard.ensure_guard_page(browser)
        getter.assert_not_called()


class _ShotPage:
    def __init__(self, hang_full=False):
        self.fronted = 0
        self.calls = []
        self.hang_full = hang_full

    def bring_to_front(self):
        self.fronted += 1

    def screenshot(self, full_page=True, type="png", timeout=None):
        self.calls.append(full_page)
        if full_page and self.hang_full:
            raise type_error("TimeoutError")
        return b"\x89PNG\r\n\x1a\n"


def type_error(name):
    return type(name, (Exception,), {})("Page.screenshot: Timeout exceeded")


class ScreenshotTests(unittest.TestCase):
    def test_tab_is_brought_to_front_first(self):
        page = _ShotPage()
        self.assertTrue(carrier_page_capture.capture_png(page).startswith(b"\x89PNG"))
        self.assertEqual(page.fronted, 1)

    def test_full_page_timeout_falls_back_to_viewport(self):
        page = _ShotPage(hang_full=True)
        carrier_page_capture.capture_png(page, full_page=True)
        self.assertEqual(page.calls, [True, False])

    def test_natgen_uses_capture_helper(self):
        text = Path("robie_job_engine/natgen_pending_cancellation.py").read_text(encoding="utf-8")
        self.assertIn("capture_png(self.page, full_page=True)", text)
        self.assertNotIn('self.page.screenshot(full_page=True, type="png")', text)


class GeicoToggleTests(unittest.TestCase):
    def test_js_click_targets_pending_toggle_not_first_toggle(self):
        text = Path("robie_job_engine/geico_pending_cancellation_noc.py").read_text(encoding="utf-8")
        self.assertNotIn("document.querySelector('gds-toggle-button')", text)
        self.assertIn("Pending Cancellations/i.test", text)

    def test_clicked_but_unpressed_chip_is_not_selected(self):
        page = mock.Mock()
        page.get_by_role.return_value.count.return_value = 0
        with mock.patch.object(geico, "_pending_chip_view", return_value="unselected"), \
                mock.patch.object(geico, "_pending_chip_was_clicked", return_value=True):
            self.assertFalse(geico.pending_view_selected(page))

    def test_clicked_bare_chip_still_counts(self):
        page = mock.Mock()
        page.get_by_role.return_value.count.return_value = 0
        with mock.patch.object(geico, "_pending_chip_view", return_value="bare"), \
                mock.patch.object(geico, "_pending_chip_was_clicked", return_value=True):
            self.assertTrue(geico.pending_view_selected(page))


class _UticaPage:
    def __init__(self, body, url="https://login.uticafirst.com/oauth2/v1/authorize"):
        self.body = body
        self.url = url
        self.goto_calls = []
        self.send_button = _Loc(count=1)
        self.send_button.is_visible = lambda: True

    def locator(self, selector):
        if selector == "body":
            return _Loc(text=self.body)
        if selector == utica_login.SEND_EMAIL_BUTTON:
            return self.send_button
        raise AssertionError(selector)

    def wait_for_timeout(self, ms):
        pass

    def goto(self, url, wait_until=None):
        self.goto_calls.append(url)


class UticaLoginStepTests(unittest.TestCase):
    LIVE_FIRST_MFA = (
        "Get a verification email | carlo@streetsmart.insurance | Send a verification email to "
        'c***o@streetsmart.insurance by clicking on "Send me an email". | Back to sign in'
    )

    def test_get_a_verification_email_screen_is_detected(self):
        self.assertTrue(utica_login._is_verification_page(_UticaPage(self.LIVE_FIRST_MFA)))

    def test_send_clicks_the_button_not_the_help_text(self):
        page = _UticaPage(self.LIVE_FIRST_MFA)
        with mock.patch.object(utica_login, "_click_text") as click_text:
            utica_login._send_email_code(page)
        self.assertEqual(page.send_button.clicked, 1)
        click_text.assert_not_called()

    def test_okta_user_home_is_recognised(self):
        page = _UticaPage(
            "Carlo | Utica First Insurance Company | My Apps | Add apps to your launcher",
            url="https://login.uticafirst.com/app/UserHome?iss=x",
        )
        self.assertTrue(utica_login._on_okta_home(page))
        self.assertFalse(utica_login._on_okta_home(_UticaPage(self.LIVE_FIRST_MFA)))

    def test_login_opens_ufirstnow_from_okta_home(self):
        page = _UticaPage("My Apps", url="https://login.uticafirst.com/app/UserHome")
        state = {"n": 0}

        def logged_in(p):
            return bool(p.goto_calls and p.goto_calls[-1] == utica_login.UFIRSTNOW_URL)

        with mock.patch.object(utica_login, "_get_secret", return_value="x"), \
                mock.patch.object(utica_login, "is_logged_in", side_effect=logged_in), \
                mock.patch.object(utica_login, "_is_verification_page", return_value=False), \
                mock.patch.object(page, "locator", side_effect=lambda s: _Loc(count=0, text="My Apps")):
            utica_login.login_utica(page)
        self.assertEqual(page.goto_calls[-1], utica_login.UFIRSTNOW_URL)


class QaDriveLayoutTests(unittest.TestCase):
    def test_every_dry_run_carrier_has_a_top_level_folder(self):
        from robie_job_engine import carrier_dry_run

        for name in carrier_dry_run.CARRIER_ORDER:
            self.assertIn(name, carrier_qa_drive.CARRIER_QA_FOLDERS)
        ids = [fid for _, fid in carrier_qa_drive.CARRIER_QA_FOLDERS.values()]
        self.assertNotIn(carrier_qa_drive.RETIRED_NESTED_ROOT_ID, ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_module_folder_names_match_layout(self):
        import importlib

        for module, key in (
            ("guard_pending_cancellation", "guard"),
            ("natgen_pending_cancellation", "natgen"),
            ("progressive_pending_cancellation", "progressive"),
            ("utica_pending_cancellation", "uticafirst"),
            ("farmersofsalem_pending_cancellation", "farmersofsalem"),
            ("progressive_bop", "progressive_bop"),
        ):
            mod = importlib.import_module(f"robie_job_engine.{module}")
            title = carrier_qa_drive.CARRIER_QA_FOLDERS[key][0]
            self.assertEqual(mod.DRIVE_QA_FOLDER_NAME, f"{carrier_qa_drive.CARRIER_QA_DRIVE_ROOT_NAME}/{title}")
        self.assertEqual(geico.GEICO_QA_DRIVE_FOLDER_ID, carrier_qa_drive.CARRIER_QA_FOLDERS["geico"][1])


if __name__ == "__main__":
    unittest.main()


class GuardOnePolicyHoldsTests(unittest.TestCase):
    """Live 2026-10-08 PR run: policy 1 pulled, policy 2 timed out -> whole carrier FAILED."""

    def setUp(self):
        import os
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"})
        env.start()
        self.addCleanup(env.stop)

    def test_policy_center_timeout_holds_that_policy_only(self):
        from test_guard_pending_cancellation import AS_OF, LIVE_POLICIES, FakeGuardPage

        from robie_job_engine.intake_core import SourceArchive

        first = LIVE_POLICIES[0][0]

        class Browser(guard.PlaywrightGuardBrowser):
            def open_policy(self, policy_number):
                if policy_number == first:
                    raise type("TimeoutError", (Exception,), {})("Timeout 15000ms exceeded")
                return super().open_policy(policy_number)

        out = Path(self.tmp.name)
        receipt = guard.run_pull(
            Browser(FakeGuardPage()), guard.GuardDeliveryLedger(out), SourceArchive(out / "sources"), as_of=AS_OF
        )
        self.assertEqual(receipt["count"], len(LIVE_POLICIES) - 1)
        self.assertEqual(len(receipt["held"]), 1)
        self.assertIn(first, receipt["held"][0]["hold_reason"])
        self.assertIn("did not open", receipt["held"][0]["hold_reason"])

    def test_return_to_cancellations_reselects_the_tab(self):
        browser = guard.PlaywrightGuardBrowser.__new__(guard.PlaywrightGuardBrowser)
        browser.page = mock.Mock()
        browser._list_url = "https://gigezrate.guard.com/portal/book-of-business"
        browser.load_cancellations = mock.Mock(return_value=())
        browser.return_to_cancellations()
        browser.page.goto.assert_called_once()
        browser.load_cancellations.assert_called_once()

    def test_missing_policy_link_reselects_then_holds(self):
        browser = guard.PlaywrightGuardBrowser.__new__(guard.PlaywrightGuardBrowser)
        browser.page = mock.Mock()
        browser.page.evaluate.return_value = False
        browser._select_cancellations_tab = mock.Mock()
        with self.assertRaises(IntakeHold) as ctx:
            browser.open_policy("PRAU716089")
        browser._select_cancellations_tab.assert_called_once()
        self.assertIn("missing from the Cancellations grid", str(ctx.exception))
        browser.page.wait_for_selector.assert_not_called()


class NatGenErrorPageTests(unittest.TestCase):
    def _page(self, url, body):
        page = mock.Mock()
        page.url = url
        page.locator.return_value.inner_text.return_value = body
        return page

    def test_error_page_holds_with_natgen_reason(self):
        from robie_job_engine import natgen_pending_cancellation as ng

        page = self._page(
            "https://natgenagency.com/ErrorPage.aspx?eid=961733806",
            "Error Page\nRenewal policy exists: 2031936859-01\nNJ - Integon National - Personal Auto",
        )
        with self.assertRaises(IntakeHold) as ctx:
            ng.raise_if_natgen_error_page(page, "2031936859 00")
        self.assertIn("Renewal policy exists: 2031936859-01", str(ctx.exception))
        self.assertIn("2031936859 00", str(ctx.exception))

    def test_normal_page_passes(self):
        from robie_job_engine import natgen_pending_cancellation as ng

        ng.raise_if_natgen_error_page(self._page("https://natgenagency.com/Policy/PolicySummary.aspx", "x"), "1")

    def test_one_policy_hold_does_not_stop_the_pull(self):
        from datetime import date

        from robie_job_engine import natgen_pending_cancellation as ng
        from robie_job_engine.natgen_retrieval import NocDateHold

        portal = ng.NatGenPendingCancellationPortal.__new__(ng.NatGenPendingCancellationPortal)
        row = SimpleNamespace(
            policy_number="2031936859 00", processed_on=date(2026, 10, 8),
            cancel_effective=date(2026, 10, 20), document_id="doc-1", source_url="u", filename="f.pdf",
        )
        portal._rows = {"doc-1": row}
        portal.date_holds = []
        portal.browser = SimpleNamespace(
            capture_noc=mock.Mock(side_effect=IntakeHold("NatGen showed an error page for 2031936859 00: Renewal policy exists"))
        )
        with self.assertRaises(NocDateHold):
            portal.download_document("doc-1")
        self.assertEqual(len(portal.date_holds), 1)
        self.assertEqual(portal.date_holds[0]["document_id"], "doc-1")
        self.assertIsNone(portal.date_holds[0]["observed"])

    def test_capture_failure_still_restores_list(self):
        from robie_job_engine import natgen_pending_cancellation as ng

        browser = ng.PlaywrightNatGenNocBrowser.__new__(ng.PlaywrightNatGenNocBrowser)
        browser.page = SimpleNamespace(url="https://natgenagency.com/Reports/AgencyActivityReports.aspx?r=5")
        browser._list_url = browser.page.url
        browser._grid = object()
        browser._row_locators = (object(),)
        browser._restore_list = mock.Mock()
        target = SimpleNamespace(document_id="d", row_index=0, policy_number="1")
        with mock.patch.object(ng, "parse_noc_grid", return_value=[target]), \
                mock.patch.object(ng, "collect_noc_observation", side_effect=IntakeHold("boom")):
            with self.assertRaises(IntakeHold):
                browser.capture_noc("d")
        browser._restore_list.assert_called_once()


class ProgressiveFaoLiveTests(unittest.TestCase):
    """Live 2026-10-08: CL Express documents markup and the new policy servicing site."""

    def test_cl_express_headerless_name_column_parses(self):
        from datetime import date

        from robie_job_engine import progressive_pending_cancellation as fao

        docs = fao.parse_policy_documents(
            ("", "Date", "Delivery"),
            (
                ("E-mail", "9/28/2026", "EMAIL"),
                ("Cancel Notice", "9/28/2026", "USPS"),
                ("", "", ""),
                ("Cancel Notice", "8/24/2026", "USPS"),
                ("Final Cancel", "7/20/2026", "USPS"),
            ),
            policy_number="970498127",
        )
        self.assertEqual([d.document_name for d in docs], ["E-mail", "Cancel Notice", "Cancel Notice", "Final Cancel"])
        self.assertEqual([d.row_index for d in docs], [0, 1, 3, 4])
        newest = fao.newest_cancellation_documents(docs)
        self.assertEqual(len(newest), 1)
        self.assertEqual((newest[0].document_name, newest[0].document_date), ("Cancel Notice", date(2026, 9, 28)))

    def test_two_cancel_docs_on_newest_date_stay_ambiguous(self):
        from datetime import date

        from robie_job_engine import progressive_pending_cancellation as fao

        docs = [
            fao.FaoDocument("1", "Cancel Notice", date(2026, 9, 28), "USPS", 0),
            fao.FaoDocument("1", "Notice of Cancellation", date(2026, 9, 28), "USPS", 1),
            fao.FaoDocument("1", "Cancel Notice", date(2026, 8, 1), "USPS", 2),
        ]
        self.assertEqual(len(fao.newest_cancellation_documents(docs)), 2)

    def test_policy_servicing_host_is_accepted_and_recognised(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        url = "https://policyservicing.apps.foragentsonly.com/app/policy-hub/871490213/policy-and-coverages"
        self.assertTrue(fao._is_policy_page_url(url))
        self.assertEqual(fao.require_fao_url(url), url)
        self.assertFalse(fao._is_policy_page_url("https://www.foragentsonly.com/managepolicies/"))

    def test_cl_documents_header_link_is_used_when_no_tab(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        link = mock.Mock()
        link.is_visible.return_value = True
        tab_loc = mock.Mock()
        tab_loc.count.return_value = 0
        link_loc = mock.Mock()
        link_loc.count.return_value = 1
        link_loc.nth.return_value = link
        page = mock.Mock()
        page.get_by_role.side_effect = lambda role, name=None: tab_loc if role == "tab" else link_loc
        fao.open_cl_documents(page)
        link.click.assert_called_once()
        pattern = page.get_by_role.call_args_list[-1].kwargs["name"]
        self.assertTrue(pattern.match("Documents") and pattern.match("DOCUMENTS"))

    def test_policy_page_timeout_holds_one_policy_not_the_carrier(self):
        import os
        import tempfile

        from robie_job_engine import progressive_pending_cancellation as fao
        from robie_job_engine.intake_core import SourceArchive

        rows = (
            SimpleNamespace(policy_number="871490213", tab_label="", reason="NON-PAYMENT", insured_name="A",
                            cancel_date=None, list_url="u"),
            SimpleNamespace(policy_number="970498127", tab_label="", reason="NON-PAYMENT", insured_name="B",
                            cancel_date=None, list_url="u"),
        )
        browser = mock.Mock()
        browser.screenshot_report.return_value = b"\x89PNG\r\n\x1a\n"
        browser.load_current_tab.side_effect = [rows, (), ()]
        browser.open_policy_summary.side_effect = [type("TimeoutError", (Exception,), {})("x"), None]
        browser.list_documents.side_effect = IntakeHold("Progressive FAO document list is empty")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"}), \
                mock.patch.object(fao, "_row_payload", side_effect=lambda row, **kw: {"policy": row.policy_number, **kw}), \
                mock.patch("robie_job_engine.document_retrieval_filing.require_carrier_pull"), \
                mock.patch.object(fao, "refuse_production_host"):
            receipt = fao.run_pull(browser, fao.FaoCancellationLedger(Path(tmp)), SourceArchive(Path(tmp) / "s"),
                                   as_of=__import__("datetime").date(2026, 10, 8))
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual([h["policy"] for h in receipt["held"]], ["871490213", "970498127"])
        self.assertIn("did not open", receipt["held"][0]["reason"])


class FarmersOfSalemFinysGridTests(unittest.TestCase):
    def test_live_headers_map(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        headers = ("Details", "", "Loss #", "Policy/Quote", "Insured Name", "Notes", "Department", "Type",
                   "Due Days", "Due On", "Created By")
        idx = fos._header_indexes(headers, fos._PENDING_FIELDS)
        self.assertEqual((idx["policy_number"], idx["insured_name"], idx["item_type"], idx["due_date"]), (3, 4, 7, 9))

    def test_split_kendo_grid_keeps_only_cancellation_rows(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        def cell(text):
            return SimpleNamespace(inner_text=lambda: text)

        def row(*texts):
            cells = [cell(t) for t in texts]
            return SimpleNamespace(locator=lambda sel: SimpleNamespace(all=lambda: cells))

        live = [
            row("View Detail", "", "", "HONJ017732", "Noreen VanSalisbury", "n", "Agent", "Cancellation", "7", "10/15/2026", "System Generated"),
            row("View Detail", "", "", "QCD0039860", "Faisal Panjwani", "n", "Agent", "Referral", "-7", "10/1/2026", "M"),
            row("View Detail", "", "", "HONJM06241", "Chawki Azar", "n", "Agent", "Reinstatement", "-9", "9/29/2026", "M"),
            row("View Detail", "", "", "CDNJ001979", "Khalid Chaudhry", "n", "Agent", "Cancellation", "-10", "9/28/2026", "System Generated"),
        ]
        header_names = ["Details", "", "Loss #", "Policy/Quote", "Insured Name", "Notes", "Department", "Type", "Due Days", "Due On", "Created By"]
        heads = [cell(h) for h in header_names]

        class Body:
            def locator(self, sel):
                if sel == "tbody tr":
                    return SimpleNamespace(all=lambda: list(live))
                if sel.startswith("xpath="):
                    return SimpleNamespace(count=lambda: 0)
                return SimpleNamespace(all=lambda: [], first=SimpleNamespace(locator=lambda s: SimpleNamespace(all=lambda: [])))

        class Header:
            def locator(self, sel):
                if sel == "thead th":
                    return SimpleNamespace(all=lambda: heads)
                if sel.startswith("xpath=ancestor::div"):
                    return SimpleNamespace(count=lambda: 1, first=Body())
                return SimpleNamespace(all=lambda: [], first=SimpleNamespace(locator=lambda s: SimpleNamespace(all=lambda: [])))

        class Empty:
            def locator(self, sel):
                if sel.startswith("xpath="):
                    return SimpleNamespace(count=lambda: 0)
                return SimpleNamespace(all=lambda: [], first=SimpleNamespace(locator=lambda s: SimpleNamespace(all=lambda: [])))

        page = SimpleNamespace(url="https://fos.finys.com/", locator=lambda sel: SimpleNamespace(all=lambda: [Header(), Empty(), Body()]))
        items = fos.extract_pending_items(page)
        self.assertEqual([i.policy_number for i in items], ["HONJ017732", "CDNJ001979"])
        self.assertEqual(items[0].insured_name, "Noreen VanSalisbury")


class ProgressiveBopFromReconciledTests(unittest.TestCase):
    """BOP fixes carried over from feat/carrier-reconciled (live BOP app 2026-10-07/08)."""

    def test_new_report_export_button_names(self):
        from robie_job_engine import progressive_bop as bop

        self.assertIn("Export Pending Cancel for Non-Payment Xls", bop._EXCEL_EXPORTS)
        self.assertIn("Export Pending Cancel for Non-Payment Pdf", bop._PDF_EXPORTS)

    def test_default_cdp_is_the_carrier_browser_not_ezlynx(self):
        from robie_job_engine import progressive_bop as bop

        self.assertEqual(bop.DEFAULT_CDP_URL, "http://127.0.0.1:9223")

    def test_empty_export_is_not_treated_as_a_blank_report(self):
        from datetime import date

        from robie_job_engine import progressive_bop as bop

        with self.assertRaises(IntakeHold):
            bop.report_from_extracted(tables=[], page_text="", excel_bytes=b"", pdf_bytes=None,
                                      report_date=date(2026, 10, 8))

    def test_hplanding_without_bop_falls_back_to_direct_bop_app(self):
        from robie_job_engine import progressive_bop as bop

        shell = mock.Mock()
        hplanding = mock.Mock()
        calls = []

        def open_report(page, report_date=None):
            calls.append(page)
            if page is hplanding:
                raise IntakeHold("Progressive control 'View Reports' is missing or ambiguous")

        with mock.patch.object(bop, "assert_authenticated"), \
                mock.patch.object(bop, "_on_fao_shell_home", return_value=True), \
                mock.patch.object(bop, "ensure_fao_shell_home"), \
                mock.patch.object(bop, "assert_agent_context"), \
                mock.patch.object(bop, "open_businessowner_window", return_value=hplanding), \
                mock.patch.object(bop, "_is_hplanding_target", side_effect=lambda p: p is hplanding), \
                mock.patch.object(bop, "open_pending_cancel_report", side_effect=open_report):
            page = bop.navigate_to_pending_cancel(shell, "12345")
        self.assertIs(page, shell)
        shell.goto.assert_called_once_with(bop.BOP_APP_URL, wait_until="domcontentloaded")
        self.assertEqual(calls, [hplanding, shell])


class GuardNewestNoticeTests(unittest.TestCase):
    def _doc(self, desc, issued, group="Policy Documents"):
        from datetime import date

        m, d, y = (int(x) for x in issued.split("/"))
        return guard.GuardDocument(group=group, description=desc, form="F", issued=date(y, m, d),
                                   policy_number="JMWC776835", href="/x", scribe_item_id=f"~{desc[:3]}{d}~")

    def test_history_resolves_to_newest_notice(self):
        docs = [
            self._doc("Cancellation - 09/08/2026 - 09/08/2026", "09/08/2026"),
            self._doc("Notice of Cancellation - 08/17/2026 - 08/17/2026", "08/17/2026"),
            self._doc("Notice of Cancellation - 05/18/2026 - 05/18/2026", "05/18/2026"),
            self._doc("Cancellation - 10/01/2026", "10/01/2026", group="Billing Documents"),
        ]
        newest = guard.newest_cancellation_documents(docs)
        self.assertEqual([d.description for d in newest], ["Cancellation - 09/08/2026 - 09/08/2026"])

    def test_two_on_the_newest_date_stay_ambiguous(self):
        docs = [self._doc("Cancellation - 08/03/2026", "08/03/2026"), self._doc("Cancellation - 08/03/2026", "08/03/2026"),
                self._doc("Notice of Cancellation", "07/16/2026")]
        self.assertEqual(len(guard.newest_cancellation_documents(docs)), 2)


class NatGenReportFallbackTests(unittest.TestCase):
    def test_error_page_tab_opens_report_by_address(self):
        from robie_job_engine import natgen_pending_cancellation as ng

        page = mock.Mock()
        page.url = "https://natgenagency.com/ErrorPage.aspx?eid=1"
        state = {"on": False}
        page.goto.side_effect = lambda *a, **k: state.update(on=True)
        with mock.patch.object(ng, "assert_authenticated"), \
                mock.patch.object(ng, "_named_state", return_value="none"), \
                mock.patch.object(ng, "_already_on_pending_report", side_effect=lambda p: state["on"]):
            ng.open_pending_cancellations(page)
        page.goto.assert_called_once()
        self.assertEqual(page.goto.call_args.args[0], ng.PENDING_REPORT_URL)


class UticaExtGridTests(unittest.TestCase):
    TXN_HEADERS = ["", "POLICY NUMBER", "TRANSACTION TYPE", "INSURED NAME", "EFFECTIVE", "PROCESSED", "AGENT",
                   "BUSINESS INTRODUCER", "PRODUCER", "PREMIUM BEFORE", "CHANGE", "PREMIUM AFTER", "STATUS", "DOCUMENTS"]

    def _row(self, policy, ttype, insured, eff):
        return ["", policy, ttype, insured, eff, "10/08/2026", "Streetsmart", "Streetsmart", "", "", "$ 0.00",
                "$ 0.00", "Processed", "Documents"]

    def _grids(self):
        return [
            {"id": "blk_1", "headers": ["Pending Items", "Count"], "rows": []},
            {"id": "blk_60532882500_1366748", "headers": self.TXN_HEADERS, "rows": [
                self._row("ULC3001792810", "Pending Cancellation(NOC)", "THIESEN GROUP CORP", "11/13/2026"),
                self._row("ART3000699220", "Intent to Non-Renew", "JITOW LLC", "12/27/2026"),
                self._row("ART3000019221", "Pending Cancellation(NOC)", "ABSOLUTE LAWN SERVICES LLC", "10/23/2026"),
                self._row("ART3001040460", "Renewal", "Mangas Stucco and Painting LLC", "12/12/2026"),
            ]},
        ]

    def test_transaction_grid_is_found_and_parsed(self):
        from robie_job_engine import utica_pending_cancellation as ut

        grid_id, headers, rows = ut.find_ext_grid(self._grids(), ut._TXN_GRID_HEADERS, what="transactions")
        self.assertEqual(grid_id, "blk_60532882500_1366748")
        parsed = ut.parse_transactions_grid(headers, rows, list_url="u")
        targets = [r.policy_number for r in parsed if ut.is_cancellation_transaction(r.transaction_type)]
        self.assertEqual(targets, ["ULC3001792810", "ART3000699220", "ART3000019221"])
        self.assertEqual(ut.transaction_row_index(headers, rows, parsed[2]), 2)

    def test_live_document_grid_parses(self):
        from robie_job_engine import utica_pending_cancellation as ut

        grids = [{"id": "blk_60532946600_1293048",
                  "headers": ["", "ID", "NAME", "CONTENT TYPE", "DESCRIPTION", "ADDED DATE", "SOURCE", "RENDERING STATUS"],
                  "rows": [["", "505335313999", "NonPay Notice-Insured", "Document Package", "", "10/08/2026 02:23 AM",
                            "UF Document Delivery", "Completed"],
                           ["", "505335314099", "NonPay Notice-Agent", "Document Package", "", "10/08/2026 02:23 AM",
                            "UF Document Delivery", "Completed"]]}]
        _, headers, rows = ut.find_ext_grid(grids, ut._DOC_GRID_HEADERS, what="document")
        docs = ut.parse_document_grid(headers, rows, policy_number="ART3000019221")
        self.assertEqual([d.doc_id for d in docs], ["505335313999", "505335314099"])
        self.assertTrue(all(ut.is_cancellation_document(d.name, d.description) for d in docs))

    def test_unsafe_grid_id_holds(self):
        from robie_job_engine import utica_pending_cancellation as ut

        grids = self._grids()
        grids[1]["id"] = "x'];alert(1)//"
        with self.assertRaises(IntakeHold):
            ut.find_ext_grid(grids, ut._TXN_GRID_HEADERS, what="transactions")

    def test_open_transactions_uses_exit_from_document_list(self):
        from robie_job_engine import utica_pending_cancellation as ut

        page = mock.Mock()
        page.url = "https://ufirstnow.uticafirst.com/oneshield/sso?osst=abc"
        control = mock.Mock()
        browser = ut.PlaywrightUticaCancellationBrowser(page)
        with mock.patch.object(ut, "unique_control_ci", side_effect=[IntakeHold("missing"), control]), \
                mock.patch.object(ut, "click_ext_exit", return_value=True) as exit_click, \
                mock.patch.object(ut, "wait_for_text_ci", return_value=True):
            browser.open_transactions()
        exit_click.assert_called_once_with(page)
        control.click.assert_called_once()


class SecondLiveRoundTests(unittest.TestCase):
    """PR-code run 6e436e68 on hermes-test-01."""

    def test_fos_five_letter_policy_numbers(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        for value in ("HONJ017732", "CDNJ001979", "SCNJM07385"):
            self.assertEqual(fos.parse_policy_number(value), value)
        for bad in ("QCD0039860X", "AB12", "SCNJM0738"):
            with self.assertRaises(IntakeHold):
                fos.parse_policy_number(bad)

    def test_fao_tab_on_cl_express_is_still_the_fao_tab(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        cl = SimpleNamespace(url="https://clpolicy.foragentsonly.com/Express/Default.aspx?pageName=PolicyDocuments")
        other = SimpleNamespace(url="https://bop.americanstrategic.com/")
        self.assertIs(fao.select_fao_page([cl, other]), cl)
        fao_tab = SimpleNamespace(url="https://www.foragentsonly.com/home/")
        with self.assertRaises(IntakeHold):
            fao.select_fao_page([cl, fao_tab])

    def test_fao_report_load_from_policy_page_goes_home_before_agent_check(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        page = mock.Mock()
        page.url = "https://clpolicy.foragentsonly.com/Express/Default.aspx?pageName=PolicyDocuments"
        seen = []

        def goto(url, **kw):
            page.url = url
            seen.append(("goto", url))

        page.goto.side_effect = goto
        browser = fao.PlaywrightFaoCancellationBrowser.__new__(fao.PlaywrightFaoCancellationBrowser)
        browser.page, browser.agent_code, browser._list_url = page, "12345", ""
        with mock.patch.object(fao, "assert_agent_context", side_effect=lambda p, c: seen.append(("agent", p.url))):
            browser.load_report()
        self.assertEqual(seen[0], ("goto", fao.REPORT_URL))
        self.assertEqual(seen[1], ("agent", fao.REPORT_URL))

    def test_guard_printable_documents_timeout_holds_one_policy(self):
        import os
        import tempfile

        from test_guard_pending_cancellation import AS_OF, LIVE_POLICIES, FakeGuardPage

        from robie_job_engine.intake_core import SourceArchive

        first = LIVE_POLICIES[0][0]

        class Browser(guard.PlaywrightGuardBrowser):
            def __init__(self, page):
                super().__init__(page)
                self._current = ""

            def open_policy(self, policy_number):
                self._current = policy_number
                return super().open_policy(policy_number)

            def open_printable_documents(self):
                if self._current == first:
                    raise type("TimeoutError", (Exception,), {})("waiting for text=Policy Documents")
                return super().open_printable_documents()

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            out = Path(tmp)
            receipt = guard.run_pull(Browser(FakeGuardPage()), guard.GuardDeliveryLedger(out),
                                     SourceArchive(out / "sources"), as_of=AS_OF)
        self.assertEqual(receipt["count"], len(LIVE_POLICIES) - 1)
        self.assertIn("did not load", receipt["held"][0]["hold_reason"])
