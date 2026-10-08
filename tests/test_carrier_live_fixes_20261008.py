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

    def test_invalid_hold_quotes_guards_own_message(self):
        page = _GuardLoginPage("Login\nInvalid User Code/Password combination.\nLOGIN", "https://gigezrate.guard.com/auth/")
        with mock.patch.object(guard_login, "_get_secret", side_effect=self._secrets):
            with self.assertRaises(IntakeHold) as ctx:
                guard_login.login_guard(page)
        self.assertIn("rejected the user code/password", str(ctx.exception))
        self.assertIn("Invalid User Code/Password combination.", str(ctx.exception))

    def test_locked_account_is_reported_as_locked_not_wrong_password(self):
        page = _GuardLoginPage("Login\nYour account has been locked after 3 invalid attempts.", "https://gigezrate.guard.com/auth/")
        with mock.patch.object(guard_login, "_get_secret", side_effect=self._secrets):
            with self.assertRaises(IntakeHold) as ctx:
                guard_login.login_guard(page)
        self.assertIn("locked or disabled", str(ctx.exception))
        self.assertIn("account has been locked", str(ctx.exception))
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


class GuardReloginTests(unittest.TestCase):
    """Live 2026-10-08 f53568f6: Guard bounced to /auth after 3 policies; 21 held."""

    LIST = "https://gigezrate.guard.com/portal/book-of-business"

    def _browser(self, url):
        browser = guard.PlaywrightGuardBrowser.__new__(guard.PlaywrightGuardBrowser)
        browser.page = mock.Mock()
        browser.page.url = url
        browser._list_url = self.LIST
        browser._relogins = 0
        browser.load_cancellations = mock.Mock(return_value=())
        return browser

    def test_signed_in_page_does_not_relogin(self):
        browser = self._browser(self.LIST)
        with mock.patch.object(guard_login, "login_guard") as login:
            self.assertFalse(browser.relogin_if_signed_out())
        login.assert_not_called()

    def test_auth_redirect_does_not_submit_the_password_again(self):
        browser = self._browser("https://gigezrate.guard.com/auth/")
        with mock.patch.object(guard_login, "login_guard") as login:
            with self.assertRaises(IntakeHold) as ctx:
                browser.relogin_if_signed_out()
        login.assert_not_called()
        self.assertIn("one login attempt only", str(ctx.exception))

    def test_relogin_is_capped(self):
        browser = self._browser("https://gigezrate.guard.com/auth/")
        browser._relogins = guard.GUARD_RELOGIN_LIMIT
        with mock.patch.object(guard_login, "login_guard") as login:
            with self.assertRaises(IntakeHold) as ctx:
                browser.relogin_if_signed_out()
        login.assert_not_called()
        self.assertIn("one login attempt only", str(ctx.exception))

    def test_relogin_refuses_non_test_host_before_secrets(self):
        browser = self._browser("https://gigezrate.guard.com/auth/")
        with mock.patch.object(guard, "require_hermes_test_host",
                               side_effect=IntakeHold("not hermes-test-01")), \
                mock.patch.object(guard, "refuse_production_host"), \
                mock.patch.object(guard_login, "login_guard") as login:
            with self.assertRaises(IntakeHold):
                browser.relogin_if_signed_out()
        login.assert_not_called()

    def test_return_to_cancellations_relogs_in_when_bounced(self):
        browser = self._browser(self.LIST)

        def goto(url, **_):
            browser.page.url = "https://gigezrate.guard.com/auth/"

        browser.page.goto.side_effect = goto
        with mock.patch.object(guard_login, "login_guard") as login:
            with self.assertRaises(IntakeHold) as ctx:
                browser.return_to_cancellations()
        login.assert_not_called()
        self.assertIn("one login attempt only", str(ctx.exception))
        self.assertEqual(browser.page.goto.call_count, 1)

    def test_run_pull_retries_policy_after_session_expiry(self):
        import os
        import tempfile

        from test_guard_pending_cancellation import AS_OF, LIVE_POLICIES, FakeGuardPage

        from robie_job_engine.intake_core import SourceArchive

        second = LIVE_POLICIES[1][0]

        class Browser(guard.PlaywrightGuardBrowser):
            expired_once = False
            relogins = 0

            def open_policy(self, policy_number):
                if policy_number == second and not self.expired_once:
                    self.expired_once = True
                    self.page.url = "https://gigezrate.guard.com/auth/"
                    raise type("Error", (Exception,), {})("navigation interrupted")
                return super().open_policy(policy_number)

            def relogin_if_signed_out(self):
                if not self.on_sign_in_page():
                    return False
                self.relogins += 1
                self.page.url = self._list_url
                return True

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"}):
            out = Path(tmp)
            page = FakeGuardPage()
            home = page.url
            browser = Browser(page)
            browser._list_url = home
            receipt = guard.run_pull(browser, guard.GuardDeliveryLedger(out),
                                     SourceArchive(out / "sources"), as_of=AS_OF)
        self.assertEqual(browser.relogins, 1)
        self.assertEqual(receipt["held"], [])
        self.assertEqual(receipt["count"], len(LIVE_POLICIES))


class BopFaoHomeWaitTests(unittest.TestCase):
    """Live 2026-10-08 f53568f6: BOP held "FAO Home did not open" after the FAO pull."""

    def test_waits_for_the_landing_url_after_the_home_click(self):
        from robie_job_engine import progressive_bop as bop

        report = "https://www.foragentsonly.com/managepolicies/reports/policiesneedservice/policiespendingcancellation/"
        landing = "https://www.foragentsonly.com/landingpages/managepolicies/"
        page = SimpleNamespace(url=report)
        seen = []

        def wait_for_url(predicate, timeout):
            seen.append(timeout)
            self.assertFalse(predicate(report))
            self.assertTrue(predicate(landing))
            page.url = landing

        page.wait_for_url = wait_for_url
        bop._wait_for_fao_shell_home(page)
        self.assertEqual(seen, [bop.FAO_HOME_WAIT_MS])
        self.assertEqual(page.url, landing)

    def test_wait_timeout_is_swallowed_so_the_caller_holds(self):
        from robie_job_engine import progressive_bop as bop

        page = SimpleNamespace(url="https://www.foragentsonly.com/managepolicies/reports/x/")
        page.wait_for_url = mock.Mock(side_effect=type("TimeoutError", (Exception,), {})("t"))
        bop._wait_for_fao_shell_home(page)
        self.assertFalse(bop._on_fao_shell_home(page))

    def test_already_home_does_not_wait(self):
        from robie_job_engine import progressive_bop as bop

        page = SimpleNamespace(url="https://www.foragentsonly.com/", wait_for_url=mock.Mock())
        bop._wait_for_fao_shell_home(page)
        page.wait_for_url.assert_not_called()


class FinysQuickSearchTests(unittest.TestCase):
    """Live 2026-10-08 f53568f6: "Finys policy search box is missing or ambiguous"."""

    class _Loc:
        def __init__(self, page, sel, n=1):
            self.page, self.sel, self.n = page, sel, n

        def count(self):
            return self.n

        def fill(self, value):
            self.page.events.append(("fill", self.sel, value))

        def press_sequentially(self, value, delay=0):
            self.page.events.append(("type", self.sel, value))

        def click(self):
            self.page.events.append(("click", self.sel))

        def get_by_role(self, role, name=None, exact=False):
            return FinysQuickSearchTests._Loc(self.page, f"{self.sel}>{role}:{name}", self.page.ok_count)

        def inner_text(self):
            return self.page.label

    def _page(self, widget=True):
        test = self

        class Page:
            url = "https://fos.finys.com/"

            def __init__(self):
                self.events = []
                self.ok_count = 0
                self.label = ""

            def locator(self, sel):
                return test._Loc(self, sel, 1 if widget else 0)

            def get_by_role(self, role, name=None, exact=False):
                return test._Loc(self, f"{role}:{name}", 0)

        return Page()

    def test_policy_number_row_and_its_own_search_button(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        page = self._page()
        with mock.patch.object(fos, "_wait_for", return_value=True):
            fos.search_policy(page, "SCNJM07385")
        self.assertEqual(page.events, [
            ("click", fos.FINYS_POLICY_SEARCH_INPUT),
            ("fill", fos.FINYS_POLICY_SEARCH_INPUT, ""),
            ("type", fos.FINYS_POLICY_SEARCH_INPUT, "SCNJM07385"),
            ("click", fos.FINYS_POLICY_SEARCH_BUTTON),
        ])
        self.assertTrue(fos.FINYS_POLICY_SEARCH_BUTTON.endswith("Button2"))

    def test_open_message_window_is_dismissed_before_typing(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        page = self._page()
        page.ok_count = 1
        with mock.patch.object(fos, "_wait_for", return_value=True):
            fos.search_policy(page, "SCNJM07385")
        self.assertEqual(page.events[0], ("click", ".k-window>button:Ok"))

    def test_summary_header_label_confirms_the_policy(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        page = self._page()
        page.label = "SCNJM07385"
        seen = []

        def fake_wait(cond):
            seen.append(cond())
            return seen[-1]

        with mock.patch.object(fos, "_wait_for", side_effect=fake_wait):
            fos.search_policy(page, "SCNJM07385")
        self.assertEqual(seen, [True])
        page.label = "HONJ017732"
        with mock.patch.object(fos, "_wait_for", side_effect=fake_wait):
            with self.assertRaisesRegex(IntakeHold, "Policy Summary for SCNJM07385"):
                fos.search_policy(page, "SCNJM07385")

    def test_no_widget_and_no_named_box_still_holds(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        with self.assertRaisesRegex(IntakeHold, "search box is missing"):
            fos.search_policy(self._page(widget=False), "SCNJM07385")

    def test_one_policy_search_failure_holds_that_policy_only(self):
        import os
        import tempfile

        from test_farmersofsalem_pending_cancellation import (
            AS_OF, HODJ, HONJ, PENDING_ROWS, FakeFinysPage, _docs_for, pdf_bytes,
        )

        from robie_job_engine import farmersofsalem_pending_cancellation as fos
        from robie_job_engine.intake_core import SourceArchive

        page = FakeFinysPage(
            pending_rows=PENDING_ROWS[:2],
            docs_by_policy={HONJ: _docs_for(HONJ, pdf_bytes(b"a")), HODJ: _docs_for(HODJ, pdf_bytes(b"b"))},
        )
        browser = fos.FinysFoSBrowser(page)
        real_open = browser.open_policy

        def open_policy(policy):
            if policy == HONJ:
                raise IntakeHold(f"Policy Summary for {policy} is missing or ambiguous")
            return real_open(policy)

        browser.open_policy = open_policy
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"}), \
                mock.patch.object(fos, "_NAV_TIMEOUT_MS", 300):
            out = Path(tmp) / "pull"
            receipt = fos.run_pull(browser, fos.LocalDeliveryLedger(out), SourceArchive(out / "sources"), as_of=AS_OF)
        self.assertEqual(receipt["count"], 1)
        self.assertEqual([h["policy_number"] for h in receipt["held"]], [HONJ])
        self.assertIn("Policy Summary", receipt["held"][0]["reason"])


class FaoStuckTabTests(unittest.TestCase):
    """Live 2026-10-08 f53568f6: one hung policygateway tab -> 11 later policies timed out."""

    def _browser(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        browser = fao.PlaywrightFaoCancellationBrowser.__new__(fao.PlaywrightFaoCancellationBrowser)
        browser._list_url = fao.REPORT_URL
        browser.agent_code = "x"
        stuck = mock.Mock()
        fresh = mock.Mock()
        fresh.url = fao.REPORT_URL
        stuck.context = SimpleNamespace(new_page=mock.Mock(return_value=fresh))
        browser.page = stuck
        return browser, stuck, fresh

    def test_replace_opens_report_in_same_context_and_closes_stuck_tab(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        browser, stuck, fresh = self._browser()
        browser.replace_stuck_tab()
        self.assertIs(browser.page, fresh)
        fresh.goto.assert_called_once_with(fao.REPORT_URL, wait_until="domcontentloaded",
                                           timeout=fao.STUCK_TAB_GOTO_MS)
        stuck.close.assert_called_once_with(run_before_unload=False, timeout=5000)

    def test_fresh_tab_off_fao_is_closed_and_stuck_tab_kept(self):
        browser, stuck, fresh = self._browser()
        fresh.url = "https://evil.example/"
        with self.assertRaises(IntakeHold):
            browser.replace_stuck_tab()
        fresh.close.assert_called_once()
        stuck.close.assert_not_called()
        self.assertIs(browser.page, stuck)

    def test_run_pull_replaces_tab_when_return_to_report_hangs(self):
        import os
        import tempfile

        from robie_job_engine import progressive_pending_cancellation as fao
        from robie_job_engine.intake_core import SourceArchive

        rows = tuple(
            SimpleNamespace(policy_number=n, tab_label="", reason="NON-PAYMENT", insured_name="A",
                            cancel_date=None, list_url="u")
            for n in ("984419689", "970498127")
        )
        timeout = type("TimeoutError", (Exception,), {})
        browser = mock.Mock()
        browser.screenshot_report.return_value = b"\x89PNG\r\n\x1a\n"
        browser.load_current_tab.side_effect = [rows, (), ()]
        browser.open_policy_summary.side_effect = [timeout("gateway"), timeout("gateway")]
        browser.return_to_report.side_effect = timeout("stuck")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"}), \
                mock.patch.object(fao, "_row_payload", side_effect=lambda row, **kw: {"policy": row.policy_number, **kw}), \
                mock.patch("robie_job_engine.document_retrieval_filing.require_carrier_pull"), \
                mock.patch.object(fao, "refuse_production_host"):
            fao.run_pull(browser, fao.FaoCancellationLedger(Path(tmp)), SourceArchive(Path(tmp) / "s"),
                         as_of=__import__("datetime").date(2026, 10, 8))
        self.assertEqual(browser.replace_stuck_tab.call_count, 2)


class FinysLandingReloadTests(unittest.TestCase):
    """Live 2026-10-08 c01e424c: tab left on Policy Summary -> "Finys table is missing"."""

    def test_reloads_landing_once_then_reads_tasks(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        page = mock.Mock()
        page.url = "https://fos.finys.com/"
        browser = fos.FinysFoSBrowser(page)
        items = (SimpleNamespace(policy_number="HONJ017732"),)
        with mock.patch.object(fos, "extract_pending_items",
                               side_effect=[IntakeHold("Finys table is missing or ambiguous"), items]), \
                mock.patch.object(fos, "_has_pending_grid", return_value=True), \
                mock.patch.object(fos, "_dismiss_finys_message") as dismiss:
            self.assertEqual(browser.load_pending_items(), items)
        page.goto.assert_called_once_with(fos.FINYS_LANDING_URL, wait_until="domcontentloaded")
        dismiss.assert_called_once()

    def test_still_missing_after_reload_holds(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        page = mock.Mock()
        page.url = "https://fos.finys.com/"
        browser = fos.FinysFoSBrowser(page)
        with mock.patch.object(fos, "extract_pending_items",
                               side_effect=IntakeHold("Finys table is missing or ambiguous")), \
                mock.patch.object(fos, "_wait_for", return_value=False), \
                mock.patch.object(fos, "_dismiss_finys_message"):
            with self.assertRaisesRegex(IntakeHold, "Finys table"):
                browser.load_pending_items()
        page.goto.assert_called_once()


class FinysDocumentSummaryLiveTests(unittest.TestCase):
    """Live 2026-10-08 fcc8ca8f: notice kind sits in Type, date in Process Date, icon link."""

    HEADERS = ("", "Email", "Description", "Department", "Department Group", "Type", "Process Date", "Remove from list")

    def _row(self, cells, links=1):
        icon = mock.Mock(name="dlink")
        row = SimpleNamespace(cells=cells)
        row.get_by_role = lambda *a, **k: SimpleNamespace(all=lambda: [])
        row.locator = lambda sel: SimpleNamespace(all=lambda: [icon] * links)
        return row, icon

    def _extract(self, rows):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        indexes = fos._header_indexes(self.HEADERS, fos._DOC_FIELDS)
        with mock.patch.object(fos, "_find_table_by_headers", return_value=(None, indexes)), \
                mock.patch.object(fos, "_table_body_rows", return_value=rows), \
                mock.patch.object(fos, "_cell_text", side_effect=lambda row, i: row.cells[i]):
            return fos.extract_documents(mock.Mock()), indexes

    def test_headers_map_type_and_process_date(self):
        _, indexes = self._extract([])
        self.assertEqual(indexes["description"], 2)
        self.assertEqual(indexes["doc_type"], 5)
        self.assertEqual(indexes["doc_date"], 6)

    def test_intent_to_cancel_in_type_column_is_the_target_with_icon_link(self):
        from datetime import date

        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        itc, icon = self._row(["", "", "renewal reminder notice", "Billing", "Billing",
                               "Intent to Cancel Notice", "9/14/2026", ""])
        inv, _ = self._row(["", "", "renewalinvoice", "Billing", "Billing", "Renewal Invoice", "8/12/2026", ""])
        docs, _ = self._extract([itc, inv])
        target = fos.select_target_document(docs)
        self.assertEqual(target.notice_key, "intent-to-cancel")
        self.assertEqual(target.doc_date, date(2026, 9, 14))
        self.assertIs(target.view, icon)
        self.assertIsNone(docs[1].notice_key)

    def test_two_icon_links_in_one_row_hold(self):
        itc, _ = self._row(["", "", "x", "Billing", "Billing", "Intent to Cancel Notice", "9/14/2026", ""], links=2)
        with self.assertRaisesRegex(IntakeHold, "View link"):
            self._extract([itc])


class BopStaleTabTests(unittest.TestCase):
    """Live 2026-10-08 03637095: stale BOP tab under a 'Session Expired' modal."""

    def test_closes_only_bop_app_tabs_in_the_shell_context(self):
        from robie_job_engine import progressive_bop as bop

        stale = mock.Mock(url="https://bop.americanstrategic.com/")
        other = mock.Mock(url="https://fos.finys.com/")
        shell = mock.Mock(url="https://www.foragentsonly.com/landingpages/managepolicies/")
        shell.context = SimpleNamespace(pages=[shell, stale, other])
        self.assertEqual(bop.close_stale_bop_tabs(shell), 1)
        stale.close.assert_called_once()
        other.close.assert_not_called()
        shell.close.assert_not_called()

    def test_session_expired_modal_holds_with_a_clear_reason(self):
        from robie_job_engine import progressive_bop as bop

        page = mock.Mock()
        dialog = page.locator.return_value
        dialog.count.return_value = 1
        dialog.is_visible.return_value = True
        dialog.inner_text.return_value = "× Session Expired You've been logged out due to inactivity. Ok"
        with self.assertRaisesRegex(IntakeHold, "Session Expired"):
            bop.open_pending_cancel_report(page)
        page.get_by_role.return_value.click.assert_not_called()

    def test_no_modal_carries_on(self):
        from robie_job_engine import progressive_bop as bop

        page = mock.Mock()
        page.locator.return_value.count.return_value = 0
        bop._raise_if_bop_session_expired(page)


class FinysSameTabPdfTests(unittest.TestCase):
    """Live 2026-10-08 4a22f443: the download icon navigated the same tab to GetFile/<x>.pdf?ft=..."""

    GETFILE = "https://fos.finys.com/FileManager/FileManager/GetFile/1053143_copy.pdf?ft=abc%3d"

    def _page(self, url):
        page = mock.Mock()
        page.url = url
        resp = mock.Mock()
        resp.body.return_value = PDF
        page.context = SimpleNamespace(pages=[page], request=mock.Mock(get=mock.Mock(return_value=resp)))
        return page

    def test_pdf_url_with_query_string_is_recognised(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        self.assertTrue(fos._is_pdf_url(self.GETFILE))
        self.assertFalse(fos._is_pdf_url("https://fos.finys.com/"))

    def test_playwright_timeout_falls_back_to_same_tab_pdf(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        page = self._page(self.GETFILE)
        page.expect_download.side_effect = type("TimeoutError", (Exception,), {})("Timeout 8000ms")
        self.assertEqual(fos.download_view_pdf(page, mock.Mock()), PDF)
        page.context.request.get.assert_called_once()
        page.close.assert_not_called()

    def test_same_tab_off_finys_is_not_fetched(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        page = self._page("https://evil.example/x.pdf")
        page.expect_download.side_effect = type("TimeoutError", (Exception,), {})("t")
        with self.assertRaisesRegex(IntakeHold, "did not produce a PDF"):
            fos.download_view_pdf(page, mock.Mock())
        page.context.request.get.assert_not_called()


class FaoViewerTimeoutTests(unittest.TestCase):
    """Live 2026-10-08 4a22f443: PDFHandler GET timed out -> whole carrier FAILED, viewer tab left open."""

    def test_viewer_timeout_holds_and_closes_the_viewer_tab(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        viewer = mock.Mock(url="https://clpolicy.foragentsonly.com/Express/PDFHandler.ashx?x=1")
        listeners = {}
        context = SimpleNamespace(on=lambda ev, fn: listeners.setdefault(ev, fn), remove_listener=lambda ev, fn: None)
        page = mock.Mock()
        page.context = context

        def click():
            listeners["page"](viewer)

        cm = mock.MagicMock()
        cm.__enter__.side_effect = lambda: (click(), None)[1]
        cm.__exit__.side_effect = lambda *a: (_ for _ in ()).throw(type("TimeoutError", (Exception,), {})("Timeout"))
        page.expect_download.return_value = cm
        with mock.patch.object(fao, "read_playwright_pdf_view",
                               side_effect=type("TimeoutError", (Exception,), {})("APIRequestContext.get: Timeout 8000ms")):
            with self.assertRaisesRegex(IntakeHold, "PDF viewer did not return the PDF"):
                fao.collect_document_capture(page, lambda: None)
        viewer.close.assert_called_once()

    def test_capture_failure_holds_one_policy_and_the_pull_goes_on(self):
        import os
        import tempfile
        from datetime import date

        from robie_job_engine import progressive_pending_cancellation as fao
        from robie_job_engine.intake_core import SourceArchive

        rows = tuple(
            SimpleNamespace(policy_number=n, tab_label="", reason="NON-PAYMENT", insured_name="A",
                            cancel_date=None, list_url="u")
            for n in ("875934744", "970498127")
        )
        doc = SimpleNamespace(document_id="d", filename="f.pdf", document_date=date(2026, 10, 6),
                              document_name="Cancel Notice")
        browser = mock.Mock()
        browser.screenshot_report.return_value = b"\x89PNG\r\n\x1a\n"
        browser.load_current_tab.side_effect = [rows, (), ()]
        browser.list_documents.return_value = (doc,)
        browser.capture_document.side_effect = type("TimeoutError", (Exception,), {})("APIRequestContext.get")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"}), \
                mock.patch.object(fao, "newest_cancellation_documents", return_value=[doc]), \
                mock.patch.object(fao, "_row_payload", side_effect=lambda row, **kw: {"policy": row.policy_number, **kw}), \
                mock.patch.object(fao.FaoCancellationLedger, "delivery_status", return_value=False), \
                mock.patch("robie_job_engine.document_retrieval_filing.require_carrier_pull"), \
                mock.patch.object(fao, "refuse_production_host"):
            receipt = fao.run_pull(browser, fao.FaoCancellationLedger(Path(tmp)), SourceArchive(Path(tmp) / "s"),
                                   as_of=date(2026, 10, 8))
        self.assertEqual([h["policy"] for h in receipt["held"]], ["875934744", "970498127"])
        self.assertIn("capture failed (TimeoutError)", receipt["held"][0]["reason"])


class BopSandboxedDownloadTests(unittest.TestCase):
    """Live 2026-10-08 d83ed53b: the sandboxed carrier Chrome returns empty downloads."""

    HEADER_ONLY = (
        "Pending Cancel for Non-Payment\nPrint Date: 10/08/2026\nPolicy Number\nInception \nDate\n"
        "Insured Name / Business Name\nState\nPhone \nNumber\nEmail Address\nCancel Date\nAmount Due\nPage 1 of 1"
    )
    FIELDS = {"ReportId": "3", "StartDate": "10/08/2026", "EndDate": "10/08/2026", "AgentId": "000000", "FileFormat": "pdf"}

    def _page(self, *, fields=None, body=b"%PDF-1.4 report", url="https://bop.americanstrategic.com/"):
        page = mock.Mock(url=url)
        form = page.locator.return_value
        form.count.return_value = 1
        form.evaluate.return_value = {
            "action": "https://bop.americanstrategic.com/Reports",
            "method": "post",
            "fields": dict(self.FIELDS if fields is None else fields),
        }
        page.context.request.post.return_value = SimpleNamespace(ok=True, body=lambda: body)
        return page

    def test_header_only_pdf_text_is_a_blank_report(self):
        from datetime import date
        from robie_job_engine import progressive_bop as bop

        report = bop.policies_from_report_text(self.HEADER_ONLY, report_date=date(2026, 10, 8), source="pdf")
        self.assertTrue(report.blank)
        self.assertEqual(report.policies, ())

    def test_header_with_a_long_number_still_holds(self):
        from datetime import date
        from robie_job_engine import progressive_bop as bop

        text = self.HEADER_ONLY.replace("Page 1 of 1", "Acme LLC NJ 7325550123 10/20/2026 $100.00\nPage 1 of 1")
        with self.assertRaisesRegex(IntakeHold, "policies are missing or ambiguous"):
            bop.policies_from_report_text(text, report_date=date(2026, 10, 8), source="pdf")

    def test_empty_download_replays_the_reports_form_for_a_pdf(self):
        from robie_job_engine import progressive_bop as bop

        page = self._page()
        empty = bop.PdfObservation(downloads=(b"",), pages=())
        with mock.patch.object(bop, "_matching_exports", return_value=[mock.Mock()]), \
                mock.patch.object(bop, "collect_pdf", return_value=empty):
            self.assertEqual(bop.download_export(page, bop._PDF_EXPORTS), b"%PDF-1.4 report")
        args, kwargs = page.context.request.post.call_args
        self.assertEqual(args[0], "https://bop.americanstrategic.com/Reports")
        self.assertEqual(kwargs["form"], self.FIELDS)

    def test_xls_form_or_non_pdf_reply_is_not_used(self):
        from robie_job_engine import progressive_bop as bop

        self.assertIsNone(bop._replay_bop_reports_form(self._page(fields={**self.FIELDS, "FileFormat": "xls"})))
        self.assertIsNone(bop._replay_bop_reports_form(self._page(body=b"<html>")))
        self.assertIsNone(bop._replay_bop_reports_form(self._page(url="https://www.foragentsonly.com/")))
        extra = self._page(fields={**self.FIELDS, "Other": "x"})
        self.assertIsNone(bop._replay_bop_reports_form(extra))
        extra.context.request.post.assert_not_called()

    def test_empty_xls_download_without_pdf_replay_falls_through(self):
        from robie_job_engine import progressive_bop as bop

        page = self._page(fields={**self.FIELDS, "FileFormat": "xls"})
        empty = bop.PdfObservation(downloads=(b"",), pages=())
        with mock.patch.object(bop, "_matching_exports", return_value=[mock.Mock()]), \
                mock.patch.object(bop, "collect_pdf", return_value=empty):
            self.assertIsNone(bop.download_export(page, bop._EXCEL_EXPORTS))
