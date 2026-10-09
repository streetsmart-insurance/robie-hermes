"""Sign-in wiring and the 2026-10-08 pull gaps. No live portals."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from robie_job_engine.intake_core import IntakeHold


def _reset_attempts() -> None:
    from robie_job_engine import farmersofsalem_login, progressive_login, travelers_login

    progressive_login._PASSWORD_SUBMITTED = False
    travelers_login._PASSWORD_SUBMITTED = False
    farmersofsalem_login._PASSWORD_SUBMITTED = False


class _Box:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind
        self.first = self

    def count(self):
        return 1

    def fill(self, value):
        self.page.filled.append((self.kind, value))


class _Button:
    def __init__(self, page):
        self.page = page
        self.first = self

    def count(self):
        return 1

    def click(self):
        self.page.clicks += 1


class _Zero:
    def count(self):
        return 0

    first = None


class ProgressiveLoginTests(unittest.TestCase):
    def setUp(self):
        _reset_attempts()

    def _page(self, *, reject=False, question=False):
        page = SimpleNamespace(
            url="about:blank",
            filled=[],
            clicks=0,
            body="User ID\nPassword\nLog In",
            reject=reject,
            question=question,
            password_on=True,
            answered=False,
        )

        def goto(url, **_kwargs):
            page.url = url

        def wait(_ms):
            if page.clicks and page.reject and not page.answered:
                page.body = "The user id or password is invalid."
                page.url = "https://foragentsonlylogin.progressive.com/Login/"
            elif page.clicks and page.question and not page.answered:
                page.body = "Security Question\nWhat city were you born in?"
                page.password_on = False
            elif page.answered or (page.clicks and not page.reject and not page.question):
                page.url = "https://www.foragentsonly.com/home"
                page.body = "Manage Policies"
                page.password_on = False

        def locator(sel):
            if sel == "body":
                return SimpleNamespace(inner_text=lambda: page.body)
            if sel == "input[type='password']":
                return _Box(page, "password") if page.password_on else _Zero()
            if sel == "input[name='userId']":
                return _Box(page, "user") if page.password_on else _Zero()
            if sel == "input[type='text']" and page.question and not page.password_on:
                box = _Box(page, "answer")
                real_fill = box.fill

                def fill(value):
                    real_fill(value)
                    page.answered = True

                box.fill = fill
                return box
            if "submit" in sel:
                return _Zero()
            return _Zero()

        page.goto = goto
        page.wait_for_timeout = wait
        page.locator = locator
        page.get_by_role = lambda role, name=None: _Button(page) if role == "button" else _Zero()
        return page

    def test_secret_names_are_the_robie_pair_only(self):
        from robie_job_engine import progressive_login as login

        self.assertEqual(login.USER_SECRET, "progressive-robie-login")
        self.assertEqual(login.PASS_SECRET, "progressive-robie-password")
        self.assertEqual(login.QUESTIONS_SECRET, "progressive-robie-security-questions")
        self.assertEqual(login.UNUSED_PROGRESSIVE_USERNAME, "progressive_username")
        page = self._page()
        with mock.patch.object(login, "_require_test_host"), \
                mock.patch.object(login, "_get_secret", side_effect=AssertionError("secrets")):
            login.login_progressive(
                page, credentials=lambda: ("33617c", "pw-value"), questions=()
            )
        self.assertEqual([kind for kind, _ in page.filled], ["user", "password"])
        self.assertEqual(page.clicks, 1)
        self.assertNotIn("pw-value", "")

    def test_rejection_is_one_attempt_and_does_not_echo_the_password(self):
        from robie_job_engine import progressive_login as login

        page = self._page(reject=True)
        with mock.patch.object(login, "_require_test_host"), \
                mock.patch.object(login, "_get_secret") as getter:
            with self.assertRaises(IntakeHold) as ctx:
                login.login_progressive(page, credentials=lambda: ("33617c", "pw-value"))
            with self.assertRaises(IntakeHold) as again:
                login.login_progressive(self._page(), credentials=lambda: ("33617c", "pw-value"))
        getter.assert_not_called()
        self.assertEqual(page.clicks, 1)
        self.assertIn("Not retried", str(ctx.exception))
        self.assertIn("already attempted", str(again.exception))
        self.assertNotIn("pw-value", str(ctx.exception))

    def test_security_question_is_answered_once_from_the_secret(self):
        from robie_job_engine import progressive_login as login

        page = self._page(question=True)
        pairs = (("What city were you born in?", "answer-value"),)
        with mock.patch.object(login, "_require_test_host"):
            login.login_progressive(page, credentials=lambda: ("33617c", "pw-value"), questions=pairs)
        self.assertEqual(page.filled[-1], ("answer", "answer-value"))
        self.assertNotIn("answer-value", page.body)
        self.assertTrue(page.url.startswith("https://www.foragentsonly.com/"))

    def test_unknown_question_holds_without_a_guess(self):
        from robie_job_engine import progressive_login as login

        with self.assertRaises(IntakeHold):
            login.matching_answer((("pet name", "x"), ("first school", "y")), "Security Question\nMother's maiden name")


class TravelersTabTests(unittest.TestCase):
    def setUp(self):
        _reset_attempts()

    def test_secret_names_and_one_rejection(self):
        from robie_job_engine import travelers_login as login

        self.assertEqual((login.USER_SECRET, login.PASS_SECRET), ("travelers_username", "travelers_password"))
        page = SimpleNamespace(
            url="about:blank", body="Sign In", filled=[], clicks=0, password_on=True,
        )

        def locator(sel):
            if sel == "body":
                return SimpleNamespace(inner_text=lambda: page.body)
            if sel == "input[type='password']" and page.password_on:
                return _Box(page, "password")
            if sel == "input[name='username']":
                return _Box(page, "user")
            return _Zero()

        def wait(_ms):
            if page.clicks:
                page.body = "Incorrect username or password."
                page.url = "https://foragents.travelers.com/login"

        page.locator = locator
        page.goto = lambda url, **k: setattr(page, "url", url)
        page.wait_for_timeout = wait
        page.get_by_role = lambda role, name=None: _Button(page) if role == "button" else _Zero()
        with mock.patch.object(login, "_require_test_host"):
            with self.assertRaises(IntakeHold) as ctx:
                login.login_travelers(page, credentials=lambda: ("CarloF1", "pw-value"))
            with self.assertRaises(IntakeHold):
                login.login_travelers(page, credentials=lambda: ("CarloF1", "pw-value"))
        self.assertEqual(page.clicks, 1)
        self.assertNotIn("pw-value", str(ctx.exception))
        self.assertIn("Not retried", str(ctx.exception))

    def test_zero_tabs_opens_one_and_two_tabs_keeps_one(self):
        from robie_job_engine import travelers_pending_cancellation as travelers

        opened = []

        class Page:
            def __init__(self, url):
                self.url = url
                self.closed = False

            def close(self):
                self.closed = True

        class Ctx:
            def __init__(self, pages):
                self.pages = pages

            def new_page(self):
                page = Page("about:blank")
                opened.append(page)
                self.pages.append(page)
                return page

        empty = SimpleNamespace(contexts=[Ctx([])])
        with mock.patch.object(travelers, "require_carrier_pull", create=True), \
                mock.patch("robie_job_engine.document_retrieval_filing.require_carrier_pull"), \
                mock.patch.object(travelers, "refuse_production_host"), \
                mock.patch.object(travelers, "require_hermes_test_host"), \
                mock.patch("robie_job_engine.travelers_login.login_travelers", side_effect=lambda page, **k: setattr(page, "url", "https://foragents.travelers.com/Business")), \
                mock.patch("robie_job_engine.travelers_login.is_signed_in", side_effect=lambda page: "foragents.travelers.com/Business" in page.url and "login" not in page.url):
            page = travelers.ensure_travelers_page(empty)
        self.assertEqual(len(opened), 1)
        self.assertIs(page, opened[0])

        first, second = Page("https://foragents.travelers.com/Business"), Page("https://foragents.travelers.com/login")
        browser = SimpleNamespace(contexts=[Ctx([first, second])])
        with mock.patch("robie_job_engine.document_retrieval_filing.require_carrier_pull"), \
                mock.patch.object(travelers, "refuse_production_host"), \
                mock.patch("robie_job_engine.travelers_login.login_travelers") as login, \
                mock.patch("robie_job_engine.travelers_login.is_signed_in", side_effect=lambda page: page.url.endswith("/Business")):
            chosen = travelers.ensure_travelers_page(browser)
        self.assertIs(chosen, first)
        self.assertTrue(second.closed)
        login.assert_not_called()


class FarmersLoginTests(unittest.TestCase):
    def setUp(self):
        _reset_attempts()

    def test_uses_the_newer_pair_not_the_robie_pair(self):
        from robie_job_engine import farmersofsalem_login as login

        self.assertEqual(login.USER_SECRET, "farmers_of_salem_username")
        self.assertEqual(login.PASS_SECRET, "farmers_of_salem_password")
        self.assertTrue(login.OLDER_USER_SECRET.startswith("farmers_of_salem_robie_"))
        page = SimpleNamespace(url="about:blank", body="Login", filled=[], clicks=0, password_on=True)

        def locator(sel):
            if sel == "body":
                return SimpleNamespace(inner_text=lambda: page.body)
            if sel == f"#{login.USER_ID}":
                return _Box(page, "user")
            if sel == f"#{login.PASS_ID}" and page.password_on:
                return _Box(page, "password")
            if sel == f"#{login.SUBMIT_ID}":
                return _Button(page)
            return _Zero()

        def wait(_ms):
            if page.clicks:
                page.url = login.AGENT_HOME_URL
                page.body = "Farmers Of Salem :: Agent Home\nFOS PORTAL"
                page.password_on = False

        page.locator = locator
        page.title = lambda: "Farmers Of Salem :: Agent Login"
        page.goto = lambda url, **k: setattr(page, "url", url)
        page.wait_for_timeout = wait
        page.get_by_role = lambda role, name=None: _Zero()
        finys = SimpleNamespace(url="https://fos.finys.com/")
        seen = []

        def secrets(name):
            seen.append(name)
            return {"farmers_of_salem_username": "user", "farmers_of_salem_password": "pw-value"}[name]

        with mock.patch.object(login, "_require_test_host"), mock.patch.object(login, "_get_secret", side_effect=secrets):
            returned = login.login_farmers(page, open_portal=lambda _page: finys)
        self.assertIs(returned, finys)
        self.assertEqual(seen, ["farmers_of_salem_username", "farmers_of_salem_password"])
        self.assertEqual(page.clicks, 1)
        with mock.patch.object(login, "_require_test_host"):
            with self.assertRaises(IntakeHold) as ctx:
                login.login_farmers(SimpleNamespace(url="https://farmersofsalem.com/agent_login.aspx"), credentials=lambda: ("u", "p"))
        self.assertIn("already attempted", str(ctx.exception))


class UticaPagingTests(unittest.TestCase):
    def test_reads_past_the_first_25_rows(self):
        from robie_job_engine import utica_pending_cancellation as utica

        first = tuple((f"p{i}",) for i in range(25))
        second = tuple((f"p{i}",) for i in range(25, 30))
        states = iter(["next", "done"])
        rows = {"now": first}

        def click():
            rows["now"] = second
            return True

        collected = utica.collect_paged_rows(
            first, next_state=lambda: next(states), click_next=click, read_rows=lambda: rows["now"], what="transactions"
        )
        self.assertEqual(len(collected), 30)
        self.assertEqual(collected[-1], ("p29",))

    def test_same_page_twice_holds(self):
        from robie_job_engine import utica_pending_cancellation as utica

        rows = (("only",),)
        with self.assertRaisesRegex(IntakeHold, "same rows"):
            utica.collect_paged_rows(
                rows, next_state=lambda: "next", click_next=lambda: True, read_rows=lambda: rows, what="transactions"
            )


class ProgressiveServicingTests(unittest.TestCase):
    def _page(self, heading="Auto 871490213"):
        from robie_job_engine import progressive_pending_cancellation as fao

        class Head:
            def inner_text(self):
                return heading

        class Empty:
            def count(self):
                return 0

            def all(self):
                return []

        class Heads:
            def all(self):
                return [Head()]

        class Page:
            def __init__(self):
                self.url = "https://policyservicing.apps.foragentsonly.com/app/policy-hub/871490213/policy-and-coverages"
                self.gotos = []
                self.selectors = []

            def locator(self, selector):
                if selector == "h1, h2, h3, h4":
                    return Heads()
                return Empty()

            def goto(self, url, **_kwargs):
                self.gotos.append(url)
                self.url = url

            def wait_for_selector(self, selector, timeout=None):
                self.selectors.append(selector)

        page = Page()
        page.hub = fao.DOCUMENTS_HUB_URL
        return page

    def test_documents_hub_is_opened_for_the_current_policy(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        page = self._page()
        fao.open_policy_servicing_documents(page, "871490213")
        self.assertEqual(page.gotos, [fao.DOCUMENTS_HUB_URL])
        self.assertNotIn("/policy-hub/871490213/documents", page.gotos[0])
        self.assertIn(fao.ARCHIVE_TABLE_CSS, page.selectors)

    def test_token_url_is_not_a_finished_policy_page(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        token = "https://policyservicing.apps.foragentsonly.com/app/token?resume=/app/policy-hub/935495408"
        hub = "https://policyservicing.apps.foragentsonly.com/app/policy-hub/935495408/policy-and-coverages"
        self.assertFalse(fao._is_policy_page_url(token))
        self.assertTrue(fao._is_policy_page_url(hub))

    def test_heading_must_match_before_documents_are_opened(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        page = self._page(heading="Auto 111111111")
        with self.assertRaisesRegex(IntakeHold, "heading does not match 935495408"):
            fao.open_policy_servicing_documents(page, "935495408")
        self.assertEqual(page.gotos, [])

    def test_archive_row_parses_the_shared_date_and_title_cell(self):
        from datetime import date

        from robie_job_engine import progressive_pending_cancellation as fao

        doc = fao.parse_archive_document(
            policy_number="935495408",
            date_text="09/25/26",
            title="Cancel Notice (PDF)",
            delivery="USPS",
            row_index=0,
        )
        self.assertEqual(doc.document_name, "Cancel Notice")
        self.assertEqual(doc.document_date, date(2026, 9, 25))
        self.assertEqual(doc.delivery, "USPS")
        self.assertTrue(fao.is_cancellation_document("Cancel Notice"))
        self.assertTrue(fao.is_cancellation_document("Nonpayment"))
        self.assertEqual(fao.parse_carrier_date("09/25/26"), date(2026, 9, 25))

    def test_page_wait_retries_once_then_holds(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        calls = {"n": 0}

        def action():
            calls["n"] += 1
            raise TimeoutError("timed out")

        with self.assertRaisesRegex(IntakeHold, "one retry was used"):
            fao._with_one_retry(action, what="policy 871490213")
        self.assertEqual(calls["n"], 2)


class GeicoPolicyLinkTests(unittest.TestCase):
    def test_policy_number_link_is_followed_when_view_policy_is_absent(self):
        from robie_job_engine import geico_pending_cancellation_noc as geico

        policy = "1234567890"
        anchor = mock.Mock()
        anchor.get_attribute.return_value = "https://edgeextended.geico.com/policy/1234567890"
        anchor.inner_text.return_value = policy
        anchors = mock.Mock()
        anchors.count.return_value = 1
        anchors.nth.return_value = anchor
        view = mock.Mock()
        view.count.return_value = 0
        row = mock.Mock()
        row.get_by_role.return_value = view
        row.locator.return_value = anchors
        browser = geico.PlaywrightGeicoNocBrowser.__new__(geico.PlaywrightGeicoNocBrowser)
        browser.page = mock.Mock()
        with mock.patch.object(browser, "_follow_policy_link", return_value=geico.NoticePath("billing_only")) as follow:
            path = browser._resolve_policy_without_view_link(row, policy)
        follow.assert_called_once()
        self.assertEqual(path.kind, "billing_only")

    def test_unresolvable_row_names_the_policy(self):
        from robie_job_engine import geico_pending_cancellation_noc as geico

        empty = mock.Mock()
        empty.count.return_value = 0
        row = mock.Mock()
        row.locator.return_value = empty
        row.get_by_role.return_value = empty
        browser = geico.PlaywrightGeicoNocBrowser.__new__(geico.PlaywrightGeicoNocBrowser)
        path = browser._resolve_policy_without_view_link(row, "1234567890")
        self.assertEqual(path.kind, "no_policy_link")
        self.assertIn("1234567890", path.detail)
        self.assertIn("no policy link or details view", path.detail)


class FarmersCommercialHoldTests(unittest.TestCase):
    def test_kendo_link_header_and_commercial_row_without_a_policy(self):
        from robie_job_engine import farmersofsalem_pending_cancellation as fos

        def cell(text, link=""):
            node = SimpleNamespace(inner_text=lambda: link or text)

            def locator(sel):
                if sel == ".k-link" and link:
                    return SimpleNamespace(all=lambda: [node])
                return SimpleNamespace(all=lambda: [])

            return SimpleNamespace(inner_text=lambda: text, locator=locator)

        def row(*texts):
            cells = [cell(t) for t in texts]
            return SimpleNamespace(locator=lambda sel: SimpleNamespace(all=lambda: cells))

        live = [
            row("View Detail", "", "", "HONJ017732", "Noreen", "n", "Agent", "Cancellation", "7", "10/15/2026", "System"),
            row("View Detail", "", "", "", "Commercial LLC", "n", "Commercial", "Diary", "1", "10/20/2026", "System"),
        ]
        heads = [cell("", link=h) if h else cell("") for h in (
            "Details", "", "Loss #", "Policy/Quote", "Insured Name", "Notes", "Department", "Type",
            "Due Days", "Due On", "Created By",
        )]

        class Header:
            def locator(self, sel):
                if sel == "thead th":
                    return SimpleNamespace(all=lambda: heads)
                if sel.startswith("xpath="):
                    return SimpleNamespace(count=lambda: 1, first=Body())
                return SimpleNamespace(all=lambda: [])

        class Body:
            def locator(self, sel):
                if sel == "tbody tr":
                    return SimpleNamespace(all=lambda: list(live))
                return SimpleNamespace(all=lambda: [], count=lambda: 0)

        page = SimpleNamespace(
            url="https://fos.finys.com/",
            locator=lambda sel: SimpleNamespace(all=lambda: [Header(), Body()]),
        )
        items = fos.extract_pending_items(page)
        self.assertEqual([item.policy_number for item in items], ["HONJ017732"])
        self.assertEqual(len(page.fos_row_holds), 1)
        self.assertIn("commercial item", page.fos_row_holds[0]["reason"])
        self.assertIn("Commercial LLC", page.fos_row_holds[0]["reason"])


class GuardSecretTests(unittest.TestCase):
    def test_guard_stays_on_berkshire_secrets(self):
        from robie_job_engine import guard_login

        self.assertEqual(guard_login.GUARD_USER_SECRET, "berkshire_guard_username")
        self.assertEqual(guard_login.GUARD_PASS_SECRET, "berkshire_guard_password")
        self.assertNotEqual(guard_login.GUARD_USER_SECRET, "guard_username")


class DailySignInTests(unittest.TestCase):
    def test_workers_that_need_a_login_are_wired_and_a_rejection_does_not_start_the_pull(self):
        from datetime import date
        from pathlib import Path
        import tempfile

        from robie_job_engine import carrier_daily_run as daily

        self.assertIn("progressive_login.ensure_progressive_tab", daily.CARRIER_SIGN_IN["progressive"])
        self.assertIn("progressive_login.ensure_progressive_tab", daily.CARRIER_SIGN_IN["progressive_bop"])
        self.assertIn("travelers_login.ensure_travelers_tab", daily.CARRIER_SIGN_IN["travelers"])
        self.assertIn("farmersofsalem_login.ensure_farmers_tab", daily.CARRIER_SIGN_IN["farmersofsalem"])
        self.assertIn("travelers", daily.DAILY_CARRIERS)
        ran = []

        def open_tab(name, _url):
            if name == "travelers":
                raise IntakeHold("Travelers rejected the username or password. Not retried, so the account is not locked.")
            return None

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"ROBIE_ENV": "TEST"}):
            summary = daily.run_daily(
                day=date(2026, 10, 8), root=Path(tmp), carriers=("geico", "travelers"),
                run_one=lambda name, **_kw: ran.append(name) or {"display": name, "status": "OK", "downloaded": 0, "held": []},
                close_tabs=lambda _url: [], open_tab=open_tab,
            )
            self.assertEqual(ran, ["geico"])
            self.assertEqual(summary["carriers"]["travelers"]["status"], "HELD")
            self.assertIn("Not retried", summary["carriers"]["travelers"]["reason"])
            self.assertEqual(__import__("os").environ[daily.KILL_SWITCH_ENV], "0")


class FaoBudgetAndLogTests(unittest.TestCase):
    def test_step_log_flushes_stderr_and_the_progress_file(self):
        import io
        import tempfile
        from pathlib import Path

        from robie_job_engine import progressive_pending_cancellation as fao

        buffer = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            fao._STEP_LOG = Path(tmp) / "fao-progress.log"
            try:
                with mock.patch.object(fao.sys, "stderr", buffer):
                    fao.step_log("policy 871490213 start")
                text = (Path(tmp) / "fao-progress.log").read_text(encoding="utf-8")
            finally:
                fao._STEP_LOG = None
        self.assertIn("policy 871490213 start", text)
        self.assertIn("policy 871490213 start", buffer.getvalue())

    def test_worker_deadline_interrupts_a_blocked_call(self):
        import time

        from robie_job_engine import progressive_pending_cancellation as fao

        with self.assertRaises(fao.FaoDeadline):
            with fao.time_budget(0.05, "worker deadline", fao.FaoDeadline):
                time.sleep(2)

    def test_page_budget_does_not_replace_the_worker_alarm(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        fired = []
        previous = fao.signal.signal(fao.signal.SIGALRM, lambda *_: fired.append("outer"))
        fao.signal.setitimer(fao.signal.ITIMER_REAL, 5)
        try:
            with fao.time_budget(0.01, "page"):
                pass
            self.assertEqual(fao.signal.getitimer(fao.signal.ITIMER_REAL)[0] > 0, True)
        finally:
            fao.signal.setitimer(fao.signal.ITIMER_REAL, 0)
            fao.signal.signal(fao.signal.SIGALRM, previous)

    def test_summary_timeout_opens_a_fresh_tab_and_the_next_policy_runs(self):
        import os
        import tempfile
        from datetime import date
        from pathlib import Path

        from robie_job_engine import progressive_pending_cancellation as fao
        from robie_job_engine.intake_core import SourceArchive

        rows = tuple(
            SimpleNamespace(policy_number=n, tab_label="", reason="NON-PAYMENT", insured_name="A",
                            cancel_date=None, list_url="u")
            for n in ("970498127", "871490213")
        )
        browser = mock.Mock()
        browser.screenshot_report.return_value = b"\x89PNG\r\n\x1a\n"
        browser.load_current_tab.side_effect = [rows, (), ()]
        browser.open_policy_summary.side_effect = type("TimeoutError", (Exception,), {})("summary")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"}), \
                mock.patch.object(fao, "_row_payload", side_effect=lambda row, **kw: {"policy": row.policy_number, **kw}), \
                mock.patch("robie_job_engine.document_retrieval_filing.require_carrier_pull"), \
                mock.patch.object(fao, "refuse_production_host"):
            receipt = fao.run_pull(
                browser, fao.FaoCancellationLedger(Path(tmp)), SourceArchive(Path(tmp) / "s"),
                as_of=date(2026, 10, 8),
            )
        self.assertEqual(browser.replace_stuck_tab.call_count, 2)
        browser.return_to_report.assert_not_called()
        self.assertEqual(receipt["status"], "PULLED")
        self.assertEqual(len(receipt["held"]), 2)

    def test_worker_deadline_is_partial_and_counts_unprocessed_policies(self):
        import os
        import tempfile
        from contextlib import nullcontext
        from datetime import date
        from pathlib import Path

        from robie_job_engine import progressive_pending_cancellation as fao
        from robie_job_engine.intake_core import SourceArchive

        rows = tuple(
            SimpleNamespace(policy_number=str(n), tab_label="", reason="NON-PAYMENT", insured_name="A",
                            cancel_date=None, list_url="u")
            for n in range(3)
        )
        browser = mock.Mock()
        browser.screenshot_report.return_value = b"\x89PNG\r\n\x1a\n"
        browser.load_current_tab.side_effect = [rows, (), ()]
        browser.open_policy_summary.side_effect = fao.FaoDeadline("deadline")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ROBIE_ENV": "TEST"}), \
                mock.patch.object(fao, "_row_payload", side_effect=lambda row, **kw: {"policy": row.policy_number, **kw}), \
                mock.patch("robie_job_engine.document_retrieval_filing.require_carrier_pull"), \
                mock.patch.object(fao, "refuse_production_host"), \
                mock.patch.object(fao, "time_budget", lambda *a, **k: nullcontext()):
            receipt = fao.run_pull(
                browser, fao.FaoCancellationLedger(Path(tmp)), SourceArchive(Path(tmp) / "s"),
                as_of=date(2026, 10, 8),
            )
        self.assertEqual(receipt["status"], "PARTIAL")
        self.assertNotEqual(receipt["status"], "PULLED")
        self.assertEqual(receipt["unprocessed"], 3)
        self.assertIn("3 policies left unprocessed", receipt["reason"])

    def test_policy_click_does_not_wait_for_the_redirect(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        seen = []

        class Link:
            def click(self, **kwargs):
                seen.append(kwargs)

        fao._click(Link(), no_wait_after=True)
        self.assertEqual(seen, [{"timeout": fao.CLICK_TIMEOUT_MS, "no_wait_after": True}])
        self.assertGreaterEqual(fao.POLICY_PAGE_WAIT_MS, 30000)

    def test_documents_control_matches_the_fao_link_text(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        node = mock.Mock()
        node.is_visible.return_value = True
        node.inner_text.return_value = "DOCUMENTS"
        empty = mock.Mock()
        empty.count.return_value = 0
        links = mock.Mock()
        links.count.return_value = 1
        links.nth.return_value = node
        page = mock.Mock()
        page.get_by_role.return_value = empty
        page.locator.side_effect = lambda sel: links if sel == fao.CL_DOCUMENTS_CSS else empty
        self.assertIs(fao.documents_control(page), node)
        self.assertNotIn("ext-element", fao.CL_DOCUMENTS_CSS)

    def test_cdp_reconnect_failure_is_partial_with_the_remaining_count(self):
        import os
        import tempfile
        from datetime import date
        from pathlib import Path

        from robie_job_engine import progressive_pending_cancellation as fao
        from robie_job_engine.intake_core import SourceArchive

        rows = tuple(
            SimpleNamespace(policy_number=n, tab_label="", reason="NON-PAYMENT", insured_name="A",
                            cancel_date=None, list_url="u")
            for n in ("970498127", "871490213")
        )

        class Browser:
            def screenshot_report(self):
                return b"\x89PNG\r\n\x1a\n"

            def load_report(self):
                return None

            def select_tab(self, _label):
                return None

            def load_current_tab(self, _label):
                if not hasattr(self, "_loaded"):
                    self._loaded = True
                    return rows
                return ()

            def open_policy_summary(self, _policy_number):
                raise TimeoutError("summary page timed out")

            def reconnect_after_timeout(self):
                raise fao.FaoReconnectFailed("cdp down")

            def finish_tabs(self):
                return None

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"ROBIE_ENV": "TEST", "ROBIE_BROWSER_CDP_URL": "http://127.0.0.1:9223"},
        ), mock.patch.object(fao, "_row_payload", side_effect=lambda row, **kw: {"policy": row.policy_number, **kw}), \
                mock.patch("robie_job_engine.document_retrieval_filing.require_carrier_pull"), \
                mock.patch.object(fao, "refuse_production_host"):
            receipt = fao.run_pull(
                Browser(), fao.FaoCancellationLedger(Path(tmp)), SourceArchive(Path(tmp) / "s"),
                as_of=date(2026, 10, 8),
            )
        self.assertEqual(receipt["status"], "PARTIAL")
        self.assertEqual(receipt["unprocessed"], 1)
        self.assertIn("connection is dead", receipt["reason"])
        self.assertIn("1 policies left unprocessed", receipt["reason"])


class UticaTabTests(unittest.TestCase):
    def test_extra_oneshield_sso_tabs_are_closed_and_one_is_reused(self):
        from robie_job_engine import utica_pending_cancellation as utica

        class Page:
            def __init__(self, url):
                self.url = url
                self.closed = False

            def close(self, **_kwargs):
                self.closed = True

        tabs = [
            Page("https://ufirstnow.uticafirst.com/oneshield/sso?osst=1"),
            Page("https://ufirstnow.uticafirst.com/oneshield/sso?osst=2"),
            Page("https://ufirstnow.uticafirst.com/oneshield/sso?osst=3"),
        ]
        context = SimpleNamespace(pages=tabs, new_page=mock.Mock())
        browser = SimpleNamespace(contexts=[context])
        with mock.patch("robie_job_engine.document_retrieval_filing.require_carrier_pull"), \
                mock.patch.object(utica, "refuse_production_host"), \
                mock.patch("robie_job_engine.utica_login.is_logged_in", side_effect=lambda page: page is tabs[0]):
            kept = utica.ensure_utica_page(browser)
        self.assertIs(kept, tabs[0])
        self.assertFalse(tabs[0].closed)
        self.assertTrue(tabs[1].closed and tabs[2].closed)
        context.new_page.assert_not_called()

    def test_worker_deadline_is_not_an_exception_playwright_can_swallow(self):
        from robie_job_engine import progressive_pending_cancellation as fao

        self.assertTrue(issubclass(fao.FaoDeadline, BaseException))
        self.assertFalse(issubclass(fao.FaoDeadline, Exception))
        self.assertFalse(issubclass(fao.FaoBudget, Exception))


class FarmersHomeTests(unittest.TestCase):
    def setUp(self):
        _reset_attempts()

    def test_signed_in_home_is_not_sent_to_the_login_form(self):
        from robie_job_engine import farmersofsalem_login as login

        class Hidden:
            def is_visible(self):
                return False

        class PasswordField:
            def count(self):
                return 2

            def nth(self, index):
                return Hidden()

        page = SimpleNamespace(
            url=login.AGENT_HOME_URL,
            body="Welcome back",
            gotos=[],
        )
        page.title = lambda: "Farmers Of Salem :: Agent Home"
        page.locator = lambda sel: PasswordField() if login.PASS_ID in sel else _Zero()
        page.get_by_role = lambda role, name=None: _Zero()
        page.goto = lambda url, **k: page.gotos.append(url)
        self.assertTrue(login.is_signed_in(page))
        other = SimpleNamespace(url="https://www.farmersofsalem.com/agent/book.aspx", body="", gotos=[])
        other.title = lambda: "Book"
        other.locator = lambda sel: _Zero()
        other.get_by_role = lambda role, name=None: _Zero()
        self.assertTrue(login.is_signed_in(other))
        login_page = SimpleNamespace(url=login.LOGIN_URL, body="Enter Your User Name", gotos=[])
        login_page.title = lambda: "Farmers Of Salem :: Agent Login"
        login_page.locator = lambda sel: _Zero()
        login_page.get_by_role = lambda role, name=None: _Zero()
        self.assertFalse(login.is_signed_in(login_page))
        finys = SimpleNamespace(url="https://fos.finys.com/")
        with mock.patch.object(login, "_require_test_host"), mock.patch.object(login, "_get_secret") as secret:
            returned = login.login_farmers(page, open_portal=lambda _page: finys)
        secret.assert_not_called()
        self.assertEqual(page.gotos, [])
        self.assertIs(returned, finys)

    def test_live_login_form_ignores_the_agent_search_inputs(self):
        """Three visible text inputs. Only ContentPlaceHolder1 is the login."""
        from robie_job_engine import farmersofsalem_login as login

        class Field:
            def __init__(self, field_id):
                self.field_id = field_id
                self.filled = None
                self.clicked = 0

            def is_visible(self):
                return True

            def fill(self, value):
                self.filled = value

            def click(self):
                self.clicked += 1

            def count(self):
                return 1

        fields = {
            login.USER_ID: Field(login.USER_ID),
            "ctl00_SearchAgentByNameUserControl1_txtName": Field("search-name"),
            "ctl00_SearchAgentByNameUserControl1_txtZip": Field("zip"),
            login.PASS_ID: Field(login.PASS_ID),
            login.SUBMIT_ID: Field(login.SUBMIT_ID),
            "ctl00_SearchAgentByNameUserControl1_btnSearch": Field("search-button"),
        }

        class One:
            def __init__(self, field):
                self.field = field
                self.first = field

            def count(self):
                return 1

            def nth(self, index):
                return self.field

            def fill(self, value):
                self.field.fill(value)

            def click(self):
                self.field.click()

            def is_visible(self):
                return True

        page = SimpleNamespace(
            url=login.LOGIN_URL,
            body="Enter Your User Name",
            clicks=0,
        )
        page.title = lambda: "Farmers Of Salem :: Agent Login"
        page.goto = lambda url, **k: None
        page.wait_for_timeout = lambda _ms: None
        page.get_by_role = lambda role, name=None: _Zero()

        def locator(sel):
            if sel == "body":
                return SimpleNamespace(inner_text=lambda: page.body)
            for field_id, field in fields.items():
                if field_id in sel and "ContentPlaceHolder1" in sel:
                    return One(field)
            return _Zero()

        page.locator = locator
        with mock.patch.object(login, "_require_test_host"):
            with self.assertRaises(IntakeHold):
                login.login_farmers(page, credentials=lambda: ("user", "pw-value"), open_portal=lambda p: p)
        self.assertEqual(fields[login.USER_ID].filled, "user")
        self.assertEqual(fields[login.PASS_ID].filled, "pw-value")
        self.assertEqual(login._USER_SELECTORS[0], f"#{login.USER_ID}")
        self.assertIn("ctl00$ContentPlaceHolder1$txtName", login._USER_SELECTORS[1])
        self.assertIn("ctl00$ContentPlaceHolder1$txtPass", login._PASS_SELECTORS[1])
        self.assertEqual(login._SUBMIT_SELECTORS[0], f"#{login.SUBMIT_ID}")
        self.assertEqual(fields[login.SUBMIT_ID].clicked, 1)
        self.assertIsNone(fields["ctl00_SearchAgentByNameUserControl1_txtName"].filled)
        self.assertIsNone(fields["ctl00_SearchAgentByNameUserControl1_txtZip"].filled)
        self.assertEqual(fields["ctl00_SearchAgentByNameUserControl1_btnSearch"].clicked, 0)


class TabHygieneTests(unittest.TestCase):
    def test_new_tabs_close_and_the_kept_page_stays(self):
        from robie_job_engine.carrier_tabs import close_new_pages, snapshot_ids

        class Page:
            def __init__(self, url):
                self.url = url
                self.closed = False
                self.context = None

            def close(self):
                self.closed = True

        context = SimpleNamespace(pages=[])
        first = Page("https://ufirstnow.uticafirst.com/")
        first.context = context
        context.pages.append(first)
        before = snapshot_ids(first)
        second = Page("https://ufirstnow.uticafirst.com/extra")
        second.context = context
        context.pages.append(second)
        closed = close_new_pages(first, before, keep=first)
        self.assertEqual(closed, 1)
        self.assertTrue(second.closed)
        self.assertFalse(first.closed)

    def test_bop_pull_closes_the_application_tab(self):
        from robie_job_engine import progressive_bop as bop

        class Page:
            def __init__(self, url):
                self.url = url
                self.closed = False

            def close(self):
                self.closed = True

        shell = Page("https://www.foragentsonly.com/landingpages/managepolicies/")
        bop_tab = Page("https://bop.americanstrategic.com/app")
        context = SimpleNamespace(pages=[shell, bop_tab])
        shell.context = context
        bop_tab.context = context
        browser = SimpleNamespace(shell=shell, report_page=bop_tab)
        portal = bop.BopPendingCancelPortal.__new__(bop.BopPendingCancelPortal)
        portal.browser = browser
        portal._pages_before = {id(shell)}
        portal.close_opened_tabs()
        self.assertTrue(bop_tab.closed)
        self.assertFalse(shell.closed)


if __name__ == "__main__":
    unittest.main()
