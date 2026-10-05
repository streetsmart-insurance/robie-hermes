"""Fixture tests for the Travelers Direct Bill Activity pull. No live login."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine.travelers_pending_cancellation import (
    ActivityDate,
    DetailRow,
    ListRow,
    PlaywrightTravelersBrowser,
    PullHeld,
    TravelersDeliveryLedger,
    classify_section,
    parse_activity_dates,
    parse_carrier_date,
    parse_detail_table,
    parse_list_rows,
    parse_save_form,
    refuse_production_host,
    report_document_id,
    report_filename,
    require_agent_code,
    require_policy_number,
    row_document_id,
    run_pull,
    _SECTION_CANCELLATION,
    _SECTION_PRE_CANCELLATION,
    _SECTION_REINSTATEMENT,
)
from robie_job_engine.intake_core import IntakeHold, SourceArchive


LIST_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8cfc00000000300010005fe02fedccc59e700000000"
    "49454e44ae426082"
)
AS_OF = date(2026, 10, 5)
LIST_URL = "https://foragents.travelers.com/Business/billingandpolicyservices/directbillactivity"
DETAIL_URL = "https://foragents.travelers.com/Business/BillingAndPolicyServices/DirectBillActivityDetails"

# select_date options: (value, display text).
DATE_OPTIONS = (
    ("10/05/2026", "10/05/2026"),
    ("10/02/2026", "10/02/2026"),
    ("09/30/2026", "09/30/2026"),
    ("09/22/2026", "09/22/2026"),
    ("06/29/2026", "06/29/2026"),  # outside the 14-day window
)

# Pre-cancellation Alert section fixture (mapped 2026-10-05).
PRE_HEADERS = (
    "Account Number", "Policy Number", "Account Name",
    "DNOC Minimum Due", "Partial Payment", "Account Balance",
    "Cancellation Notice Date",
)
PRE_ROWS = (
    ("A1001", "UB-B326957A", "D SERVICE LANDSCAPING LLC", "$864.84", "$0.00", "$864.84", "10/12/2026"),
    ("A1002", "UB-0S764907", "CRYSTAL WOOD FLOORS LLC", "$5,938.00", "$100.00", "$5,838.00", "10/12/2026"),
)
PRE_TITLE = "Pre-cancellation Alert"
PRE_INTRO = (
    "Pre-cancellation Alert A cancellation notice will be sent for the following "
    "policies on the date shown below. Total due amounts exclude installment charge."
)

# Cancellation Alert section fixture (column order differs: Account Name before Policy Number).
CAN_HEADERS = (
    "Account Number", "Account Name", "Policy Number",
    "DNOC Minimum Due", "Partial Payment", "Account Balance", "Cancellation Date",
)
CAN_ROWS = (
    ("A2001", "EMPOWER GROUP, LLC", "UB-C8291492", "$556.16", "$0.00", "$556.16", "10/20/2026"),
)
CAN_TITLE = "Cancellation Alert"
CAN_INTRO = (
    "Cancellation Alert The following policies will cancel on the date shown "
    "below if sufficient payment is not received:"
)

# Reinstatement section fixture (no bold title).
REIN_HEADERS = ("Account Number", "Account Name", "Policy Number", "Agent Activity Date")
REIN_ROWS = (
    ("A3001", "SMITH BROS", "UB-R1111111", "09/25/2026"),
)
REIN_TITLE = ""
REIN_INTRO = (
    "The following policies appeared previously as pre-cancellation policy. "
    "Since that date subsequent activity has taken place to clear the cancellation. "
    "Please note there may be other policies on the above accounts."
)

SAVE_FORM_FIELDS = {
    "agentCode": "0X4688",
    "date": "10/5/2026",
    "__RequestVerificationToken": "token-abc-123",
}


def word_doc_bytes(mark: bytes) -> bytes:
    return b"PK\x03\x04" + mark + b"\x00" * 64


# ---------------------------------------------------------------------------
# Fake Playwright surface
# ---------------------------------------------------------------------------

class FakeNode:
    def __init__(self, tag="", role="", text="", children=(), attrs=None, on_click=None):
        self.tag = tag
        self.role = role
        self.text = text
        self.children = list(children)
        self.attrs = dict(attrs or {})
        self._on_click = on_click

    def matches_role(self, role, name, exact):
        if self.role != role:
            return False
        if name is None:
            return True
        return self.text == name if exact else name.lower() in self.text.lower()

    def find_role(self, role, name, exact):
        found = [self] if self.matches_role(role, name, exact) else []
        for child in self.children:
            found.extend(child.find_role(role, name, exact))
        return found

    def select(self, parts):
        if not parts:
            return [self]
        found = []
        if self.tag == parts[0]:
            if len(parts) == 1:
                found.append(self)
            else:
                for child in self.children:
                    found.extend(child.select(parts[1:]))
        for child in self.children:
            found.extend(child.select(parts))
        seen = []
        for node in found:
            if not any(node is other for other in seen):
                seen.append(node)
        return seen

    def inner_text(self):
        parts = [self.text]
        for child in self.children:
            parts.append(child.inner_text())
        return " ".join(p for p in parts if p)

    def get_attribute(self, name):
        return self.attrs.get(name)


class FakeLocator:
    def __init__(self, page, nodes):
        self._page = page
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    @property
    def first(self):
        return FakeLocator(self._page, self.nodes[:1])

    def all(self):
        return [FakeLocator(self._page, [node]) for node in self.nodes]

    def nth(self, index):
        return FakeLocator(self._page, [self.nodes[index]])

    def click(self):
        if len(self.nodes) != 1:
            raise AssertionError("click requires exactly one node")
        node = self.nodes[0]
        self._page.clicks.append(node.text or node.tag)
        if node._on_click is not None:
            node._on_click()

    def inner_text(self):
        if len(self.nodes) != 1:
            raise AssertionError("inner_text requires exactly one node")
        return self.nodes[0].inner_text()

    def get_attribute(self, name):
        if len(self.nodes) != 1:
            raise AssertionError("get_attribute requires exactly one node")
        return self.nodes[0].get_attribute(name)

    def select_option(self, value):
        if len(self.nodes) != 1:
            raise AssertionError("select_option requires exactly one node")
        self._page.selected_date = value
        self._page.clicks.append(f"select:{value}")

    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self.nodes:
            found.extend(node.find_role(role, name, exact))
        return FakeLocator(self._page, found)

    def locator(self, selector):
        # Supports: tag, tag#id, tag.class, tag[attr="v"], tag[attr*="v"], and
        # simple descendant chains like "div#x table".
        return FakeLocator(self._page, _css_select(self.nodes, selector))


def _css_select(nodes, selector):
    """Minimal CSS matcher for the fixture DOM."""
    results = []
    for part in selector.split():
        step = []
        for node in (results or list(nodes)):
            step.extend(_match_step(node, part))
        # Also search descendants of the original roots on the first part.
        if not results:
            for node in nodes:
                step.extend(_descendants_matching(node, part))
        results = step
    # Deduplicate.
    seen = []
    for node in results:
        if not any(node is other for other in seen):
            seen.append(node)
    return seen


def _match_step(node, part):
    """Match a single selector part against a node and its descendants."""
    matched = []
    for candidate in [node] + _all_descendants(node):
        if _matches_part(candidate, part):
            matched.append(candidate)
    return matched


def _descendants_matching(node, part):
    return [d for d in _all_descendants(node) if _matches_part(d, part)]


def _all_descendants(node):
    out = []
    for child in node.children:
        out.append(child)
        out.extend(_all_descendants(child))
    return out


def _matches_part(node, part):
    tag = None
    rest = part
    # tag
    m = __import__("re").match(r"^([a-zA-Z][a-zA-Z0-9]*)", rest)
    if m:
        tag = m.group(1).lower()
        rest = rest[m.end():]
    if tag and node.tag.lower() != tag:
        return False
    # #id
    m = __import__("re").search(r"#([A-Za-z0-9_-]+)", rest)
    if m and node.attrs.get("id") != m.group(1):
        return False
    # .class
    for cls in __import__("re").findall(r"\.([A-Za-z0-9_-]+)", rest):
        classes = (node.attrs.get("class") or "").split()
        if cls not in classes:
            return False
    # [attr="v"] and [attr*="v"]
    for attr, op, val in __import__("re").findall(r'\[([A-Za-z0-9_-]+)(\*?=)"([^"]*)"\]', rest):
        actual = node.get_attribute(attr) or ""
        if op == "=" and actual != val:
            return False
        if op == "*=" and val not in actual:
            return False
    return True


class FakePopup:
    def __init__(self, page):
        self._page = page

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @property
    def value(self):
        return self._page.detail_page


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def body(self):
        return self._body


class FakeRequest:
    def __init__(self, page):
        self._page = page

    def post(self, url, form=None, timeout=None):
        self._page.posted_forms.append((url, dict(form or {})))
        if self._page.post_returns_non_word:
            return FakeResponse(b"<html>not a document</html>")
        return FakeResponse(word_doc_bytes(b"travelers-report"))


class FakeContext:
    def __init__(self, page):
        self.request = FakeRequest(page)


class FakeDetailPage:
    """The new-tab Direct Bill Activity Details page."""

    def __init__(self, parent, *, section_title, section_intro, headers, rows,
                 save_fields=SAVE_FORM_FIELDS, missing=()):
        self._parent = parent
        self.url = DETAIL_URL
        self.closed = False
        self.missing = set(missing)
        bold = [FakeNode(tag="b", text=section_title)] if section_title else []
        header_cells = [FakeNode(tag="th", text=h) for h in headers]
        body_rows = []
        for cells in rows:
            body_rows.append(FakeNode(
                tag="tr",
                children=[FakeNode(tag="td", text=c) for c in cells],
            ))
        table = FakeNode(tag="table", children=[
            FakeNode(tag="thead", children=[FakeNode(tag="tr", children=header_cells)]),
            FakeNode(tag="tbody", children=body_rows),
        ])
        form_children = []
        for name, value in save_fields.items():
            if name in self.missing:
                continue
            form_children.append(FakeNode(tag="input", attrs={"name": name, "value": value}))
        form = FakeNode(
            tag="form",
            attrs={"action": "/Business/BillingAndPolicyServices/SaveReportToWordDocument"},
            children=form_children,
        )
        self._section = FakeNode(
            tag="div", attrs={"id": "billingActivityReportDetail"},
            text=section_intro, children=bold + [table, form],
        )
        self.context = FakeContext(parent)

    def _roots(self):
        return [self._section]

    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self._roots():
            found.extend(node.find_role(role, name, exact))
        return FakeLocator(self, found)

    def locator(self, selector):
        return FakeLocator(self, _css_select(self._roots(), selector))

    def wait_for_selector(self, selector, timeout=None):
        if selector.startswith("text="):
            needle = selector[len("text="):].strip().lower()
            found = any(
                needle in node.inner_text().lower()
                for node in self._roots()
            )
            if not found:
                raise TimeoutError(f"missing {selector}")
            return
        if not self.locator(selector).count():
            raise TimeoutError(f"missing {selector}")

    def close(self):
        self.closed = True


class MockTravelersPage:
    """Fake foragents.travelers.com list page driving the real page object."""

    def __init__(self, *, date_options=DATE_OPTIONS, missing=(),
                 list_rows=(("0X4688", "2026-10-05T00:00:00"),),
                 detail=None, post_returns_non_word=False):
        self.url = LIST_URL
        self.date_options = list(date_options)
        self.missing = set(missing)
        self.list_rows = list(list_rows)
        self.detail_page = detail
        self.post_returns_non_word = post_returns_non_word
        self.clicks: list[str] = []
        self.selected_date: str | None = None
        self.posted_forms: list[tuple[str, dict]] = []
        self._searched = False

    # -- DOM ------------------------------------------------------------
    def _roots(self):
        nodes = []
        if "agency_reports" not in self.missing:
            nodes.append(FakeNode(tag="button", role="tab", text="Agency Reports"))
        if "report_link" not in self.missing:
            nodes.append(FakeNode(
                tag="a", role="link",
                text="Direct Bill Activity Cancellation & Reinstatement Notices",
            ))
        if "select_date" not in self.missing:
            options = [
                FakeNode(tag="option", text=text, attrs={"value": value})
                for value, text in self.date_options
            ]
            nodes.append(FakeNode(tag="select", attrs={"id": "select_date"}, children=options))
        if "search" not in self.missing:
            nodes.append(FakeNode(
                tag="button", role="button", text="Search",
                on_click=self._do_search,
            ))
        if self._searched and "view_link" not in self.missing:
            for agent_code, stamp in self.list_rows:
                nodes.append(FakeNode(
                    tag="a",
                    attrs={
                        "class": "gridActionLink",
                        "href": "javascript:void(0);",
                        "data-agentcode": agent_code,
                        "data-datereceived": stamp,
                    },
                    text="view",
                    on_click=self._open_detail,
                ))
            nodes.append(FakeNode(tag="div", text="Showing 1 results"))
        return nodes

    def _do_search(self):
        self._searched = True

    def _open_detail(self):
        # The click target is resolved by the page object via expect_popup.
        pass

    # -- Playwright surface ----------------------------------------------
    def get_by_role(self, role, name=None, exact=True):
        found = []
        for node in self._roots():
            found.extend(node.find_role(role, name, exact))
        return FakeLocator(self, found)

    def locator(self, selector):
        return FakeLocator(self, _css_select(self._roots(), selector))

    def wait_for_selector(self, selector, timeout=None):
        if selector.startswith("text="):
            needle = selector[len("text="):].strip().lower()
            found = any(
                needle in node.inner_text().lower()
                for node in self._roots()
            )
            if not found:
                raise TimeoutError(f"missing {selector}")
            return
        if not self.locator(selector).count():
            raise TimeoutError(f"missing {selector}")

    def expect_popup(self):
        return FakePopup(self)

    def goto(self, url, wait_until=None):
        self.url = url

    def screenshot(self, full_page=True, type="png"):
        return LIST_PNG


# ---------------------------------------------------------------------------
# Parse tests
# ---------------------------------------------------------------------------

class SectionClassificationTests(unittest.TestCase):
    def test_pre_cancellation(self):
        self.assertEqual(
            classify_section(PRE_TITLE, PRE_INTRO), _SECTION_PRE_CANCELLATION)

    def test_cancellation(self):
        self.assertEqual(
            classify_section(CAN_TITLE, CAN_INTRO), _SECTION_CANCELLATION)

    def test_reinstatement_by_intro(self):
        self.assertEqual(
            classify_section(REIN_TITLE, REIN_INTRO), _SECTION_REINSTATEMENT)

    def test_ambiguous_section_holds(self):
        with self.assertRaises(IntakeHold):
            classify_section("Monthly Summary", "Some unrelated report text.")


class ActivityDateTests(unittest.TestCase):
    def test_recent_window_filters_old_dates(self):
        dates = parse_activity_dates(DATE_OPTIONS, as_of=AS_OF)
        self.assertEqual(
            [d.activity_date for d in dates],
            [date(2026, 10, 5), date(2026, 10, 2), date(2026, 9, 30), date(2026, 9, 22)],
        )
        # 06/29/2026 is outside the 14-day window.
        self.assertNotIn(date(2026, 6, 29), [d.activity_date for d in dates])

    def test_newest_first(self):
        dates = parse_activity_dates(DATE_OPTIONS, as_of=AS_OF)
        self.assertTrue(
            all(a.activity_date >= b.activity_date for a, b in zip(dates, dates[1:])))

    def test_empty_options_hold(self):
        with self.assertRaises(IntakeHold):
            parse_activity_dates((), as_of=AS_OF)

    def test_all_outside_window_holds(self):
        old = (("06/29/2026", "06/29/2026"),)
        with self.assertRaises(IntakeHold):
            parse_activity_dates(old, as_of=AS_OF)

    def test_bad_window_holds(self):
        with self.assertRaises(IntakeHold):
            parse_activity_dates(DATE_OPTIONS, as_of=AS_OF, window_days=0)


class ListRowTests(unittest.TestCase):
    def test_valid_rows(self):
        rows = parse_list_rows((("0X4688", "2026-10-05T00:00:00"),
                                ("0725HN", "2026-10-02T00:00:00")))
        self.assertEqual(rows[0], ListRow("0X4688", "2026-10-05T00:00:00"))
        self.assertEqual(rows[1].agent_code, "0725HN")

    def test_bad_agent_code_holds(self):
        with self.assertRaises(IntakeHold):
            parse_list_rows((("!!!", "2026-10-05T00:00:00"),))

    def test_bad_timestamp_holds(self):
        with self.assertRaises(IntakeHold):
            parse_list_rows((("0X4688", "yesterday"),))

    def test_empty_rows_hold(self):
        with self.assertRaises(IntakeHold):
            parse_list_rows(())


class DetailTableTests(unittest.TestCase):
    def test_pre_cancellation_layout(self):
        rows = parse_detail_table(
            _SECTION_PRE_CANCELLATION, PRE_HEADERS, PRE_ROWS,
            agent_code="0X4688", activity_date=date(2026, 10, 5))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].policy_number, "UB-B326957A")
        self.assertEqual(rows[0].account_name, "D SERVICE LANDSCAPING LLC")
        self.assertEqual(rows[0].dnoc_minimum_due, "$864.84")
        self.assertEqual(rows[0].notice_date, "10/12/2026")
        self.assertEqual(
            rows[0].document_id, "travelers:0X4688:2026-10-05:UB-B326957A")

    def test_cancellation_layout_column_order(self):
        # Account Name precedes Policy Number in the Cancellation Alert layout.
        rows = parse_detail_table(
            _SECTION_CANCELLATION, CAN_HEADERS, CAN_ROWS,
            agent_code="0X4688", activity_date=date(2026, 9, 30))
        self.assertEqual(rows[0].policy_number, "UB-C8291492")
        self.assertEqual(rows[0].account_name, "EMPOWER GROUP, LLC")
        self.assertEqual(rows[0].notice_date, "10/20/2026")

    def test_reinstatement_layout(self):
        rows = parse_detail_table(
            _SECTION_REINSTATEMENT, REIN_HEADERS, REIN_ROWS,
            agent_code="0X4688", activity_date=date(2026, 9, 22))
        self.assertEqual(rows[0].policy_number, "UB-R1111111")
        self.assertEqual(rows[0].notice_date, "09/25/2026")

    def test_missing_required_header_holds(self):
        with self.assertRaises(IntakeHold):
            parse_detail_table(
                _SECTION_PRE_CANCELLATION, ("Account Number", "Account Name"),
                PRE_ROWS, agent_code="0X4688", activity_date=date(2026, 10, 5))

    def test_empty_rows_hold(self):
        with self.assertRaises(IntakeHold):
            parse_detail_table(
                _SECTION_PRE_CANCELLATION, PRE_HEADERS, (),
                agent_code="0X4688", activity_date=date(2026, 10, 5))

    def test_bad_section_holds(self):
        with self.assertRaises(IntakeHold):
            parse_detail_table(
                "BOGUS", PRE_HEADERS, PRE_ROWS,
                agent_code="0X4688", activity_date=date(2026, 10, 5))


class SaveFormTests(unittest.TestCase):
    def test_valid_form(self):
        form = parse_save_form(SAVE_FORM_FIELDS)
        payload = form.payload()
        self.assertEqual(payload["agentCode"], "0X4688")
        self.assertEqual(payload["date"], "10/5/2026")
        self.assertEqual(payload["__RequestVerificationToken"], "token-abc-123")

    def test_missing_token_holds(self):
        fields = dict(SAVE_FORM_FIELDS)
        del fields["__RequestVerificationToken"]
        with self.assertRaises(IntakeHold):
            parse_save_form(fields)

    def test_missing_agent_code_holds(self):
        fields = dict(SAVE_FORM_FIELDS, agentCode="")
        with self.assertRaises(IntakeHold):
            parse_save_form(fields)


class IdentityTests(unittest.TestCase):
    def test_report_identity(self):
        self.assertEqual(
            report_document_id("0X4688", date(2026, 10, 5)),
            "travelers:0X4688:2026-10-05:report")
        self.assertEqual(
            report_filename("0X4688", date(2026, 10, 5)),
            "Travelers Direct Bill Activity 0X4688 2026-10-05.docx")

    def test_row_identity(self):
        self.assertEqual(
            row_document_id("0X4688", date(2026, 10, 5), "UB-B326957A"),
            "travelers:0X4688:2026-10-05:UB-B326957A")

    def test_bad_agent_code_holds(self):
        with self.assertRaises(IntakeHold):
            require_agent_code("!!!")

    def test_bad_policy_holds(self):
        with self.assertRaises(IntakeHold):
            require_policy_number("")

    def test_carrier_date_formats(self):
        self.assertEqual(parse_carrier_date("10/05/2026"), date(2026, 10, 5))
        self.assertEqual(parse_carrier_date("2026-10-05"), date(2026, 10, 5))
        with self.assertRaises(IntakeHold):
            parse_carrier_date("not a date")


# ---------------------------------------------------------------------------
# Browser / pull tests
# ---------------------------------------------------------------------------

def make_detail(section="pre", parent=None):
    if section == "pre":
        return FakeDetailPage(
            parent, section_title=PRE_TITLE, section_intro=PRE_INTRO,
            headers=PRE_HEADERS, rows=PRE_ROWS)
    if section == "cancel":
        return FakeDetailPage(
            parent, section_title=CAN_TITLE, section_intro=CAN_INTRO,
            headers=CAN_HEADERS, rows=CAN_ROWS)
    return FakeDetailPage(
        parent, section_title=REIN_TITLE, section_intro=REIN_INTRO,
        headers=REIN_HEADERS, rows=REIN_ROWS)


class NavigationTests(unittest.TestCase):
    def test_open_list_and_select_dates(self):
        page = MockTravelersPage()
        browser = PlaywrightTravelersBrowser(page)
        browser.open_direct_bill_activity()
        self.assertEqual(page.url, LIST_URL)
        dates = browser.list_activity_dates(as_of=AS_OF)
        self.assertEqual(len(dates), 4)  # 06/29 filtered out
        self.assertEqual(dates[0].activity_date, date(2026, 10, 5))

    def test_search_date_returns_view_rows(self):
        page = MockTravelersPage()
        browser = PlaywrightTravelersBrowser(page)
        browser.open_direct_bill_activity()
        dates = browser.list_activity_dates(as_of=AS_OF)
        rows = browser.search_date(dates[0])
        self.assertEqual(rows, (ListRow("0X4688", "2026-10-05T00:00:00"),))
        self.assertIn("select:10/05/2026", page.clicks)
        self.assertIn("Search", page.clicks)

    def test_open_detail_returns_new_tab(self):
        page = MockTravelersPage()
        detail = make_detail("pre", parent=page)
        page.detail_page = detail
        browser = PlaywrightTravelersBrowser(page)
        browser.open_direct_bill_activity()
        dates = browser.list_activity_dates(as_of=AS_OF)
        rows = browser.search_date(dates[0])
        opened = browser.open_detail(rows[0])
        self.assertIs(opened, detail)
        self.assertEqual(opened.url, DETAIL_URL)

    def test_open_detail_missing_link_holds(self):
        page = MockTravelersPage(list_rows=())
        # search still required for the view links to render
        browser = PlaywrightTravelersBrowser(page)
        browser.open_direct_bill_activity()
        dates = browser.list_activity_dates(as_of=AS_OF)
        with self.assertRaises(IntakeHold):
            browser.search_date(dates[0])

    def test_parse_detail_pre_cancellation(self):
        detail = make_detail("pre")
        page = MockTravelersPage()
        browser = PlaywrightTravelersBrowser(page)
        section, rows = browser.parse_detail(
            detail, agent_code="0X4688", activity_date=date(2026, 10, 5))
        self.assertEqual(section, _SECTION_PRE_CANCELLATION)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].policy_number, "UB-0S764907")

    def test_download_word_doc_posts_form(self):
        parent = MockTravelersPage()
        detail = make_detail("pre", parent=parent)
        browser = PlaywrightTravelersBrowser(parent)
        content = browser.download_word_doc(detail)
        self.assertTrue(content.startswith(b"PK\x03\x04"))
        url, form = parent.posted_forms[0]
        self.assertIn("SaveReportToWordDocument", url)
        self.assertEqual(form["agentCode"], "0X4688")
        self.assertEqual(form["__RequestVerificationToken"], "token-abc-123")

    def test_download_non_word_holds(self):
        parent = MockTravelersPage(post_returns_non_word=True)
        detail = make_detail("pre", parent=parent)
        browser = PlaywrightTravelersBrowser(parent)
        with self.assertRaisesRegex(IntakeHold, "not a Word document"):
            browser.download_word_doc(detail)

    def test_download_missing_token_holds(self):
        parent = MockTravelersPage()
        detail = FakeDetailPage(
            parent, section_title=PRE_TITLE, section_intro=PRE_INTRO,
            headers=PRE_HEADERS, rows=PRE_ROWS,
            save_fields={"agentCode": "0X4688", "date": "10/5/2026"},
            missing=("__RequestVerificationToken",))
        browser = PlaywrightTravelersBrowser(parent)
        with self.assertRaisesRegex(IntakeHold, "verificationtoken|field"):
            browser.download_word_doc(detail)

    def test_missing_select_holds(self):
        page = MockTravelersPage(missing=("select_date",))
        browser = PlaywrightTravelersBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "date selector"):
            browser.open_direct_bill_activity()

    def test_wrong_host_holds(self):
        page = MockTravelersPage()
        page.url = "https://example.com/Business"
        browser = PlaywrightTravelersBrowser(page)
        with self.assertRaisesRegex(IntakeHold, "portal URL"):
            browser.open_direct_bill_activity()


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict("os.environ", {"ROBIE_ENV": "TEST"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = Path(self.tmp.name) / "pull"
        self.ledger = TravelersDeliveryLedger(self.output)
        self.archive = SourceArchive(self.output / "sources")

    def _page(self, section="pre"):
        page = MockTravelersPage()
        detail = make_detail(section, parent=page)
        page.detail_page = detail
        return page

    def _run(self, page, **kwargs):
        browser = PlaywrightTravelersBrowser(page)
        return run_pull(browser, self.ledger, self.archive, as_of=AS_OF, **kwargs)

    def test_full_pull_downloads_word_doc_and_rows(self):
        page = self._page("pre")
        receipt = self._run(page, window_days=14)
        self.assertEqual(receipt["status"], "PULLED")
        # 4 dates in the window x 2 policies per date (mock repeats the fixture).
        self.assertEqual(receipt["count"], 8)
        self.assertEqual(receipt["held"], [])
        self.assertEqual(len(receipt["dates_processed"]), 4)
        expected = self.output / "2026-10-05" / (
            "Travelers Direct Bill Activity 0X4688 2026-10-05.docx")
        self.assertTrue(expected.exists())
        self.assertTrue(expected.read_bytes().startswith(b"PK\x03\x04"))
        policies = [d["policy_number"] for d in receipt["downloaded"]]
        self.assertEqual(policies.count("UB-B326957A"), 4)
        self.assertEqual(policies.count("UB-0S764907"), 4)
        # One Word doc POST per date.
        self.assertEqual(len(page.posted_forms), 4)
        # The form POST carried the anti-forgery token.
        _, form = page.posted_forms[0]
        self.assertEqual(form["__RequestVerificationToken"], "token-abc-123")

    def test_second_run_skips_by_ledger(self):
        page = self._page("pre")
        first = self._run(page, window_days=14)
        self.assertEqual(first["count"], 8)
        posts_after_first = len(page.posted_forms)
        self.assertEqual(posts_after_first, 4)
        page2 = self._page("pre")
        second = self._run(page2, window_days=14)
        self.assertEqual(second["status"], "PULLED")
        self.assertEqual(second["count"], 0)
        self.assertEqual(len(page2.posted_forms), 0)  # no re-download
        self.assertEqual(len(second["skipped_already_delivered"]), 8)

    def test_reinstatement_rows_held_not_downloaded(self):
        page = self._page("reinstatement")
        with self.assertRaises(PullHeld) as ctx:
            self._run(page, window_days=14)
        held = ctx.exception.details["held"]
        # 4 dates x 1 reinstatement row per date.
        self.assertEqual(len(held), 4)
        self.assertTrue(all("reinstatement" in h["hold_reason"].lower() for h in held))
        self.assertEqual(ctx.exception.details["downloaded"], [])
        self.assertEqual(len(page.posted_forms), 0)  # no Word doc for reinstatements

    def test_cancellation_alert_section_pulls(self):
        page = self._page("cancel")
        receipt = self._run(page, window_days=14)
        # 4 dates x 1 policy per date (mock repeats the fixture).
        self.assertEqual(receipt["count"], 4)
        self.assertEqual(
            receipt["downloaded"][0]["section"], _SECTION_CANCELLATION)
        self.assertEqual(
            receipt["downloaded"][0]["policy_number"], "UB-C8291492")

    def test_non_word_download_holds(self):
        page = self._page("pre")
        page.post_returns_non_word = True
        with self.assertRaises(PullHeld) as ctx:
            self._run(page, window_days=14)
        held = ctx.exception.details["held"]
        self.assertTrue(any("Word document" in h["hold_reason"] for h in held))

    def test_kill_switch_requires_test_env(self):
        with patch.dict("os.environ", {}, clear=True):
            page = self._page("pre")
            browser = PlaywrightTravelersBrowser(page)
            with self.assertRaises(IntakeHold):
                run_pull(browser, self.ledger, self.archive, as_of=AS_OF)

    def test_ledger_conflict_holds(self):
        page = self._page("pre")
        self._run(page, window_days=14)
        victim = self.output / "2026-10-05" / (
            "Travelers Direct Bill Activity 0X4688 2026-10-05.docx")
        victim.write_bytes(b"tampered")
        page2 = self._page("pre")
        with self.assertRaises(PullHeld) as ctx:
            self._run(page2, window_days=14)
        held = ctx.exception.details["held"]
        self.assertTrue(any("conflicts" in h["hold_reason"] for h in held))

    def test_production_host_refused(self):
        with patch("socket.gethostname", return_value="hermes-poc-01"), \
             patch("socket.getfqdn", return_value="hermes-poc-01"):
            with self.assertRaisesRegex(IntakeHold, "hermes-poc-01"):
                refuse_production_host()


if __name__ == "__main__":
    unittest.main()
