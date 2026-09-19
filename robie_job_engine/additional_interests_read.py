"""Read-only EZLynx Additional Interests table (structured DOM, not PDF).

Carlo 2026-09-19: PolicyApi cannot see Additional Interests / mortgagee /
loan, and policy-by-ID detail endpoints do not exist. The sanctioned
fallback is a **read-only** browser read of the policy Additional
Interests tab table (lender name, loan number, type).

This is not a new browser stack. It attaches the existing SSRobie Chrome
CDP session the same way ``CdpReadPort`` / ``BoundedBrowserReadWorker``
do, clicks a unique Additional Interest(s) tab the same way FormEntry
coverages clicks a unique nav name, and evaluates table cells. It never
launches Chrome, never writes notes/docs, never opens a file chooser,
never OCRs, and never scrapes a PDF.

Unbound callers must not construct this port. ``__init__`` does not
connect; only ``read_additional_interests`` talks to CDP.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

# Local copies of the enrichment guards. Do not import mortgagee_enrichment
# at module load — that module imports this one.
REASON_PDF_SCRAPE = (
    "refused: will not scrape a PDF/OCR blob into lender or loan fields"
)
REASON_NO_INVENT = "refused: no unique structured field match — will not invent values"
REASON_SSN = "refused: SSN / full SSN is never used for mortgagee enrichment"
_SCRAPE_KEYS = frozenset({
    "ocr", "ocrtext", "pdftext", "pdfscrape", "extractedtext",
    "fulltext", "rawtext", "pagetext", "textextract",
})
_SSN_KEYS = frozenset({
    "ssn", "fullssn", "socialsecurity", "socialsecuritynumber",
    "taxpayerssn", "ssnfull", "socialsecurityno",
})
_SSN_VALUE_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")


def _fold_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").casefold())


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_ssn(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and _SSN_VALUE_RE.match(text))


def _assert_no_ssn_keys(mapping: dict[str, Any]) -> None:
    for key in mapping:
        if _fold_key(key) in _SSN_KEYS:
            raise _enrichment_error(REASON_SSN)


def _enrichment_error(reason: str) -> Exception:
    try:
        from .mortgagee_enrichment import MortgageeEnrichmentError
    except ImportError:  # pragma: no cover
        from mortgagee_enrichment import MortgageeEnrichmentError  # type: ignore
    return MortgageeEnrichmentError(reason)

SOURCE_ADDITIONAL_INTERESTS = "additional_interests"

ADDITIONAL_INTEREST_TAB_NAMES = (
    "Additional Interests",
    "Additional Interest",
)
TAB_ROLES = ("tab", "link", "button")

# Header names that uniquely identify lender / loan / type columns.
_TABLE_LENDER_HEADERS = frozenset({
    "name",
    "interestname",
    "mortgageename",
    "mortgagee",
    "lendername",
    "lender",
    "company",
    "companyname",
    "additionalinterestname",
    "additionalinterest",
    "mortgageeclause",
})
_TABLE_LOAN_HEADERS = frozenset({
    "loannumber",
    "mortgageloannumber",
    "loanno",
    "mortgageloanno",
    "loan",
})
_TABLE_TYPE_HEADERS = frozenset({
    "type",
    "interesttype",
    "additionalinteresttype",
    "interesttypecode",
})
_HEADER_SCORE_HINTS = frozenset({
    "name", "interestname", "mortgagee", "lender", "type",
    "interesttype", "loannumber", "loan", "loanno", "company",
    "additionalinterest",
})
_MORTGAGEE_TYPES = frozenset({
    "mortgagee",
    "firstmortgagee",
    "secondmortgagee",
    "thirdmortgagee",
    "mortgageeclause",
    "lender",
    "mortgage",
    "mortgageholder",
})
_NON_MORTGAGEE_TYPES = frozenset({
    "additionalinsured",
    "additionalinterest",
    "losspayee",
    "certificateholder",
})

# Structured DOM evaluate — header-scored unique table, never .first/.nth.
ADDITIONAL_INTERESTS_TABLE_JS = r"""
() => {
  const fold = (s) => String(s || "").replace(/[^a-z0-9]/gi, "").toLowerCase();
  const hints = ["name", "interestname", "mortgagee", "lender", "type",
                 "interesttype", "loannumber", "loan", "loanno", "company",
                 "additionalinterest"];
  const headerHits = (headers) => {
    const folded = headers.map(fold);
    return folded.filter((h) => hints.some((w) => h.includes(w) || w.includes(h))).length;
  };
  const headerCellsOf = (table) => {
    const thead = table.querySelector("thead");
    if (thead) {
      const row = thead.querySelector("tr") || thead;
      return Array.from(row.querySelectorAll("th,td")).map(
        (el) => (el.innerText || el.textContent || "").trim()
      );
    }
    const first = table.querySelector("tr");
    if (!first) return [];
    return Array.from(first.querySelectorAll("th,td")).map(
      (el) => (el.innerText || el.textContent || "").trim()
    );
  };
  const dataRowsOf = (table) => {
    const body = table.querySelector("tbody");
    if (body) return Array.from(body.querySelectorAll("tr"));
    const rows = Array.from(table.querySelectorAll("tr"));
    return rows.slice(1);
  };
  const scored = [];
  for (const table of Array.from(document.querySelectorAll("table"))) {
    const headers = headerCellsOf(table).filter((h) => String(h || "").trim());
    const score = headerHits(headers);
    if (score === 0) continue;
    const rows = [];
    for (const tr of dataRowsOf(table)) {
      const cells = Array.from(tr.querySelectorAll("td,th")).map(
        (el) => (el.innerText || el.textContent || "").trim()
      );
      if (!cells.some(Boolean)) continue;
      const row = {};
      headers.forEach((h, i) => { row[h || ("col" + i)] = cells[i] || ""; });
      rows.push(row);
    }
    scored.push({ score, headers, rows });
  }
  if (!scored.length) {
    return { table_found: false, tab_visible: true, headers: [], rows: [] };
  }
  scored.sort((a, b) => b.score - a.score);
  if (scored.length > 1 && scored[0].score === scored[1].score) {
    return {
      table_found: false,
      tab_visible: true,
      conflict: "HITL: multiple Additional Interests tables scored equally — not picking a side",
      headers: [],
      rows: [],
    };
  }
  return {
    table_found: true,
    tab_visible: true,
    headers: scored[0].headers,
    rows: scored[0].rows,
  };
}
"""


class _HtmlTableParser(HTMLParser):
    """Collect raw ``<table>`` grids. No positional guess — caller scores."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._current: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._capture = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "table":
            self._current = []
        elif tag == "tr" and self._current is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
            self._capture = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
            self._capture = False
        elif tag == "tr" and self._row is not None and self._current is not None:
            if any(cell.strip() for cell in self._row):
                self._current.append(self._row)
            self._row = None
        elif tag == "table" and self._current is not None:
            self.tables.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._capture and self._cell is not None:
            self._cell.append(data)


def _header_score(headers: list[str]) -> int:
    folded = [_fold_key(h) for h in headers]
    return sum(
        1
        for h in folded
        if any(hint in h or h in hint for hint in _HEADER_SCORE_HINTS)
    )


def _grids_from_html(html: str) -> list[tuple[list[str], list[list[str]]]]:
    parser = _HtmlTableParser()
    parser.feed(html)
    out: list[tuple[list[str], list[list[str]]]] = []
    for grid in parser.tables:
        if not grid:
            continue
        headers = [cell.strip() for cell in grid[0]]
        body = grid[1:]
        out.append((headers, body))
    return out


def _refuse_scrape_and_ssn(mapping: dict[str, Any]) -> None:
    _assert_no_ssn_keys(mapping)
    for key, raw in mapping.items():
        folded = _fold_key(key)
        if folded in _SCRAPE_KEYS and _clean_text(raw):
            raise _enrichment_error(REASON_PDF_SCRAPE)
        if folded in _SSN_KEYS:
            raise _enrichment_error(REASON_SSN)
        if _looks_like_ssn(raw):
            raise _enrichment_error(REASON_SSN)


def _pick_column(headers: list[str], allowed: frozenset[str]) -> str | None:
    hits = [h for h in headers if _fold_key(h) in allowed]
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        return None
    return None


def _row_dict_from_cells(headers: list[str], cells: list[str]) -> dict[str, str]:
    row: dict[str, str] = {}
    for index, header in enumerate(headers):
        key = header or f"col{index}"
        row[key] = cells[index] if index < len(cells) else ""
    return row


def _is_mortgagee_type(interest_type: str) -> bool | None:
    folded = _fold_key(interest_type)
    if not folded:
        return None
    if folded in _MORTGAGEE_TYPES or any(token in folded for token in _MORTGAGEE_TYPES):
        return True
    if folded in _NON_MORTGAGEE_TYPES:
        return False
    return None


def _records_from_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[Any], str]:
    try:
        from .mortgagee_enrichment import MortgageRecord
    except ImportError:  # pragma: no cover
        from mortgagee_enrichment import MortgageRecord  # type: ignore
    mortgages: list[Any] = []
    for mapping in rows:
        if not isinstance(mapping, dict):
            continue
        _refuse_scrape_and_ssn(mapping)
        headers = [str(key) for key in mapping]
        lender_key = _pick_column(headers, _TABLE_LENDER_HEADERS)
        loan_key = _pick_column(headers, _TABLE_LOAN_HEADERS)
        type_key = _pick_column(headers, _TABLE_TYPE_HEADERS)
        if lender_key is None:
            # Fallback: a uniquely allowlisted lender-shaped key that
            # did not collide. If two lender headers disagree, HITL.
            found: list[str] = []
            for key, raw in mapping.items():
                if _fold_key(key) in _TABLE_LENDER_HEADERS:
                    text = _clean_text(raw)
                    if text:
                        found.append(text)
            unique = list(dict.fromkeys(found))
            if len(unique) > 1:
                return [], (
                    "HITL: multiple Additional Interests name columns disagree "
                    f"({unique!r}) — not picking a side"
                )
            lender = unique[0] if unique else ""
        else:
            lender = _clean_text(mapping.get(lender_key))
        if loan_key is None:
            found_loans: list[str] = []
            for key, raw in mapping.items():
                if _fold_key(key) in _TABLE_LOAN_HEADERS:
                    text = _clean_text(raw)
                    if text:
                        found_loans.append(text)
            unique_loans = list(dict.fromkeys(found_loans))
            if len(unique_loans) > 1:
                return [], (
                    "HITL: multiple Additional Interests loan columns disagree "
                    f"({unique_loans!r}) — not picking a side"
                )
            loan = unique_loans[0] if unique_loans else ""
        else:
            loan = _clean_text(mapping.get(loan_key))
        interest_type = _clean_text(mapping.get(type_key)) if type_key else ""
        kind = _is_mortgagee_type(interest_type)
        if kind is False and not loan:
            continue
        if not lender and not loan:
            continue
        if not lender or not loan:
            if kind is True or kind is None:
                return [], (
                    f"{REASON_NO_INVENT} (Additional Interests row has "
                    f"{'lender' if lender else 'no lender'}, "
                    f"{'loan' if loan else 'no loan'})"
                )
            continue
        if _looks_like_ssn(lender) or _looks_like_ssn(loan):
            raise _enrichment_error(REASON_SSN)
        mortgages.append(
            MortgageRecord(
                lender_name=lender,
                loan_number=loan,
                source=SOURCE_ADDITIONAL_INTERESTS,
                interest_type=interest_type,
            )
        )
    return mortgages, ""


def parse_additional_interests_table(
    payload: Any,
) -> tuple[list[MortgageRecord], str, bool, bool]:
    """Parse a structured Additional Interests table snapshot.

    Returns ``(mortgages, conflict, explicit_empty, table_found)``.

    ``explicit_empty`` is True only when the table is present and has
    zero data rows. A missing tab / missing table is not proof of zero.
    PDF/OCR keys are refused. SSN is refused. Never invents lender/loan.
    """
    if payload is None:
        return [], "", False, False
    if isinstance(payload, str):
        if "<table" not in payload.casefold():
            return [], "", False, False
        grids = _grids_from_html(payload)
        scored = [
            (score, headers, body)
            for headers, body in grids
            if (score := _header_score(headers)) > 0
        ]
        if not scored:
            return [], "", False, False
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return [], (
                "HITL: multiple Additional Interests tables scored equally "
                "— not picking a side"
            ), False, False
        _score, headers, body = scored[0]
        rows = [_row_dict_from_cells(headers, cells) for cells in body]
        mortgages, conflict = _records_from_rows(rows)
        table_found = True
        explicit_empty = bool(table_found and not rows and not conflict)
        return mortgages, conflict, explicit_empty, table_found

    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
        for row in rows:
            _refuse_scrape_and_ssn(row)
        mortgages, conflict = _records_from_rows(rows)
        table_found = True
        explicit_empty = bool(not rows and not conflict)
        return mortgages, conflict, explicit_empty, table_found

    if not isinstance(payload, dict):
        return [], "", False, False

    _refuse_scrape_and_ssn(payload)
    if payload.get("conflict"):
        return [], str(payload["conflict"]), False, bool(payload.get("tab_visible"))

    raw_rows = payload.get("rows")
    if raw_rows is None:
        raw_rows = payload.get("additional_interests")
    if isinstance(raw_rows, list):
        rows = [row for row in raw_rows if isinstance(row, dict)]
    else:
        rows = []
    for row in rows:
        _refuse_scrape_and_ssn(row)

    table_found = bool(payload.get("table_found", bool(rows or payload.get("headers"))))
    if payload.get("table_found") is False and not rows:
        table_found = False
    mortgages, conflict = _records_from_rows(rows) if table_found else ([], "")
    if conflict:
        return mortgages, conflict, False, table_found
    explicit_empty = bool(table_found and not rows)
    return mortgages, "", explicit_empty, table_found


def click_unique_additional_interests_tab(page: Any) -> str | None:
    """Click the unique Additional Interest(s) tab. No positional guess."""
    for name in ADDITIONAL_INTEREST_TAB_NAMES:
        for role in TAB_ROLES:
            getter = getattr(page, "get_by_role", None)
            if not callable(getter):
                continue
            loc = getter(role, name=name, exact=True)
            try:
                count = loc.count()
            except Exception:
                continue
            if count != 1:
                continue
            loc.click(timeout=8000)
            return f'get_by_role("{role}", name="{name}", exact=True)'
    try:
        try:
            from .locator_registry import LocatorRegistry
        except ImportError:  # pragma: no cover
            from locator_registry import LocatorRegistry  # type: ignore
        loc = LocatorRegistry().resolve_element(
            page, "ezlynx", "policy_setup", "tab_additional_interest",
        )
        loc.click(timeout=8000)
        return "locator_registry:ezlynx:policy_setup.tab_additional_interest"
    except Exception:
        return None


def _read_table_from_page(page: Any, policy_number: str) -> dict[str, Any]:
    clicked = click_unique_additional_interests_tab(page)
    if not clicked:
        return {
            "table_found": False,
            "tab_visible": False,
            "headers": [],
            "rows": [],
            "policy_number": policy_number,
            "locator": "",
        }
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {
            "table_found": False,
            "tab_visible": True,
            "headers": [],
            "rows": [],
            "policy_number": policy_number,
            "locator": clicked,
            "error": "page.evaluate is not available",
        }
    snapshot = evaluate(ADDITIONAL_INTERESTS_TABLE_JS)
    if not isinstance(snapshot, dict):
        snapshot = {"table_found": False, "tab_visible": True, "headers": [], "rows": []}
    snapshot = dict(snapshot)
    snapshot.setdefault("tab_visible", True)
    snapshot["policy_number"] = policy_number
    snapshot["locator"] = clicked
    snapshot["engine"] = "playwright"
    return snapshot


class CdpAdditionalInterestsPort:
    """Read-only Additional Interests table via existing SSRobie Chrome CDP.

    Reuses ``chat_verifier_ports._cdp_url`` / ``connect_over_cdp``. Does not
    invent policy-search navigation — the policy FormEntry page must
    already be open in the attached Chrome. Missing tab → ``tab_visible``
    False. ``page=`` injects a test double and never opens CDP.
    """

    def __init__(self, cdp_url: str | None = None, page: Any = None):
        self._cdp_url = cdp_url
        self._page = page

    def read_additional_interests(
        self,
        policy_number: str,
        applicant_id: str = "",
    ) -> dict[str, Any]:
        del applicant_id
        number = _clean_text(policy_number)
        if self._page is not None:
            return _read_table_from_page(self._page, number)
        try:
            from .chat_verifier_ports import _cdp_url
        except ImportError:  # pragma: no cover
            from chat_verifier_ports import _cdp_url  # type: ignore
        endpoint = self._cdp_url or _cdp_url()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is required for Additional Interests browser read"
            ) from exc
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=15000)
            contexts = browser.contexts
            if not contexts:
                raise RuntimeError("Chrome has no browser context")
            pages = [page for ctx in browser.contexts for page in ctx.pages]
            if not pages:
                raise RuntimeError(
                    "SSRobie Chrome has no open page — open the policy "
                    "FormEntry Additional Interests view before this read"
                )
            page = _pick_policy_page(pages, number) or pages[0]
            return _read_table_from_page(page, number)
        finally:
            playwright.stop()


def _pick_policy_page(pages: list[Any], policy_number: str) -> Any | None:
    """Prefer an already-open page whose URL/title mentions the policy.

    Does not navigate or search. No match → caller uses the first page.
    """
    needle = _clean_text(policy_number)
    if not needle:
        return None
    folded = needle.casefold()
    for candidate in pages:
        try:
            blob = f"{candidate.url} {candidate.title()}".casefold()
        except Exception:
            continue
        if folded in blob:
            return candidate
    return None
