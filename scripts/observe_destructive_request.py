#!/usr/bin/env python3
"""Capture the real EZLynx delete-transaction request signature -- once, from a human.

The guard ships with an EMPTY allowlist because guessing an endpoint pattern is
how a policy-level endpoint gets allowed by accident. This script watches an
existing Chrome/CDP session in observe-only mode while a HUMAN deletes one
duplicate pending RWL shell, and records every destructive-looking request.

    python3 scripts/observe_destructive_request.py --cdp http://127.0.0.1:9222

Then review the captured file, and paste the one correct entry into
robie_guard/guard_config.json with approved_by / approved_at filled in.

This script never clicks anything. It only listens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

DESTRUCTIVE_HINTS = ("delete", "remove", "void", "detach", "discard", "purge", "destroy")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--out", default="observed_destructive_requests.json")
    ap.add_argument("--seconds", type=int, default=300)
    args = ap.parse_args()

    captured: list[dict] = []

    def on_request(request) -> None:
        url = request.url or ""
        method = (request.method or "").upper()
        looks = method == "DELETE" or any(h in url.lower() for h in DESTRUCTIVE_HINTS)
        if not looks:
            return
        post = None
        try:
            post = request.post_data
        except Exception:  # noqa: BLE001
            pass
        captured.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "url": url,
            "resource_type": request.resource_type,
            "post_data": (post or "")[:2000],
            "suggested_url_pattern": None,
            "approved_by": None,
            "approved_at": None,
        })
        print(f"  captured  {method} {url}")

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(args.cdp)
        contexts = browser.contexts or []
        if not contexts:
            raise SystemExit("no browser context on that CDP endpoint")
        for ctx in contexts:
            ctx.on("request", on_request)
            for page in ctx.pages:
                page.on("request", on_request)

        print(f"Observing for {args.seconds}s. Perform ONE duplicate-shell deletion by hand now.")
        print("Nothing is being clicked by this script.\n")
        await asyncio.sleep(args.seconds)

    Path(args.out).write_text(json.dumps({"observed": captured}, indent=2))
    print(f"\n{len(captured)} destructive-looking request(s) written to {args.out}")
    print("Review it, pick the ONE that removed the transaction, and add it to")
    print("robie_guard/guard_config.json under allowed_destructive_requests with")
    print("approved_by and approved_at set. An unapproved entry is ignored.")


if __name__ == "__main__":
    asyncio.run(main())
