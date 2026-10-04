#!/usr/bin/env python3
"""Read-only proof of the Ascend list pagination contract.

For each list endpoint: walk the raw pages and record only shape facts
(rows per page, which meta keys exist, the meta.next / next_cursor values,
duplicate ids, any total the vendor reports). Then run the production
``paginate`` and check it returns exactly the same set of ids. No record
content is printed. GET only.

  ROBIE_ENV=TEST PYTHONPATH=. python3 scripts/ascend_pagination_probe.py
  ROBIE_ENV=TEST PYTHONPATH=. python3 scripts/ascend_pagination_probe.py --page-size 1

A list with fewer rows than the page size fits on one page, which proves
nothing about page 2. ``--page-size 1`` (or 2) forces every list with two
or more rows across several pages. ``/v1/users`` is included because it is
the list proven multi-page in Production (25 per page). If the vendor
ignores page_size, the per-page row counts in the report show it.
Each endpoint reports ``multi_page_proven`` only when the walk really
crossed a page boundary and ``paginate`` returned the same ids.
"""

from __future__ import annotations

import json
import sys

from robie_job_engine.ascend_delivery_state import paginate
from robie_job_engine.ascend_sync import AscendApiClient

ENDPOINTS = ("/v1/cancelation_returns", "/v1/programs", "/v1/payouts", "/v1/invoices", "/v1/loans", "/v1/users")
PAGE_SIZE = 50
MAX_PAGES = 500


def raw_walk(api, path, page_size=PAGE_SIZE):
    pages, ids, dupes, query = [], [], 0, {"page_size": page_size}
    for _ in range(MAX_PAGES):
        page = api.get(path, query)
        data = page.get("data") if isinstance(page, dict) else None
        meta = (page.get("meta") or page.get("pagination") or {}) if isinstance(page, dict) else {}
        rows = data if isinstance(data, list) else []
        for row in rows:
            rid = row.get("id") if isinstance(row, dict) else None
            if rid in ids:
                dupes += 1
            ids.append(rid)
        pages.append({
            "rows": len(rows),
            "top_keys": sorted(k for k in page.keys() if k != "data") if isinstance(page, dict) else [],
            "meta_keys": sorted(meta.keys()) if isinstance(meta, dict) else [],
            "meta_next": meta.get("next") if isinstance(meta.get("next"), (int, type(None))) else "non-integer",
            "has_next_cursor": bool(meta.get("next_cursor") or page.get("next_cursor")),
            "total_reported": meta.get("total") or meta.get("total_count") or meta.get("count"),
        })
        nxt = meta.get("next")
        if isinstance(nxt, int) and not isinstance(nxt, bool):
            query = {"page_size": page_size, "page": nxt}
            continue
        cur = meta.get("next_cursor") or page.get("next_cursor")
        if cur:
            query = {"page_size": page_size, "starting_after": cur}
            continue
        break
    return pages, ids, dupes


def main(argv=None, api=None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    ap.add_argument("--endpoint", action="append", default=None)
    args = ap.parse_args(argv)
    page_size = max(1, args.page_size)
    api = api or AscendApiClient()
    report, ok = {"origin": api.origin, "page_size": page_size, "endpoints": {}}, True
    multi = []
    for path in args.endpoint or ENDPOINTS:
        entry = {}
        try:
            pages, ids, dupes = raw_walk(api, path, page_size)
            entry.update({"pages": len(pages), "rows": len(ids), "duplicate_ids": dupes,
                          "page_shapes": pages})
            try:
                via = paginate(api.get, path, page_size)
                same = sorted(map(str, (r.get("id") for r in via))) == sorted(map(str, ids))
                entry.update({"paginate_rows": len(via), "paginate_matches_raw_walk": same})
            except ValueError as exc:
                entry.update({"paginate_error": str(exc), "paginate_matches_raw_walk": False})
            totals = {p["total_reported"] for p in pages if p["total_reported"] is not None}
            entry["vendor_total_matches"] = (None if not totals else totals == {len(ids)})
            entry["ok"] = bool(entry.get("paginate_matches_raw_walk")) and dupes == 0 and entry["vendor_total_matches"] in (None, True)
            entry["multi_page_proven"] = entry["ok"] and len(pages) > 1
            if entry["multi_page_proven"]:
                multi.append(path)
        except Exception as exc:  # noqa: BLE001 - report, keep probing
            entry = {"ok": False, "error": type(exc).__name__}
        ok = ok and entry["ok"]
        report["endpoints"][path] = entry
    report["multi_page_proven_on"] = multi
    print(json.dumps(report, indent=2, default=str))
    print("PAGINATION PROOF PASSED" if ok else "PAGINATION PROOF FAILED")
    print("MULTI-PAGE PROVEN on: " + ", ".join(multi) if multi
          else "MULTI-PAGE NOT PROVEN: every list fit on one page; rerun with --page-size 1")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
