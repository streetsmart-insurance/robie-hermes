#!/usr/bin/env python3
"""Proof driver for Dusty's orchestrated test (Carlo 2026-09-12).

Full build+test: search-first PolicyApi create with the gold payload, then
FormEntry coverages by literal label. Applicant 220250093 only. Never creates
TEST-HO-20260911-E01 by hand (the orchestrator refuses that policy number).

Usage:
  ROBIE_ENV=PRODUCTION EZLYNX_API_PROD_SECRET=projects/.../secrets/ezlynx-api-prod \\
    python3 scripts/proof_policy_setup.py --policy-number TEST-HO-20260912-P01 \\
      --effective-date 2026-10-02T00:00:00 --expiration-date 2027-10-02T00:00:00 \\
      --coverage "Dwelling=250000" --coverage "Other Structures=25000" \\
      [--browser]

--browser connects to the local CDP endpoint (ROBIE_BROWSER_CDP_URL,
default http://127.0.0.1:9222) for the FormEntry steps. Without it, only the
session + API steps run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FORBIDDEN_POLICY_NUMBERS = {"TEST-HO-20260911-E01"}


def parse_coverage(spec: str) -> tuple[str, str]:
    label, sep, value = spec.partition("=")
    if not sep or not label.strip():
        raise ValueError(f"bad --coverage spec {spec!r}; want Label=Value")
    return label.strip(), value.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--policy-number", required=True)
    ap.add_argument("--effective-date", required=True)
    ap.add_argument("--expiration-date", required=True)
    ap.add_argument("--applicant-id", default="220250093")
    ap.add_argument("--coverage", action="append", default=[],
                    help="Label=Value, repeatable")
    ap.add_argument("--browser", action="store_true",
                    help="run the FormEntry browser steps via local CDP")
    args = ap.parse_args()

    if args.policy_number.strip() in FORBIDDEN_POLICY_NUMBERS:
        print(json.dumps({
            "verdict": "REFUSED",
            "reason": "TEST-HO-20260911-E01 must never be created by hand",
        }))
        return 2

    from robie_job_engine.ezlynx_api import EzlynxApiClient, load_ezlynx_api_config
    from robie_job_engine.policy_setup_proof import run_proof

    coverages = dict(parse_coverage(c) for c in args.coverage)
    client = EzlynxApiClient(load_ezlynx_api_config())

    page = None
    if args.browser:
        from playwright.sync_api import sync_playwright

        cdp_url = os.environ.get("ROBIE_BROWSER_CDP_URL", "http://127.0.0.1:9222")
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

    report = run_proof(
        client=client,
        applicant_id=args.applicant_id,
        policy_number=args.policy_number.strip(),
        effective_date=args.effective_date,
        expiration_date=args.expiration_date,
        coverages=coverages or None,
        page=page,
    )
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
