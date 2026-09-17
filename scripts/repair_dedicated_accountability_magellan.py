#!/usr/bin/env python3
"""Idempotently repair the dedicated accountability Magellan collector.

The installed standalone collector already authenticates through Secret Manager.
Its failure is caused by an immediate, redundant second dashboard navigation after
successful authentication. That second navigation can discard the just-created
session and redirect back to login. This repair removes only that exact block.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


OLD_BLOCK = """            _ensure_authenticated(page, context)
            page.goto("https://app.magellan.insure/dashboard", timeout=35000)
            page.wait_for_load_state("networkidle")
"""

NEW_BLOCK = """            _ensure_authenticated(page, context)
            page.wait_for_load_state("domcontentloaded")
"""

OLD_EXTRACT_START = '            calls_data = page.evaluate("""() => {\n'
NEW_EXTRACT_START = '            extract_calls_js = """() => {\n'
OLD_EXTRACT_END = '            }""")\n            wanted = '
NEW_EXTRACT_END = '            }"""\n            calls_data = page.evaluate(extract_calls_js)\n            wanted = '
OLD_PAGE_BLOCK = """            result["calls"] = [c for c in calls_data if wanted in c.get("date_time", "")]
            if not result["calls"]:
                raise RuntimeError(f"Magellan returned no rows for target date {target_date}")
"""
V1_PAGE_BLOCK = """            target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
            collected = []
            seen = set()
            older_boundary = False
            exhausted = False
            for _page_index in range(40):
                for call in calls_data:
                    date_text = str(call.get("date_time") or "")
                    match = re.search(r"\\b\\d{2}/\\d{2}/\\d{4}\\b", date_text)
                    if not match:
                        continue
                    call_day = datetime.strptime(match.group(0), "%m/%d/%Y").date()
                    if call_day < target_day:
                        older_boundary = True
                        continue
                    if call_day != target_day:
                        continue
                    identity = json.dumps(call, sort_keys=True)
                    if identity not in seen:
                        seen.add(identity)
                        collected.append(call)
                if older_boundary:
                    break
                next_button = page.locator("li.ant-pagination-next button, li.ant-pagination-next a")
                if next_button.count() != 1 or not next_button.is_enabled():
                    exhausted = True
                    break
                next_button.click()
                page.wait_for_timeout(750)
                calls_data = page.evaluate(extract_calls_js)
            else:
                next_button = page.locator("li.ant-pagination-next button, li.ant-pagination-next a")
                if next_button.count() == 1 and next_button.is_enabled():
                    raise RuntimeError("Magellan pagination limit reached before target-date boundary")
                exhausted = True
            if not older_boundary and not exhausted:
                raise RuntimeError("Magellan target-date boundary was not verified")
            result["calls"] = collected
            result["target_date"] = target_date
            result["pages_complete"] = True
"""

NEW_PAGE_BLOCK = """            target_day = datetime.strptime(target_date, "%Y-%m-%d").date()
            collected = []
            seen = set()
            older_boundary = False
            exhausted = False
            pages_reviewed = 0
            rows_inspected = 0
            for _page_index in range(40):
                pages_reviewed += 1
                rows_inspected += len(calls_data)
                for call in calls_data:
                    date_text = str(call.get("date_time") or "")
                    match = re.search(r"\\b\\d{2}/\\d{2}/\\d{4}\\b", date_text)
                    if not match:
                        continue
                    call_day = datetime.strptime(match.group(0), "%m/%d/%Y").date()
                    if call_day < target_day:
                        older_boundary = True
                        continue
                    if call_day != target_day:
                        continue
                    identity = json.dumps(call, sort_keys=True)
                    if identity not in seen:
                        seen.add(identity)
                        collected.append(call)
                if older_boundary:
                    break
                next_button = page.locator("li.ant-pagination-next button, li.ant-pagination-next a")
                if next_button.count() != 1 or not next_button.is_enabled():
                    exhausted = True
                    break
                next_button.click()
                page.wait_for_timeout(750)
                calls_data = page.evaluate(extract_calls_js)
            else:
                next_button = page.locator("li.ant-pagination-next button, li.ant-pagination-next a")
                if next_button.count() == 1 and next_button.is_enabled():
                    raise RuntimeError("Magellan pagination limit reached before target-date boundary")
                exhausted = True
            if not older_boundary and not exhausted:
                raise RuntimeError("Magellan target-date boundary was not verified")
            result["calls"] = collected
            result["target_date"] = target_date
            result["source_status"] = "available"
            result["pages_complete"] = True
            result["pages_reviewed"] = pages_reviewed
            result["rows_inspected"] = rows_inspected
            result["older_boundary_reached"] = older_boundary
            result["pagination_exhausted"] = exhausted
            result["records_on_target_date"] = len(collected)
"""


def repair(path: Path) -> str:
    path = path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    old_count = original.count(OLD_BLOCK)
    new_count = original.count(NEW_BLOCK)
    page_old = (
        original.count(OLD_EXTRACT_START),
        original.count(OLD_EXTRACT_END),
        original.count(OLD_PAGE_BLOCK),
    )
    page_v1 = original.count(V1_PAGE_BLOCK)
    page_new = (
        original.count(NEW_EXTRACT_START),
        original.count(NEW_EXTRACT_END),
        original.count(NEW_PAGE_BLOCK),
    )

    auth_ready = old_count == 0 and new_count == 1
    page_ready = page_old == (0, 0, 0) and page_v1 == 0 and page_new == (1, 1, 1)
    if auth_ready and page_ready:
        return "already_repaired"
    if old_count not in (0, 1) or new_count not in (0, 1) or old_count + new_count != 1:
        raise RuntimeError("refusing unexpected Magellan authentication source shape")
    legacy_ready = page_old == (1, 1, 1) and page_v1 == 0 and page_new == (0, 0, 0)
    v1_ready = page_old == (0, 0, 0) and page_v1 == 1 and page_new == (1, 1, 0)
    if not (legacy_ready or v1_ready or page_ready):
        raise RuntimeError(
            "refusing unexpected Magellan pagination source shape: "
            f"legacy={page_old}, v1={page_v1}, repaired={page_new}"
        )

    backup = path.with_suffix(path.suffix + ".pre-session-repair")
    if not backup.exists():
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)

    updated = original.replace(OLD_BLOCK, NEW_BLOCK, 1)
    if legacy_ready:
        updated = updated.replace(OLD_EXTRACT_START, NEW_EXTRACT_START, 1)
        updated = updated.replace(OLD_EXTRACT_END, NEW_EXTRACT_END, 1)
        updated = updated.replace(OLD_PAGE_BLOCK, NEW_PAGE_BLOCK, 1)
    elif v1_ready:
        updated = updated.replace(V1_PAGE_BLOCK, NEW_PAGE_BLOCK, 1)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    temporary.replace(path)

    verified = path.read_text(encoding="utf-8")
    if (
        OLD_BLOCK in verified
        or verified.count(NEW_BLOCK) != 1
        or any(
            value in verified
            for value in (
                OLD_EXTRACT_START,
                OLD_EXTRACT_END,
                OLD_PAGE_BLOCK,
                V1_PAGE_BLOCK,
            )
        )
        or verified.count(NEW_EXTRACT_START) != 1
        or verified.count(NEW_EXTRACT_END) != 1
        or verified.count(NEW_PAGE_BLOCK) != 1
    ):
        raise RuntimeError("Magellan source repair verification failed")
    return "repaired"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    print(repair(Path(args.path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
