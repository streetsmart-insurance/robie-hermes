#!/usr/bin/env python3
"""Fail-closed Submission Center audit budget + bulk page read.

Production walks every non-closed row until the first closed status. With ~5k
rows that walk exceeded the diagnose `timeout 480s`, which SIGKILL'd Python
before preflight could print JSON (empty accountability-submission-preflight.txt).

This repair:
1. Accepts deadline_seconds on audit()
2. Raises PLAYWRIGHT_BLOCKED when the budget is exhausted mid-walk
3. Reads each page with one evaluate() instead of per-cell Playwright roundtrips
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

IMPORT_OLD = "from datetime import date, datetime\n"
IMPORT_NEW = "from datetime import date, datetime\nimport time\n"

SIG_OLD = "def audit(*, fresh: bool) -> dict[str, Any]:\n"
SIG_NEW = (
    "def audit(*, fresh: bool, deadline_seconds: int | None = None) -> dict[str, Any]:\n"
    "    deadline_monotonic = (\n"
    "        None\n"
    "        if deadline_seconds is None\n"
    "        else time.monotonic() + max(1, int(deadline_seconds))\n"
    "    )\n"
)

LOOP_OLD = '''        while first_closed_page is None:
            pages_reviewed += 1
            start, end, _ = _pager_range(page)
            expected_page_rows = end - start + 1
            rows = page.locator("mat-row")
            rendered_statuses = _row_statuses(page, positions["status"])
            if len(rendered_statuses) < expected_page_rows:
                raise RuntimeError(
                    "PLAYWRIGHT_BLOCKED: pager range exceeded rendered Submission Center statuses"
                )
            statuses = rendered_statuses[:expected_page_rows]
            if not statuses:
                raise RuntimeError("PLAYWRIGHT_BLOCKED: Submission Center page rendered no statuses")
            for index, status in enumerate(statuses):
                rows_inspected += 1
                if status in CLOSED:
                    first_closed_page = pages_reviewed
                    first_closed_index = index
                    first_closed_status = status
                    break
                non_closed_inspected += 1
                non_closed_statuses.append(status)
                record = _read_submission_row(
                    page,
                    rows.nth(index),
                    positions,
                    run_date=run_date,
                    page_number=pages_reviewed,
                    row_number=index + 1,
                )
                if record["qualifies"]:
                    qualifying.append({key: value for key, value in record.items() if key not in {"qualifies", "exclusion_reasons"}})
                elif (
                    record.get("age_days") is None
                    or int(record.get("age_days") or 0) > 30
                    or record["red_state_evidence"]["overdue_class"]
                ):
                    # Preserve only evidence needed to explain an old/red/ambiguous
                    # candidate. Do not retain every current non-closed applicant.
                    dispositions.append(record)
'''

LOOP_NEW = '''        while first_closed_page is None:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise RuntimeError(
                    "PLAYWRIGHT_BLOCKED: audit exceeded time budget before the closed-row boundary"
                )
            pages_reviewed += 1
            start, end, _ = _pager_range(page)
            expected_page_rows = end - start + 1
            page_records = _read_page_rows(
                page,
                positions,
                run_date=run_date,
                page_number=pages_reviewed,
                expected_rows=expected_page_rows,
            )
            statuses = [str(item.get("status") or "").strip() for item in page_records]
            if len(statuses) < expected_page_rows:
                raise RuntimeError(
                    "PLAYWRIGHT_BLOCKED: pager range exceeded rendered Submission Center statuses"
                )
            if not statuses:
                raise RuntimeError("PLAYWRIGHT_BLOCKED: Submission Center page rendered no statuses")
            for index, status in enumerate(statuses):
                rows_inspected += 1
                if status in CLOSED:
                    first_closed_page = pages_reviewed
                    first_closed_index = index
                    first_closed_status = status
                    break
                non_closed_inspected += 1
                non_closed_statuses.append(status)
                record = page_records[index]
                if record["qualifies"]:
                    qualifying.append({key: value for key, value in record.items() if key not in {"qualifies", "exclusion_reasons"}})
                elif (
                    record.get("age_days") is None
                    or int(record.get("age_days") or 0) > 30
                    or record["red_state_evidence"]["overdue_class"]
                ):
                    # Preserve only evidence needed to explain an old/red/ambiguous
                    # candidate. Do not retain every current non-closed applicant.
                    dispositions.append(record)
'''

BULK_HELPER = '''
def _read_page_rows(
    page: Page,
    positions: dict[str, int],
    *,
    run_date: date,
    page_number: int,
    expected_rows: int,
) -> list[dict[str, Any]]:
    """Read one Submission Center page in a single evaluate (avoids per-cell roundtrips)."""
    payload = page.evaluate(
        """({ positions, expectedRows, expectedColor }) => {
            const rows = Array.from(document.querySelectorAll('mat-row')).slice(0, expectedRows);
            return rows.map((row, index) => {
                const cells = Array.from(row.querySelectorAll('mat-cell'));
                const textAt = (key) => {
                    const cell = cells[positions[key]];
                    return cell ? cell.innerText.replace(/\\\\s+/g, ' ').trim() : '';
                };
                const quoteCell = cells[positions.quote_due_date];
                const className = quoteCell ? String(quoteCell.className || '') : '';
                const computedColor = quoteCell
                    ? getComputedStyle(quoteCell).color.trim()
                    : '';
                const overdueClass = /(?:^|\\\\s)[^\\\\s]*overdue[^\\\\s]*(?:\\\\s|$)/i.test(className);
                const hrefs = Array.from(row.querySelectorAll('a[href]'))
                    .map((a) => a.href || '')
                    .filter((href) => href && href.toLowerCase().includes('submission'));
                const unique = [...new Set(hrefs)];
                return {
                    applicant: textAt('applicant'),
                    assigned_producer: textAt('assigned_producer'),
                    status: textAt('status'),
                    quote_due_date: textAt('quote_due_date'),
                    effective_date: textAt('effective_date'),
                    overdue_class: overdueClass,
                    computed_color: computedColor,
                    submission_url: unique.length === 1 ? unique[0] : '',
                    source_row: index + 1,
                };
            });
        }""",
        {
            "positions": positions,
            "expectedRows": expected_rows,
            "expectedColor": RED_OVERDUE_COLOR,
        },
    )
    if not isinstance(payload, list) or len(payload) < expected_rows:
        raise RuntimeError(
            "PLAYWRIGHT_BLOCKED: bulk Submission Center page read returned incomplete rows"
        )
    records: list[dict[str, Any]] = []
    for item in payload[:expected_rows]:
        quote_due_date = _parse_visible_date(str(item.get("quote_due_date") or ""))
        effective_date = _parse_visible_date(str(item.get("effective_date") or ""))
        age_days = (run_date - quote_due_date).days if quote_due_date else None
        overdue_class = bool(item.get("overdue_class"))
        computed_color = str(item.get("computed_color") or "").strip()
        direct_url = str(item.get("submission_url") or "").strip()
        red_overdue = overdue_class and computed_color == RED_OVERDUE_COLOR
        reasons: list[str] = []
        if quote_due_date is None:
            reasons.append("Quote Due Date was not a supported visible date")
        elif age_days is not None and age_days <= 30:
            reasons.append(f"Quote Due Date was {age_days} day(s) old; day 31 qualifies")
        if not overdue_class:
            reasons.append("Quote Due Date cell lacked the live overdue class")
        elif computed_color != RED_OVERDUE_COLOR:
            reasons.append(
                f"Quote Due Date overdue class rendered unexpected color {computed_color or 'missing'}"
            )
        if not direct_url:
            reasons.append("unique direct submission link was not available")
        qualifies = bool(
            quote_due_date
            and age_days is not None
            and age_days > 30
            and red_overdue
            and direct_url
        )
        records.append(
            {
                "applicant": str(item.get("applicant") or ""),
                "assigned_producer": str(item.get("assigned_producer") or "") or "Unassigned",
                "status": str(item.get("status") or "").strip(),
                "quote_due_date": quote_due_date.isoformat()
                if quote_due_date
                else str(item.get("quote_due_date") or ""),
                "effective_date": effective_date.isoformat()
                if effective_date
                else str(item.get("effective_date") or ""),
                "age_days": age_days,
                "submission_url": direct_url,
                "red_state_evidence": {
                    "overdue_class": overdue_class,
                    "computed_color": computed_color,
                    "expected_color": RED_OVERDUE_COLOR,
                },
                "source_page": page_number,
                "source_row": int(item.get("source_row") or 0),
                "qualifies": qualifies,
                "exclusion_reasons": reasons,
            }
        )
    return records


'''


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    old_count = source.count(old)
    new_count = source.count(new)
    if old_count == 0 and new_count >= 1:
        return source
    if old_count != 1:
        raise RuntimeError(
            f"refusing unexpected {label} source shape: old={old_count}, new={new_count}"
        )
    return source.replace(old, new, 1)


def _write_repaired(path: Path, updated: str, backup_suffix: str) -> None:
    backup = path.with_suffix(path.suffix + backup_suffix)
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)


def repair_browser(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    updated = original
    if "import time\n" not in updated:
        updated = _replace_once(updated, IMPORT_OLD, IMPORT_NEW, "import-time")
    if "def _read_page_rows(" not in updated:
        anchor = "def audit(*, fresh: bool"
        if "def audit(*, fresh: bool, deadline_seconds" in updated:
            anchor = "def audit(*, fresh: bool, deadline_seconds"
        idx = updated.find(anchor)
        if idx < 0:
            raise RuntimeError("audit() definition not found for bulk helper insert")
        updated = updated[:idx] + BULK_HELPER + updated[idx:]
    updated = _replace_once(updated, SIG_OLD, SIG_NEW, "audit-signature")
    # After signature replace, SIG_OLD is gone; also handle already-repaired
    if "deadline_monotonic" not in updated.split("def audit(", 1)[1][:400]:
        raise RuntimeError("deadline_monotonic missing after signature repair")
    updated = _replace_once(updated, LOOP_OLD, LOOP_NEW, "audit-loop")
    if updated == original:
        return "already_repaired"
    _write_repaired(path, updated, ".pre-audit-budget")
    verified = path.read_text(encoding="utf-8")
    for required in (
        "deadline_monotonic",
        "def _read_page_rows(",
        "audit exceeded time budget before the closed-row boundary",
        "bulk Submission Center page read returned incomplete rows",
    ):
        if required not in verified:
            raise RuntimeError(f"Submission Center audit-budget repair missing: {required}")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-path", type=Path, required=True)
    args = parser.parse_args()
    print(repair_browser(args.browser_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
