#!/usr/bin/env python3
"""Teach the dedicated Submission Center collector a truthful terminal boundary.

If the live pager is exhausted before a closed row appears, the collector may
proceed only when it has inspected and reconciled every row in the result set.
The repair is exact-shape, idempotent, and refuses unexpected Production code.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


BROWSER_INIT_OLD = '''        first_closed_status = ""

        while first_closed_page is None:
'''
BROWSER_INIT_NEW = '''        first_closed_status = ""
        full_dataset_exhausted = False

        while first_closed_page is None:
'''
BROWSER_LOOP_OLD = '''            if first_closed_page is None:
                _advance_page(page, start)

        if not non_closed_statuses:
'''
BROWSER_LOOP_NEW = '''            if first_closed_page is None:
                try:
                    _advance_page(page, start)
                except RuntimeError as exc:
                    if "non-closed group exceeded the available live pages" not in str(exc):
                        raise
                    full_dataset_exhausted = True
                    break

        if not non_closed_statuses:
'''
BROWSER_RESULT_OLD = '''            "first_closed_row_inspected": True,
            "first_closed_row_index": first_closed_index,
            "first_closed_row_page": first_closed_page,
            "first_closed_row_status": first_closed_status,
'''
BROWSER_RESULT_NEW = '''            "first_closed_row_inspected": first_closed_page is not None,
            "full_dataset_exhausted": full_dataset_exhausted,
            "boundary_kind": "first_closed_row" if first_closed_page is not None else "pager_exhausted",
            "first_closed_row_index": first_closed_index,
            "first_closed_row_page": first_closed_page,
            "first_closed_row_status": first_closed_status,
'''

BROWSER_RANGE_OLD = '''            start, _, _ = _pager_range(page)
            rows = page.locator("mat-row")
            statuses = _row_statuses(page, positions["status"])
            if not statuses:
                raise RuntimeError("PLAYWRIGHT_BLOCKED: Submission Center page rendered no statuses")
'''
BROWSER_RANGE_NEW = '''            start, end, _ = _pager_range(page)
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
'''

ADAPTER_REQUIRED_OLD = '''        "first_row_non_closed": True,
        "first_closed_row_inspected": True,
        "day_31_qualifies": True,
'''
ADAPTER_REQUIRED_NEW = '''        "first_row_non_closed": True,
        "day_31_qualifies": True,
'''
ADAPTER_VALIDATION_OLD = '''    if mismatches:
        raise SubmissionCenterSourceError(
            "Submission Center verification failed: " + "; ".join(mismatches)
        )

    records = observed.get("qualifying_records")
'''
ADAPTER_VALIDATION_NEW = '''    if mismatches:
        raise SubmissionCenterSourceError(
            "Submission Center verification failed: " + "; ".join(mismatches)
        )

    closed_boundary = observed.get("first_closed_row_inspected") is True
    exhausted_boundary = observed.get("full_dataset_exhausted") is True
    if closed_boundary == exhausted_boundary:
        raise SubmissionCenterSourceError(
            "Submission Center verification failed: exactly one terminal boundary is required"
        )
    pager_total = observed.get("pager_total")
    rows_inspected = observed.get("rows_inspected_through_boundary")
    non_closed = observed.get("non_closed_rows_inspected")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (pager_total, rows_inspected, non_closed)
    ):
        raise SubmissionCenterSourceError(
            "Submission Center verification failed: boundary counts were invalid"
        )
    if exhausted_boundary:
        if rows_inspected != pager_total or non_closed != pager_total:
            raise SubmissionCenterSourceError(
                "Submission Center verification failed: exhausted pager did not reconcile"
            )
    elif rows_inspected != non_closed + 1:
        raise SubmissionCenterSourceError(
            "Submission Center verification failed: closed-row boundary did not reconcile"
        )

    records = observed.get("qualifying_records")
'''


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    old_count = source.count(old)
    new_count = source.count(new)
    if old_count == 0 and new_count == 1:
        return source
    if old_count != 1 or new_count != 0:
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
    # Live dedicated VM may already carry a newer equivalent range reader
    # (_read_page_rows) while still exposing the required terminal-boundary
    # markers. Treat that as already repaired instead of refusing.
    already_markers = (
        'full_dataset_exhausted = False',
        '"full_dataset_exhausted": full_dataset_exhausted',
        '"boundary_kind": "first_closed_row" if first_closed_page is not None else "pager_exhausted"',
        'start, end, _ = _pager_range(page)',
    )
    if all(original.count(marker) == 1 for marker in already_markers):
        if original.count(BROWSER_RANGE_OLD) == 0:
            return "already_repaired"
    updated = _replace_once(original, BROWSER_INIT_OLD, BROWSER_INIT_NEW, "browser-init")
    updated = _replace_once(updated, BROWSER_LOOP_OLD, BROWSER_LOOP_NEW, "browser-loop")
    updated = _replace_once(updated, BROWSER_RESULT_OLD, BROWSER_RESULT_NEW, "browser-result")
    updated = _replace_once(updated, BROWSER_RANGE_OLD, BROWSER_RANGE_NEW, "browser-range")
    if updated == original:
        return "already_repaired"
    _write_repaired(path, updated, ".pre-full-exhaustion-boundary")
    verified = path.read_text(encoding="utf-8")
    for required in (
        BROWSER_INIT_NEW,
        BROWSER_LOOP_NEW,
        BROWSER_RESULT_NEW,
        BROWSER_RANGE_NEW,
        '"full_dataset_exhausted": full_dataset_exhausted',
    ):
        if verified.count(required) != 1:
            raise RuntimeError("Submission Center browser boundary repair verification failed")
    return "repaired"


def repair_adapter(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    updated = _replace_once(
        original, ADAPTER_REQUIRED_OLD, ADAPTER_REQUIRED_NEW, "adapter-required"
    )
    updated = _replace_once(
        updated, ADAPTER_VALIDATION_OLD, ADAPTER_VALIDATION_NEW, "adapter-validation"
    )
    if updated == original:
        return "already_repaired"
    _write_repaired(path, updated, ".pre-full-exhaustion-boundary")
    verified = path.read_text(encoding="utf-8")
    for required in (ADAPTER_REQUIRED_NEW, ADAPTER_VALIDATION_NEW):
        if verified.count(required) != 1:
            raise RuntimeError("Submission Center adapter boundary repair verification failed")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-path", required=True)
    parser.add_argument("--adapter-path", required=True)
    args = parser.parse_args()
    print(f"browser={repair_browser(Path(args.browser_path))}")
    print(f"adapter={repair_adapter(Path(args.adapter_path))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
