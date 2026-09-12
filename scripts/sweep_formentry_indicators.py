#!/usr/bin/env python3
"""Read-only sweep for a minted D01 FormEntry. No writes of any kind.

The one approved mint click (run 34697459460) fired but produced no
navigation, so no formEntryId was captured. This script performs ZERO
writes and only reads:

1. Inventories every open tab/page on the CDP browser and scans each URL
   for the FormEntry shape /applicantportal/Policy/83651751/FormEntry/
   Index/<id> (the click may have opened the FormEntry in a new tab that
   the mint script never inventoried).
2. Navigates once to the D01 Edit URL, verifies the session is
   authenticated, and sweeps the DOM for FormEntry indicators: anchors
   whose href contains "FormEntry", visible text mentioning "FormEntry",
   and APE/application links.

No clicks, no fills, no typing, no checks, no option selection.
"""

import argparse
import datetime
import json
import re
import sys

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9222"
ENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83651751"
FORMENTRY_URL_RE = re.compile(r"/applicantportal/Policy/83651751/FormEntry/Index/(\d+)")

INDICATORS_JS = r"""
() => {
  function vis(el) {
    try {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse') return 'HIDDEN';
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return 'HIDDEN';
      return 'VISIBLE';
    } catch (e) { return 'UNKNOWN'; }
  }
  const anchors = [];
  document.querySelectorAll('a[href]').forEach(function(a) {
    const href = a.getAttribute('href') || '';
    if (/formentry/i.test(href)) {
      anchors.push({href: href.slice(0, 300),
                    text: (a.innerText || '').trim().slice(0, 160),
                    visibility: vis(a)});
    }
  });
  const mentions = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  const seen = new Set();
  while ((node = walker.nextNode()) && mentions.length < 40) {
    const t = (node.nodeValue || '').trim();
    if (/formentry/i.test(t) && t.length < 300 && !seen.has(t)) {
      seen.add(t);
      let el = node.parentElement;
      mentions.push({text: t.slice(0, 200),
                     tag: el ? el.tagName.toLowerCase() : null,
                     visibility: el ? vis(el) : 'UNKNOWN'});
    }
  }
  const appLinks = [];
  document.querySelectorAll('a[href], button').forEach(function(el) {
    const t = ((el.innerText || '') + ' ' + (el.getAttribute('href') || '')).trim();
    if (/(\bAPE\b|application)/i.test(t) && appLinks.length < 40) {
      appLinks.push({tag: el.tagName.toLowerCase(),
                     text: (el.innerText || '').trim().slice(0, 160),
                     href: (el.getAttribute('href') || '').slice(0, 300),
                     visibility: vis(el)});
    }
  });
  return {url: location.href, title: document.title,
          formentry_anchors: anchors, formentry_mentions: mentions,
          ape_app_links: appLinks};
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    result = {
        "capture": "sweep-formentry-indicators",
        "ok": False,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "writes_performed": 0,
        "stopped_off_policy_path": False,
        "steps": [],
    }

    def dump():
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)

    def die(msg):
        result["error"] = msg
        dump()
        print("FATAL: " + msg, file=sys.stderr)
        sys.exit(1)

    def step(name, url):
        result["steps"].append({"step": name, "url": url})
        if "/applicantportal/Policy/" not in url:
            result["stopped_off_policy_path"] = True
            die("left /applicantportal/Policy/ at step " + name + ": " + url)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        try:
            # ---- 1. inventory every open tab; scan URLs for FormEntry ----
            inv = []
            found_in_tabs = []
            for ci, ctx in enumerate(browser.contexts):
                for pi, p in enumerate(ctx.pages):
                    try:
                        u, t = p.url, p.title()
                    except Exception:
                        u, t = "?", "?"
                    inv.append({"context_index": ci, "page_index": pi,
                                "url": u, "title": t})
                    m = FORMENTRY_URL_RE.search(u or "")
                    if m:
                        found_in_tabs.append({"context_index": ci,
                                              "page_index": pi,
                                              "url": u,
                                              "formentry_id": m.group(1)})
            result["tab_inventory"] = inv
            result["formentry_found_in_open_tabs"] = found_in_tabs

            # ---- 2. single read-only navigation to the Edit page ----
            ctx = browser.contexts[0]
            page = ctx.new_page()
            try:
                page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                step("goto_edit", page.url)
                result["landing_url"] = page.url
                result["landing_looks_authenticated"] = (
                    "/applicantportal/Policy/" in page.url
                    and "login" not in page.url.lower()
                )
                if not result["landing_looks_authenticated"]:
                    die("REFUSED_UNAUTHENTICATED")

                # ---- 3. DOM sweep for FormEntry indicators (read-only) ----
                data = page.evaluate(INDICATORS_JS)
                result["indicators"] = data
                step("indicators", page.url)
            finally:
                page.close()

            result["ok"] = True
            dump()
            print("OK")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
