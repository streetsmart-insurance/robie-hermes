#!/usr/bin/env python3
"""Read-only check of D01's Edit Policy page for validation state.

Context: after two authorized "Save & Continue Edit" activations produced no
navigation and no FormEntry URL, Carlo hypothesized the save failed
validation -- select#BillingType was blank (---Select---) in the pre-click
header capture, possibly because D01 was created by API with a minimal
payload.

This script performs ZERO writes. It loads the Edit URL in a new tab on the
existing CDP browser and reads, literally:
  - text of any .field-validation-error elements
  - text of any .validation-summary-errors container
  - ids of any [aria-invalid="true"] elements
  - every <select> on the page whose selected option reads ---Select---
  - the current value/text of select#BillingType specifically

It cannot reconstruct the transient post-click DOM from the earlier runs; it
reports the current state only. No control is ever activated.
"""

import argparse
import datetime
import json
import sys

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9222"
ENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83651751"

VALIDATION_JS = r"""
(() => {
  const out = {
    field_validation_errors: [],
    validation_summary_errors: "",
    aria_invalid_ids: [],
    blank_selects: [],
    billing_type: null,
  };
  document.querySelectorAll(".field-validation-error").forEach(el => {
    const t = (el.innerText || "").trim();
    if (t) out.field_validation_errors.push(t);
  });
  const sum = document.querySelector(".validation-summary-errors");
  if (sum) out.validation_summary_errors = (sum.innerText || "").trim();
  document.querySelectorAll('[aria-invalid="true"]').forEach(el => {
    out.aria_invalid_ids.push(el.id || el.name || el.tagName);
  });
  document.querySelectorAll("select").forEach(sel => {
    const opt = sel.options[sel.selectedIndex];
    const txt = opt ? (opt.text || "").trim() : "";
    if (txt === "---Select---") {
      out.blank_selects.push({id: sel.id || null, name: sel.name || null,
                              label: sel.id || sel.name || sel.tagName});
    }
    if (sel.id === "BillingType") {
      out.billing_type = {value: sel.value, text: txt,
                          disabled: sel.disabled,
                          visible: !!(sel.offsetWidth || sel.offsetHeight)};
    }
  });
  out.url = location.href;
  out.title = document.title;
  return out;
})()
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    result = {
        "capture": "check-edit-validation-state",
        "ok": False,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "requested_url": ENTRY_URL,
        "writes_performed": 0,
        "stopped_off_policy_path": False,
    }

    def dump():
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)

    def die(msg):
        result["error"] = msg
        dump()
        print("FATAL: " + msg, file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        try:
            contexts = browser.contexts
            if not contexts:
                die("no browser contexts on CDP")
            page = contexts[0].new_page()
            try:
                page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                result["landing_url"] = page.url
                result["landing_looks_authenticated"] = (
                    "/applicantportal/Policy/" in page.url
                    and "login" not in page.url.lower()
                )
                if not result["landing_looks_authenticated"]:
                    die("REFUSED_UNAUTHENTICATED")
                if "/applicantportal/Policy/" not in page.url:
                    result["stopped_off_policy_path"] = True
                    die("left /applicantportal/Policy/: " + page.url)
                result["validation"] = page.evaluate(VALIDATION_JS)
                result["ok"] = True
                dump()
                print(json.dumps(result["validation"], indent=2))
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        finally:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
