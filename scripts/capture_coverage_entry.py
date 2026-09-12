#!/usr/bin/env python3
"""Read-only coverage-entry DISCOVERY via Robie's EXISTING CDP session.

The FormEntry Edit page for TEST-HO-20260912-D01 (policyId 83651751,
applicant 220250093) exposes policy-header fields at page level but no
Coverage A-F or deductible fields (proven by capture run 34694728948:
42 fields, zero coverage fields). The coverage-screen entry URL is never
guessed and never constructed here -- discovery only. This script finds
the control on the header screen that opens coverage entry and reports
its href / handler LITERALLY.
It does NOT follow it: the follow happens only after the literal report is
reviewed.

Connects to the already-running Robie browser on this box
(http://127.0.0.1:9222), opens a NEW tab (leaving Robie's pages untouched),
navigates to the FormEntry Edit URL, then:
  1. Enumerates every clickable control inside the policy/FormEntry region
     (account-level nav excluded), with literal href / onclick / data-*
     attributes.
  2. Flags coverage-entry candidates (cover|formentry in text/href/id) and
     reports them literally. NEVER clicks them in this run.
  3. Clicks [role=tab] / tab-like controls INSIDE the region only (the fix
     for run 34694728948, whose clicks hit account-level mat-tab-links and
     produced nine snapshots of /web/account/220250093/overview). The click
     loop aborts the moment the URL leaves /applicantportal/Policy/.
  4. Sweeps modal/dialog containers (.repeaterEntryModal.in, .modal, per the
     auto precedent) before and after, attributing each field to its
     container.

The URL actually landed on is recorded at EVERY step. If any step leaves
/applicantportal/Policy/, interaction stops immediately and the run reports.

Waits use page.wait_for_timeout exclusively.

READ-ONLY CONTRACT (enforced by design, not just intent):
  - No field-filling calls, no typing, no checking boxes, no option selection.
  - Coverage-entry candidates are reported, never clicked.
  - Region clicks are tabs/tab-like only, logged with URL before/after.
  - Nothing is ever submitted or saved; the tab is closed at the end.

Output: JSON to stdout and to the path given by --out.
Exit 0 always on a completed capture; exit 1 with a literal error JSON
when the capture itself cannot run (no CDP, no playwright, login wall, ...).
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

ENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83651751"
POLICY_PATH = "/applicantportal/Policy/"
CDP_URL = "http://127.0.0.1:9222"

PRELUDE_JS = r"""
function __vis(el) {
  try {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse')
      return 'HIDDEN';
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return 'HIDDEN';
    return 'VISIBLE';
  } catch (e) { return 'UNKNOWN'; }
}
function __inPolicyRegion(el) {
  try {
    if (el.closest('.mat-tab-nav-bar, header, nav, .global-nav, .top-nav')) return false;
    return !!el.closest('main, form, #content, .page-content, .policy-edit, [class*="policy"]');
  } catch (e) { return false; }
}
"""

ENUMERATE_JS = r"""
() => {
  const els = [];
  const out = [];
  document.querySelectorAll('a, button, [role="tab"], [role="button"], [data-toggle], [data-bs-toggle]').forEach(function(el) {
    if (!__inPolicyRegion(el)) return;
    els.push(el);
    out.push({
      index: els.length - 1,
      tag: el.tagName.toLowerCase(), id: el.id || null,
      text: (el.innerText || '').trim().slice(0, 120),
      href: el.getAttribute('href'),
      onclick: (el.getAttribute('onclick') || '').slice(0, 300),
      data_toggle: el.getAttribute('data-toggle') || el.getAttribute('data-bs-toggle'),
      data_target: el.getAttribute('data-target') || el.getAttribute('data-bs-target'),
      role: el.getAttribute('role'),
      aria_selected: el.getAttribute('aria-selected'),
      visibility: __vis(el),
    });
  });
  window.__regionClickables = els;
  return out;
}
"""

REGION_TABS_JS = r"""
() => {
  const out = [];
  (window.__regionClickables || []).forEach(function(el, i) {
    const isTab = el.getAttribute('role') === 'tab'
      || (el.getAttribute('data-toggle') || el.getAttribute('data-bs-toggle')) === 'tab';
    if (!isTab) return;
    out.push({index: i, id: el.id || null,
              text: (el.innerText || '').trim().slice(0, 80),
              selected: el.getAttribute('aria-selected'),
              visibility: __vis(el)});
  });
  return out;
}
"""

MODAL_SWEEP_JS = r"""
() => {
  const sels = ['.repeaterEntryModal.in', '.modal.in', '.modal.show',
                 '[role="dialog"]', '.modal-dialog', '.modal-content'];
  const seen = new Set();
  const modals = [];
  function dumpField(el, i) {
    const tag = el.tagName.toLowerCase();
    let value = null;
    const extra = {};
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      extra.input_type = t;
      value = (t === 'checkbox' || t === 'radio') ? el.checked : el.value;
    } else if (tag === 'select') {
      value = Array.from(el.selectedOptions).map(function(o) {
        return {value: o.value, text: (o.innerText || '').trim().slice(0, 120)};
      });
      extra.options_count = el.options.length;
      extra.all_options = Array.from(el.options).slice(0, 40).map(function(o) {
        return {value: o.value, text: (o.innerText || '').trim().slice(0, 80)};
      });
    } else { value = el.value; }
    if (typeof value === 'string') value = value.slice(0, 200);
    let label = '';
    try {
      if (el.id) {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l && l.innerText.trim()) label = l.innerText.trim().slice(0, 200);
      }
      if (!label) {
        const w = el.closest('label');
        if (w && w.innerText.trim()) label = w.innerText.trim().slice(0, 200);
      }
    } catch (e) {}
    return {tag: tag, dom_index: i, id: el.id || null,
            name: el.getAttribute('name'),
            type: tag === 'input' ? (el.type || 'text').toLowerCase() : tag,
            placeholder: el.getAttribute('placeholder'),
            label: label, value: value, visibility: __vis(el),
            disabled: !!el.disabled, readonly: !!el.readOnly, extra: extra};
  }
  sels.forEach(function(s) {
    document.querySelectorAll(s).forEach(function(m) {
      if (seen.has(m)) return;
      seen.add(m);
      const fields = [];
      m.querySelectorAll('input, select, textarea').forEach(function(el, i) {
        fields.push(dumpField(el, i));
      });
      let cls = '';
      try { cls = String(m.className && m.className.baseVal !== undefined ? m.className.baseVal : (m.className || '')); } catch (e) {}
      modals.push({
        matched_selector: s,
        tag: m.tagName.toLowerCase(), id: m.id || null,
        classes: cls.slice(0, 200),
        visibility: __vis(m),
        heading: (m.innerText || '').trim().slice(0, 200),
        field_count: fields.length, fields: fields,
      });
    });
  });
  return {url: location.href, title: document.title,
          modal_count: modals.length, modals: modals};
}
"""


def die(msg, **kw):
    err = {"capture": "coverage-entry", "ok": False, "error": msg}
    err.update(kw)
    print(json.dumps(err, indent=2))
    sys.exit(1)


def on_policy_path(url):
    return bool(url) and POLICY_PATH in url


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only coverage-entry discovery")
    ap.add_argument("--out", required=True, help="path to write dump JSON")
    ap.add_argument("--cdp", default=CDP_URL)
    ap.add_argument("--url", default=ENTRY_URL)
    args = ap.parse_args()

    if args.url != ENTRY_URL:
        die("refusing non-allowlisted URL", url=args.url)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        die("playwright not importable with this interpreter")

    result = {
        "capture": "coverage-entry",
        "ok": True,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "requested_url": args.url,
        "steps": [],
        "region_clickables": [],
        "coverage_entry_candidates": [],
        "followed_coverage_entry": False,
        "region_tab_clicks": [],
        "modal_sweep_before": None,
        "modal_sweep_after": None,
        "modal_found": False,
        "modal_field_count": 0,
        "stopped_off_policy_path": False,
        "duplicates": [],
    }

    def record_step(name, page):
        try:
            u = page.url
        except Exception:
            u = None
        result["steps"].append({"step": name, "url": u})
        return u

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp, timeout=15000)
        except Exception as exc:
            die("CDP connect failed", cdp=args.cdp,
                detail=f"{type(exc).__name__}: {exc}")
        contexts = browser.contexts
        if not contexts:
            die("CDP connected but browser has no contexts")
        inventory = []
        for ci, c in enumerate(contexts):
            for pi, p in enumerate(c.pages):
                try:
                    inventory.append({"context_index": ci, "page_index": pi,
                                      "url": p.url, "title": p.title()})
                except Exception:
                    pass
        result["inventory"] = inventory

        def looks_authenticated(u):
            return bool(u) and "ezlynx.com" in u and "/auth/account/login" not in u

        chosen = None
        for ci, c in enumerate(contexts):
            for p in c.pages:
                try:
                    if looks_authenticated(p.url):
                        chosen = ci
                        break
                except Exception:
                    pass
            if chosen is not None:
                break
        result["chosen_context"] = chosen
        if chosen is None:
            die("no authenticated context available", detail="REFUSED_UNAUTHENTICATED")
        ctx = contexts[chosen]
        page = ctx.new_page()
        try:
            # The single allowed navigation.
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    n = page.evaluate(
                        "() => document.querySelectorAll('input,select,textarea').length")
                    if n and n > 0:
                        break
                except Exception:
                    pass
                time.sleep(2)
            page.wait_for_timeout(3000)  # settle rendering.
            url = record_step("goto_edit", page)
            result["landing_url"] = url
            result["landing_looks_authenticated"] = looks_authenticated(url)
            if not result["landing_looks_authenticated"]:
                die("landing is not authenticated", landing_url=url,
                    detail="REFUSED_UNAUTHENTICATED")
            if not on_policy_path(url):
                result["stopped_off_policy_path"] = True
                die("landing left the policy path", landing_url=url)

            # 1. Enumerate region clickables with literal attributes.
            try:
                clickables = page.evaluate(PRELUDE_JS + ENUMERATE_JS)
            except Exception as exc:
                clickables = []
                result["enumerate_error"] = f"{type(exc).__name__}: {exc}"
            result["region_clickables"] = clickables
            record_step("enumerate_region_clickables", page)

            # 2. Coverage-entry candidates: reported literally, NEVER clicked.
            cov_re = re.compile(r"cover|formentry", re.I)
            cands = [c for c in clickables
                     if cov_re.search(c.get("text") or "")
                     or cov_re.search(c.get("href") or "")
                     or cov_re.search(c.get("id") or "")]
            for c in cands:
                c["will_not_click"] = True
            result["coverage_entry_candidates"] = cands
            result["followed_coverage_entry"] = False
            record_step("flag_coverage_entry_candidates", page)

            # 3. Modal sweep before region tab clicks.
            try:
                result["modal_sweep_before"] = page.evaluate(PRELUDE_JS + MODAL_SWEEP_JS)
            except Exception as exc:
                result["modal_sweep_before"] = {"evaluate_error": f"{type(exc).__name__}: {exc}"}
            record_step("modal_sweep_before", page)

            # 4. Region-scoped tab clicks only; abort if URL leaves policy path.
            try:
                region_tabs = page.evaluate(PRELUDE_JS + REGION_TABS_JS)
            except Exception:
                region_tabs = []
            for t in region_tabs:
                if t.get("selected") == "true":
                    continue
                if t.get("visibility") != "VISIBLE":
                    continue
                entry = {"tab": t["text"], "index": t["index"]}
                try:
                    url_before = page.url
                    page.evaluate("(i) => window.__regionClickables[i].click()", t["index"])
                    page.wait_for_timeout(2000)  # settle after click.
                    url_after = record_step(f"region_tab_click:{t['text']}", page)
                    entry["url_before"] = url_before
                    entry["url_after"] = url_after
                    if not on_policy_path(url_after):
                        entry["aborted"] = True
                        entry["note"] = ("URL left /applicantportal/Policy/; "
                                         "click loop stopped, no further interaction")
                        result["region_tab_clicks"].append(entry)
                        result["stopped_off_policy_path"] = True
                        break
                except Exception as exc:
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                    record_step(f"region_tab_click_error:{t['text']}", page)
                result["region_tab_clicks"].append(entry)
                if result["stopped_off_policy_path"]:
                    break

            # 5. Modal sweep after (only if still on the policy path).
            if not result["stopped_off_policy_path"]:
                try:
                    result["modal_sweep_after"] = page.evaluate(PRELUDE_JS + MODAL_SWEEP_JS)
                except Exception as exc:
                    result["modal_sweep_after"] = {
                        "evaluate_error": f"{type(exc).__name__}: {exc}"}
                record_step("modal_sweep_after", page)
        finally:
            try:
                page.close()
            except Exception:
                pass

    total = 0
    for sweep in (result.get("modal_sweep_before"), result.get("modal_sweep_after")):
        if not sweep or not isinstance(sweep, dict):
            continue
        for m in sweep.get("modals", []):
            total += m.get("field_count", 0)
    result["modal_field_count"] = total
    result["modal_found"] = total > 0

    seen_id = {}
    seen_name = {}
    for sweep_key in ("modal_sweep_before", "modal_sweep_after"):
        sweep = result.get(sweep_key) or {}
        for m in sweep.get("modals", []):
            ctx_label = f"{sweep_key}:{m.get('matched_selector')}"
            for fld in m.get("fields", []):
                if fld.get("id"):
                    seen_id.setdefault(fld["id"], []).append(
                        {"visibility": fld["visibility"], "modal": ctx_label,
                         "label": fld["label"]})
                if fld.get("name"):
                    seen_name.setdefault(fld["name"], []).append(
                        {"visibility": fld["visibility"], "modal": ctx_label,
                         "label": fld["label"]})
    for kind, seen in (("id", seen_id), ("name", seen_name)):
        for k, v in seen.items():
            if len(v) > 1:
                viss = {x["visibility"] for x in v}
                result["duplicates"].append(
                    {"kind": kind, "key": k, "count": len(v),
                     "mixed_visibility": len(viss) > 1, "occurrences": v})

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps({k: result[k] for k in
                      ("capture", "ok", "captured_at", "landing_url",
                       "landing_looks_authenticated", "chosen_context",
                       "steps", "coverage_entry_candidates",
                       "followed_coverage_entry", "region_tab_clicks",
                       "modal_found", "modal_field_count",
                       "stopped_off_policy_path")}, indent=2))
    print(f"\n... full dump written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
