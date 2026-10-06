"""GEICO underwriting memo retrieval.

Phase 2 expansion of the GEICO worker. The Client Alerts page
(https://gateway2.geico.com/client-alerts) has filter chips as of 2026-10-04:
- All Alerts (55)
- Underwriting (26) -- count is dynamic, was 27 earlier same day
- Pending Cancellations (2) -- handled by geico_pending_cancellation_noc.py
- Renewals (7)
- Claims (18)
- Umbrella Eligible (2)

SCOPE (Carlo 2026-10-04): Nicole gets cancellation documents and
underwriting memos only. No billing.

Chip DOM (verified 2026-10-04 via live inspection):
- <gds-toggle-button role="button" aria-pressed="false|true" ...>Underwriting (26)</gds-toggle-button>
- GEICO Design System web component with SHADOW DOM (inner native <button>,
  text projected via <slot>). Playwright clicks on the host element work.
- Selected state: aria-pressed="true" + empty selected="" attribute.
- Clicking filters the table client-side; URL does NOT change.

Table columns (Underwriting view):
  Client/Policy# | Type | Priority | Product/Description | Details btn |
  Due Date | Claim # | Producer Name | Action ("View Policy"/"Review Change")

Per-row document path: the Action column link ("View Policy" for policy-update
alerts, "Review Change" for coverage/vehicle/driver changes). No separate
download control exists in the list view.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any


# Chip name pattern (with optional count suffix like "Underwriting (26)").
# Count is dynamic -- verified 27 at 17:00, 26 at 17:19 on 2026-10-04.
_UNDERWRITING_CHIP_NAME = re.compile(
    r"^underwriting(?:\s*\(\s*\d+\s*\))?$",
    re.IGNORECASE,
)

# Toggle button text pattern (for gds-toggle-button elements)
_UNDERWRITING_TOGGLE_TEXT = re.compile(r"^\s*Underwriting", re.IGNORECASE)

# CSS selector for GEICO Design System toggle chips
_GDS_TOGGLE_SELECTOR = "gds-toggle-button"


@dataclass
class MemoRecord:
    """A single underwriting memo from the GEICO alerts page."""
    memo_type: str  # "underwriting"
    policy_number: str
    insured_name: str
    alert_date: str
    description: str = ""
    document_url: str = ""
    downloaded_path: str = ""


@dataclass
class MemoPullResult:
    """Result of a memo pull run."""
    memo_type: str
    records: list[MemoRecord] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class IntakeHold(Exception):
    """Raised when the page state is not as expected (mirrors NOC module)."""
    pass


def _find_chip(page: Any, toggle_pattern: re.Pattern) -> Any | None:
    """Find a gds-toggle-button chip whose text matches the pattern.

    Returns the Playwright locator for the host element, or None.
    Playwright clicks on the host pierce the shadow DOM.
    """
    chips = page.locator(_GDS_TOGGLE_SELECTOR)
    count = chips.count()
    for i in range(count):
        chip = chips.nth(i)
        text = (chip.inner_text() or "").strip()
        if toggle_pattern.match(text):
            return chip
    return None


def _is_chip_selected(chip: Any) -> bool:
    """Check if a gds-toggle-button is in the selected state."""
    pressed = chip.get_attribute("aria-pressed")
    return pressed == "true"


def ensure_underwriting_view(page: Any) -> None:
    """Select the Underwriting filter chip on the GEICO Client Alerts page.

    Verified DOM (2026-10-04):
      <gds-toggle-button role="button" aria-pressed="false">
        Underwriting (26)
      </gds-toggle-button>
    Clicking filters the table client-side; URL does not change.
    """
    chip = _find_chip(page, _UNDERWRITING_TOGGLE_TEXT)
    if chip is None:
        raise IntakeHold(
            "Underwriting chip not found on Client Alerts page. "
            "Page may not have loaded or layout changed."
        )
    if _is_chip_selected(chip):
        return  # Already selected
    chip.click()
    # Wait for selection to take effect
    page.wait_for_function(
        "(chip) => chip.getAttribute('aria-pressed') === 'true'",
        arg=chip,
        timeout=10000,
    )
    # Verify the table filtered (header shows count)
    page.wait_for_timeout(1500)  # allow client-side filter to render


def extract_memo_list(page: Any, memo_type: str) -> list[MemoRecord]:
    """Extract the memo list from the currently selected chip view.

    Verified table structure (2026-10-04, Underwriting view):
      Columns: Client/Policy# | Type | Priority | Product/Description |
               Details btn | Due Date | Claim # | Producer Name | Action
      Action cell contains a link: "View Policy" or "Review Change".

    Each row becomes a MemoRecord. The Action link href (if present) is
    stored as document_url; otherwise the detail must be opened per-row
    during download.
    """
    records: list[MemoRecord] = []

    # Find the data table. GEICO uses a standard table; fall back to
    # role-based row lookup if the markup differs.
    rows = page.locator("table tbody tr")
    if rows.count() == 0:
        rows = page.locator("[role='row']")
    n = rows.count()
    if n == 0:
        raise IntakeHold(
            f"No memo rows found in {memo_type} view. "
            "Table may not have rendered."
        )

    for i in range(n):
        row = rows.nth(i)
        cells = row.locator("td")
        if cells.count() == 0:
            cells = row.locator("[role='gridcell'], [role='cell']")
        if cells.count() < 6:
            continue  # skip header or malformed rows

        # Column order per 2026-10-04 inspection:
        # 0=Client/Policy#, 1=Type, 2=Priority, 3=Product/Description,
        # 4=Details btn, 5=Due Date, 6=Claim #, 7=Producer Name, 8=Action
        client_policy = (cells.nth(0).inner_text() or "").strip()
        alert_type = (cells.nth(1).inner_text() or "").strip()
        description = (cells.nth(3).inner_text() or "").strip()
        due_date = (cells.nth(5).inner_text() or "").strip()

        # Split "Name / Policy#" -- format seen: "Charlemagne Guevara / 6253395526"
        # and "TRANSIT IN MOTION LLC / 9300362155"
        insured_name = client_policy
        policy_number = ""
        if "/" in client_policy:
            parts = [p.strip() for p in client_policy.split("/", 1)]
            insured_name, policy_number = parts[0], parts[1]
        else:
            # Fallback: policy number is the trailing numeric token
            m = re.search(r"(\d{6,})", client_policy)
            if m:
                policy_number = m.group(1)
                insured_name = client_policy.replace(policy_number, "").strip()

        # Action link (View Policy / Review Change)
        action_cell = cells.nth(8) if cells.count() > 8 else cells.last
        link = action_cell.locator("a").first
        document_url = ""
        try:
            if link.count() > 0:
                document_url = link.get_attribute("href") or ""
        except Exception:
            pass

        records.append(MemoRecord(
            memo_type=memo_type,
            policy_number=policy_number,
            insured_name=insured_name,
            alert_date=due_date,
            description=f"{alert_type} -- {description}".strip(" -"),
            document_url=document_url,
        ))

    return records


def download_memo_document(
    page: Any,
    record: MemoRecord,
    download_dir: str,
) -> str:
    """Download the memo document for a single record.

    Path: click the row's Action link ("View Policy" / "Review Change"),
    which opens the policy/detail view. Then look for a download button,
    PDF embed, or document link.

    NOTE: The detail-view DOM has not been inspected yet (2026-10-04).
    This implements the common patterns; if none match it raises
    IntakeHold with what was found so the selector can be refined.
    """
    import os

    # Find the row for this policy number
    rows = page.locator("table tbody tr")
    if rows.count() == 0:
        rows = page.locator("[role='row']")
    target_row = None
    for i in range(rows.count()):
        row_text = rows.nth(i).inner_text() or ""
        if record.policy_number and record.policy_number in row_text:
            target_row = rows.nth(i)
            break
    if target_row is None:
        raise IntakeHold(
            f"Row for policy {record.policy_number} not found in table."
        )

    # Click the Action link (opens detail view, possibly new tab)
    action_link = target_row.locator("a").first
    if action_link.count() == 0:
        raise IntakeHold(
            f"No Action link in row for policy {record.policy_number}."
        )

    # Expect a download event after clicking
    try:
        with page.expect_download(timeout=20000) as download_info:
            action_link.click()
        download = download_info.value
    except Exception:
        # No direct download -- the link may have navigated to a detail page.
        # Look for download controls on the current/new page.
        download = None

    if download is not None:
        os.makedirs(download_dir, exist_ok=True)
        safe_name = re.sub(r"[^\w\-.]", "_", record.policy_number or "memo")
        filename = f"{record.memo_type}_{safe_name}_{download.suggested_filename}"
        dest = os.path.join(download_dir, filename)
        download.save_as(dest)
        return dest

    # Detail page path: look for PDF viewer, download button, doc links.
    # Check for a newly opened tab first.
    pages = page.context.pages
    detail_page = pages[-1] if len(pages) > 1 else page
    detail_page.wait_for_timeout(3000)

    # Common download selectors to try in order
    download_selectors = [
        "a[href$='.pdf']",
        "button:has-text('Download')",
        "a:has-text('Download')",
        "[aria-label*='download' i]",
        "embed[type='application/pdf']",
        "iframe[src*='.pdf']",
    ]
    for sel in download_selectors:
        el = detail_page.locator(sel).first
        try:
            if el.count() > 0:
                href = el.get_attribute("href") or el.get_attribute("src") or ""
                raise IntakeHold(
                    f"Detail view opened for {record.policy_number} but "
                    f"auto-download not triggered. Found candidate {sel} "
                    f"(href/src: {href[:80]}). Manual selector refinement needed."
                )
        except IntakeHold:
            raise
        except Exception:
            continue

    raise IntakeHold(
        f"Detail view for {record.policy_number} opened but no download "
        f"control found. Page URL: {detail_page.url[:100]}. "
        f"Needs live DOM inspection of the detail view."
    )


def run_memo_pull(
    page: Any,
    download_dir: str,
    *,
    max_records: int = 0,
) -> MemoPullResult:
    """Run a full underwriting memo pull.

    Args:
        page: Playwright page object (authenticated GEICO session)
        download_dir: Directory to save downloaded documents
        max_records: Maximum records to process (0 = no limit)

    Returns:
        MemoPullResult with records, downloaded paths, held items, errors.
    """
    result = MemoPullResult(memo_type="underwriting")

    # Step 1: Select the Underwriting chip view
    try:
        ensure_underwriting_view(page)
    except IntakeHold as e:
        result.errors.append(f"Could not select underwriting view: {e}")
        return result

    # Step 2: Extract the memo list
    try:
        records = extract_memo_list(page, "underwriting")
    except Exception as e:
        result.errors.append(f"Failed to extract memo list: {e}")
        return result

    if max_records > 0:
        records = records[:max_records]
    result.records = records

    # Step 3: Download each memo document
    for record in records:
        try:
            path = download_memo_document(page, record, download_dir)
            record.downloaded_path = path
            result.downloaded.append(path)
        except IntakeHold as e:
            result.held.append({
                "policy": record.policy_number,
                "insured": record.insured_name,
                "reason": str(e),
            })
        except NotImplementedError as e:
            result.errors.append(str(e))
            break
        except Exception as e:
            result.errors.append(
                f"Download failed for {record.policy_number}: {e}"
            )

    return result


def __main__() -> None:
    """CLI entry point for testing underwriting memo pulls."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Pull GEICO underwriting memos"
    )
    parser.add_argument(
        "--download-dir",
        default="/tmp/geico-memos",
        help="Directory for downloads",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="Max records (0 = no limit)",
    )
    args = parser.parse_args()

    print("GEICO underwriting memo pull")
    print("STATUS: Implemented -- needs live run to verify")
    print("See docs/handoff-geico-memo-expansion.md for status")
    sys.exit(1)


if __name__ == "__main__":
    __main__()
