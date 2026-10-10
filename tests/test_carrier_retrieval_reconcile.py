"""Tests for the 2026-10-08 carrier retrieval reconcile (Ralph's
fix-utica-auto-login branch + hermes-test-01 hand patches + handoff bugs).

Fixture-level only: no portal, no browser, no network.
"""
import os
import re
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine import carrier_dry_run, utica_pending_cancellation as utica
from robie_job_engine.carrier_locators import ci_label, unique_control_ci, wait_for_text_ci
from robie_job_engine.intake_core import IntakeHold
from robie_job_engine.progressive_pending_cancellation import require_policy_number as progressive_policy

TEST_ENV = {"ROBIE_ENV": "TEST"}


# --- DRAGON_TRANSACTION_ID ---------------------------------------------------


class TransactionIdTests(unittest.TestCase):
    def test_live_extjs_name_value_pair(self):
        html = 'cfg = [["USER_SESSION_GUID","value":"abc"],"DRAGON_TRANSACTION_ID","value":"1247842754"];'
        self.assertEqual(utica.transaction_id_from_page(html), "1247842754")

    def test_name_value_pair_with_spaces_and_unquoted_number(self):
        html = "'DRAGON_TRANSACTION_ID' , 'value' : 1247842754"
        self.assertEqual(utica.transaction_id_from_page(html), "1247842754")

    def test_json_object_form(self):
        html = '{"name":"DRAGON_TRANSACTION_ID","xtype":"hidden","value":"1247842754"}'
        self.assertEqual(utica.transaction_id_from_page(html), "1247842754")

    def test_hidden_input_form(self):
        html = '<input type="hidden" name="DRAGON_TRANSACTION_ID" id="x" value="1247842754">'
        self.assertEqual(utica.transaction_id_from_page(html), "1247842754")

    def test_legacy_assignment_forms_still_work(self):
        for html in (
            "DRAGON_TRANSACTION_ID=1246149193&x=1",
            '"DRAGON_TRANSACTION_ID": "1246149193"',
            "var DRAGON_TRANSACTION_ID = '1246149193';",
        ):
            with self.subTest(html=html):
                self.assertEqual(utica.transaction_id_from_page(html), "1246149193")

    def test_same_value_repeated_is_fine(self):
        html = '"DRAGON_TRANSACTION_ID","value":"1247842754" ... DRAGON_TRANSACTION_ID=1247842754'
        self.assertEqual(utica.transaction_id_from_page(html), "1247842754")

    def test_two_different_values_hold(self):
        html = '"DRAGON_TRANSACTION_ID","value":"1247842754" "DRAGON_TRANSACTION_ID","value":"999"'
        with self.assertRaises(IntakeHold):
            utica.transaction_id_from_page(html)

    def test_missing_holds(self):
        for html in ("", "<html>no id</html>", '"DRAGON_TRANSACTION_ID","value":""'):
            with self.subTest(html=html):
                with self.assertRaises(IntakeHold):
                    utica.transaction_id_from_page(html)


# --- case-insensitive locators ----------------------------------------------


class _Locator:
    def __init__(self, nodes):
        self.nodes = list(nodes)
        self.clicks = 0
        self.checks = 0

    def count(self):
        return len(self.nodes)

    @property
    def first(self):
        return _Locator(self.nodes[:1])

    def nth(self, index):
        return _Locator([self.nodes[index]])

    def is_visible(self):
        return bool(self.nodes) and self.nodes[0].get("visible", True)

    def click(self):
        for node in self.nodes[:1]:
            node["clicked"] = node.get("clicked", 0) + 1

    def check(self):
        for node in self.nodes[:1]:
            node["checked"] = True

    def wait_for(self, state=None, timeout=None):
        if not any(node.get("visible", True) for node in self.nodes):
            raise TimeoutError("not visible")

    def get_by_role(self, role, name=None, exact=False):
        return _match(self.nodes_children(), role, name, exact)

    def nodes_children(self):
        out = []
        for node in self.nodes:
            out.extend(node.get("children", []))
        return out


def _name_matches(label, name, exact):
    if isinstance(name, re.Pattern):
        return bool(name.search(label))
    if exact:
        return label == name
    return name.casefold() in label.casefold()


def _match(nodes, role, name, exact):
    return _Locator([n for n in nodes if n.get("role") == role and _name_matches(n["label"], name, exact)])


class FakePortal:
    """Title Case UFirst Now-like DOM: roles, visible text, a doc list heading."""

    def __init__(self, nodes, *, url="https://ufirstnow.uticafirst.com/oneshield/sso?osst=TOKEN"):
        self.nodes = nodes
        self.url = url
        self.waited = []
        self.html = ""

    def get_by_role(self, role, name=None, exact=False):
        return _match(self.nodes, role, name, exact)

    def get_by_text(self, text, exact=False):
        return _Locator([
            n for n in self.nodes
            if n.get("text_visible", True) and _name_matches(n["label"], text, exact)
        ])

    def locator(self, selector, has_text=None):
        if selector == "tr":
            return _Locator([n for n in self.nodes if n.get("role") == "row" and has_text in n["label"]])
        return _Locator([])

    def wait_for_selector(self, selector, timeout=None):
        self.waited.append(selector)

    def wait_for_timeout(self, ms):
        self.waited.append(ms)

    def content(self):
        return self.html


class LocatorHelperTests(unittest.TestCase):
    def test_ci_label_is_full_match_and_whitespace_tolerant(self):
        pattern = ci_label("Policy Transactions")
        self.assertTrue(pattern.search("POLICY   TRANSACTIONS"))
        self.assertTrue(pattern.search(" policy transactions "))
        self.assertFalse(pattern.search("Policy Transactions Archive"))

    def test_exact_label_wins_first(self):
        page = FakePortal([{"role": "button", "label": "FILTER LIST"}])
        unique_control_ci(page, "button", "FILTER LIST", carrier="X").click()
        self.assertEqual(page.nodes[0]["clicked"], 1)

    def test_title_case_portal_matches_upper_case_label(self):
        page = FakePortal([{"role": "tab", "label": "Documents"}])
        unique_control_ci(page, "tab", "DOCUMENTS", carrier="X").click()
        self.assertEqual(page.nodes[0]["clicked"], 1)

    def test_two_case_variants_are_ambiguous(self):
        page = FakePortal([{"role": "button", "label": "Filter List"}, {"role": "button", "label": "FILTER list"}])
        with self.assertRaises(IntakeHold):
            unique_control_ci(page, "button", "FILTER LIST ", carrier="X")

    def test_text_fallback_only_when_asked(self):
        page = FakePortal([{"role": "generic", "label": "Policy Transactions"}])
        with self.assertRaises(IntakeHold):
            unique_control_ci(page, "tab", "POLICY TRANSACTIONS", carrier="X")
        unique_control_ci(page, "tab", "POLICY TRANSACTIONS", carrier="X", text_fallback=True).click()
        self.assertEqual(page.nodes[0]["clicked"], 1)

    def test_text_fallback_several_visible_needs_first_visible_flag(self):
        nodes = [
            {"role": "generic", "label": "Policy Transactions", "visible": False},
            {"role": "generic", "label": "Policy Transactions"},
            {"role": "generic", "label": "policy transactions"},
        ]
        page = FakePortal(nodes)
        with self.assertRaises(IntakeHold):
            unique_control_ci(page, "tab", "Policy Transactions", carrier="X", text_fallback=True)
        unique_control_ci(
            page, "tab", "Policy Transactions", carrier="X", text_fallback=True, first_visible_text=True
        ).click()
        self.assertEqual(nodes[1].get("clicked"), 1)
        self.assertIsNone(nodes[0].get("clicked"))

    def test_wait_for_text_ci_never_raises(self):
        page = FakePortal([{"role": "heading", "label": "Policy | Transaction List"}])
        self.assertTrue(wait_for_text_ci(page, "TRANSACTION LIST", timeout_ms=10))
        self.assertFalse(wait_for_text_ci(page, "Document List", timeout_ms=10))


class UticaNavigationTests(unittest.TestCase):
    def _browser(self, nodes):
        page = FakePortal(nodes)
        return utica.PlaywrightUticaCancellationBrowser(page), page

    def test_title_case_transactions_tab_and_filter_list(self):
        nodes = [
            {"role": "generic", "label": "Policy Transactions"},
            {"role": "heading", "label": "Policy | Transaction List"},
            {"role": "radio", "label": "All"},
            {"role": "button", "label": "Filter List"},
        ]
        browser, page = self._browser(nodes)
        browser.open_transactions()
        browser.select_filter_all()
        self.assertEqual(nodes[0]["clicked"], 1)
        self.assertTrue(nodes[2]["checked"])
        self.assertEqual(nodes[3]["clicked"], 1)
        self.assertNotIn(3000, page.waited)  # heading found, no settle wait

    def test_upper_case_portal_still_works(self):
        nodes = [
            {"role": "tab", "label": "POLICY TRANSACTIONS"},
            {"role": "heading", "label": "POLICY | TRANSACTION LIST"},
            {"role": "radio", "label": "All"},
            {"role": "button", "label": "FILTER LIST"},
        ]
        browser, _ = self._browser(nodes)
        browser.open_transactions()
        browser.select_filter_all()
        self.assertEqual(nodes[0]["clicked"], 1)
        self.assertEqual(nodes[3]["clicked"], 1)

    def test_missing_filter_button_holds(self):
        browser, _ = self._browser([{"role": "radio", "label": "All"}])
        with self.assertRaises(IntakeHold):
            browser.select_filter_all()

    def test_open_documents_any_case_reads_live_transaction_id(self):
        link = {"role": "link", "label": "DOCUMENTS"}
        row = {"role": "row", "label": "ART3000335720 Pending Cancellation(NOC)", "children": [link]}
        browser, page = self._browser([row])
        page.html = '"DRAGON_TRANSACTION_ID","value":"1247842754"'
        txn = utica.TransactionRow("ART3000335720", "Pending Cancellation(NOC)", "Zangara", date(2026, 10, 10), page.url)
        browser.open_documents(txn)
        self.assertEqual(link["clicked"], 1)
        self.assertEqual(browser._transaction_id, "1247842754")
        self.assertIn(3000, page.waited)  # heading unknown -> settle, then the id check

    def test_extjs_many_tables_hold_names_the_follow_up(self):
        class ManyTables(FakePortal):
            def locator(self, selector, has_text=None):
                return _Locator([{}] * 29) if selector == "table" else super().locator(selector, has_text)

        browser = utica.PlaywrightUticaCancellationBrowser(ManyTables([]))
        with self.assertRaises(IntakeHold) as ctx:
            browser.load_transactions()
        self.assertIn("found 29 tables", str(ctx.exception))


# --- ensure_utica_page -------------------------------------------------------


class _Tab:
    def __init__(self, url, body="", ctx=None):
        self.url = url
        self.body = body
        self.closed = False

    def locator(self, selector):
        return SimpleNamespace(inner_text=lambda: self.body)

    def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, pages):
        self.pages = list(pages)
        self.created = []

    def new_page(self):
        tab = _Tab("about:blank")
        self.created.append(tab)
        self.pages.append(tab)
        return tab


SIGNED_IN = "https://ufirstnow.uticafirst.com/oneshield/sso?osst=T"


class EnsureUticaPageTests(unittest.TestCase):
    def _run(self, pages, host="hermes-test-01", login=None):
        ctx = _Ctx(pages)
        self.ctx = ctx
        browser = SimpleNamespace(contexts=[ctx])
        calls = []

        def fake_login(page):
            calls.append(page)
            if login:
                login(page)

        with patch.dict(os.environ, TEST_ENV), \
                patch("socket.gethostname", return_value=host), \
                patch("socket.getfqdn", return_value=host), \
                patch("robie_job_engine.utica_login.login_utica", fake_login):
            return utica.ensure_utica_page(browser), ctx, calls

    def test_reuses_the_one_signed_in_tab_and_ignores_expired(self):
        good = _Tab(SIGNED_IN, "Welcome Carlo Ferrara")
        expired = _Tab("https://ufirstnow.uticafirst.com/sso/login", "Welcome back, sign in")
        page, ctx, calls = self._run([expired, good])
        self.assertIs(page, good)
        self.assertEqual(calls, [])
        self.assertEqual(ctx.created, [])

    def test_two_signed_in_tabs_reuse_one_and_close_the_extra(self):
        first, second = _Tab(SIGNED_IN, "Welcome"), _Tab(SIGNED_IN, "Welcome")
        page, ctx, calls = self._run([first, second])
        self.assertIs(page, first)
        self.assertTrue(second.closed)
        self.assertFalse(first.closed)
        self.assertEqual(calls, [])
        self.assertEqual(ctx.created, [])

    def test_no_signed_in_tab_logs_in_on_test_host(self):
        page, ctx, calls = self._run([_Tab("https://example.com")])
        self.assertEqual(len(ctx.created), 1)
        self.assertEqual(calls, [ctx.created[0]])
        self.assertIs(page, ctx.created[0])

    def test_failed_login_closes_the_new_tab(self):
        def boom(page):
            raise IntakeHold("Utica login failed - not on UFirst Now portal after auth")

        with self.assertRaises(IntakeHold):
            self._run([], login=boom)
        self.assertEqual(len(self.ctx.created), 1)
        self.assertTrue(self.ctx.created[0].closed)

    def test_other_host_never_reads_credentials(self):
        with self.assertRaises(IntakeHold):
            self._run([], host="some-laptop")
        self.assertEqual(self.ctx.created, [])

    def test_production_host_holds_before_login(self):
        with patch.dict(os.environ, {"ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX": "0"}):
            with self.assertRaises(IntakeHold):
                self._run([], host="hermes-poc-01")

    def test_dry_run_spec_uses_ensure_utica_page(self):
        spec = carrier_dry_run.SPECS["uticafirst"]
        self.assertEqual(spec.select_fn_name, "ensure_utica_page")
        self.assertEqual(spec.select_takes, "browser")


# --- hold reasons in the dry run --------------------------------------------


class HoldReasonTests(unittest.TestCase):
    def test_hold_reason_prefers_hold_reason_over_carrier_reason(self):
        guard_row = {"policy_number": "PRAU716089", "reason": "Non-payment", "hold_reason": "doc ambiguous"}
        self.assertEqual(carrier_dry_run.hold_reason(guard_row), "doc ambiguous")
        self.assertEqual(carrier_dry_run.hold_reason({"reason": "Medium, not High"}), "Medium, not High")
        self.assertEqual(carrier_dry_run.hold_reason({}), "held (no reason recorded)")

    def _summary(self, behaviors):
        import tempfile
        from pathlib import Path
        from test_carrier_dry_run import _run_with_patches

        with tempfile.TemporaryDirectory() as tmp:
            return _run_with_patches(Path(tmp), behaviors, carriers="geico,guard")

    def test_ok_run_with_held_item_prints_why(self):
        geico = {
            "status": "PULLED", "count": 0, "downloaded": [], "skipped_already_delivered": [],
            "held": [{"policy_number": "6000000001", "insured_name": "Low Row",
                      "reason": "Geico lists this alert as Medium, not High; only High pending cancellations are pulled"}],
        }
        summary = self._summary({"geico": geico})
        result = summary["carriers"]["geico"]
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(result["hold_reasons"]), 1)
        text = carrier_dry_run.render_summary(summary)
        self.assertIn("1 held", text)
        self.assertIn("held 6000000001 Low Row: Geico lists this alert as Medium", text)

    def test_held_run_keeps_run_reason_and_item_reasons(self):
        exc = IntakeHold("Guard pull held")
        exc.details = {"held": [{"policy_number": "PRAU716089", "reason": "Non-payment",
                                 "hold_reason": "cancellation document is missing or ambiguous (found 2)"}]}
        summary = self._summary({"guard": exc})
        result = summary["carriers"]["guard"]
        self.assertEqual(result["status"], "HELD")
        self.assertEqual(result["reason"], "Guard pull held")
        text = carrier_dry_run.render_summary(summary)
        self.assertIn("Guard: HELD — Guard pull held", text)
        self.assertIn("PRAU716089: cancellation document is missing or ambiguous (found 2)", text)
        self.assertNotIn("Non-payment", text)


# --- Progressive NJA policy numbers (Ralph 792a4c33) -------------------------


class ProgressivePolicyNumberTests(unittest.TestCase):
    def test_nja_prefixed_and_numeric(self):
        self.assertEqual(progressive_policy("NJA129565"), "NJA129565")
        self.assertEqual(progressive_policy("nja 129565"), "NJA129565")
        self.assertEqual(progressive_policy("970498127"), "970498127")
        self.assertEqual(progressive_policy("970498127-2"), "970498127-2")

    def test_bad_numbers_hold(self):
        for bad in ("N129565", "ABCD129565", "12345", "NJA12345", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(IntakeHold):
                    progressive_policy(bad)


if __name__ == "__main__":
    unittest.main()
