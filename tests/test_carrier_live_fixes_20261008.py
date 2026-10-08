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
