#!/usr/bin/env python3
"""Retry the D01 FormEntry mint with ONE more approved write, then capture Coverages read-only.

Background: the first approved mint (run 34697459460, 2026-09-12) fired the
one authorized activation of "Save & Continue Edit" on D01's Edit Policy
header screen, but the page URL never changed within 60 seconds, so no
formEntryId was captured and whether a FormEntry was minted is UNVERIFIED.
Carlo authorized exactly one more activation on 2026-09-12.

What this run does differently from the first:
  1. Before anything else, it inventories EVERY open tab on the CDP browser
     and scans each URL for the FormEntry shape. The first run never looked
     at other tabs -- if the mint opened the FormEntry in a new tab, the
     first run missed it and this run must not mint twice.
  2. If a FormEntry URL is already open in any tab, the write is SKIPPED
     (write_occurred=false, write_skipped_reason recorded) and the run
     proceeds read-only on that tab.
  3. Otherwise it performs the ONE write exactly like the first run: three
     runtime preconditions (#PolicyNumber TEST-HO-20260912-D01, #ApplicantID
     220250093, #PolicyMasterID 83651751), then activates
     "Save & Continue Edit" resolved by EXACT accessible name.
  4. After the activation it scans the page AND every open tab for the
     FormEntry URL shape, then records the formEntryId literally.
  5. On the FormEntry page it activates the Coverages tab (exact accessible
     name) and sweeps the Coverages fields plus modal containers with
     per-container attribution. Only tabs this run opened are closed.

The dump states plainly whether a write occurred. A write that isn't
recorded as a write is the failure mode this script exists to avoid.

Waits use page.wait_for_timeout and wait_for_url only.

SINGLE-WRITE CONTRACT (enforced by the workflow's gate greps, blunt style):
  - No field-filling, no typing, no checking boxes, no option selection.
  - Exactly one click call site; the mint target is always resolved by exact
    accessible name, never by position or CSS. The same call site is reused
    for the read-only Coverages tab activation.
  - The names of the other header save controls appear nowhere in this file,
    so the one click can never be retargeted at them by a future edit.
"""

import argparse
import datetime
import json
import re
import sys
import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9222"
ENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83651751"
FORMENTRY_URL_RE = re.compile(r"/applicantportal/Policy/83651751/FormEntry/Index/(\d+)")

EXPECT = {
    "#PolicyNumber": "TEST-HO-20260912-D01",
    "#ApplicantID": "220250093",
    "#PolicyMasterID": "83651751",
}

COVERAGES_JS = r"""

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
  function labelFor(el) {
    try {
      if (el.labels && el.labels.length) {
        const t = (el.labels[0].innerText || '').trim();
        if (t) return t.slice(0, 160);
      }
      const al = el.getAttribute('aria-label');
      if (al && al.trim()) return al.trim().slice(0, 160);
      const lb = el.getAttribute('aria-labelledby');
      if (lb) {
        const t = lb.split(/\s+/).map(function(id) {
          const n = document.getElementById(id);
          return n ? (n.innerText || '').trim() : '';
        }).join(' ').trim();
        if (t) return t.slice(0, 160);
      }
      const row = el.closest('tr, .row, .form-group');
      if (row) {
        const cell = row.querySelector('th, td, .col-form-label, label');
        if (cell) {
          const t = (cell.innerText || '').trim();
          if (t && t.length < 200) return t.slice(0, 160);
        }
      }
    } catch (e) {}
    return null;
  }
  function containerOf(el) {
    try {
      const m = el.closest('.repeaterEntryModal.in, .modal.in, .modal.show, [role="dialog"], .modal-dialog, .modal-content');
      if (m) return (m.id ? '#' + m.id : String(m.className).slice(0, 80));
    } catch (e) {}
    return 'page';
  }
  const fields = [];
  document.querySelectorAll('input, select, textarea').forEach(function(el, i) {
    let val = null;
    let checked = null;
    try {
      const t = (el.getAttribute('type') || '').toLowerCase();
      if (t === 'checkbox' || t === 'radio') { checked = !!el.checked; }
      else { val = String(el.value).slice(0, 200); }
    } catch (e) {}
    fields.push({
      index: i,
      tag: el.tagName.toLowerCase(),
      input_type: el.getAttribute('type'),
      id: el.id || null,
      name: el.getAttribute('name'),
      label: labelFor(el),
      value: val,
      checked: checked,
      visibility: vis(el),
      container: containerOf(el),
    });
  });
  const tabs = [];
  document.querySelectorAll('[role="tab"]').forEach(function(el) {
    tabs.push({ text: (el.innerText || '').trim().slice(0, 80),
                selected: el.getAttribute('aria-selected') });
  });
  return { url: location.href, title: document.title,
           tab_strip: tabs, field_count: fields.length, fields: fields };
}
"""

MODAL_SWEEP_JS = r"""

() => {
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

def click_by_exact_name(page, role, name, timeout=30000):
    # THE single click call site in this script. The target is always
    # resolved by exact accessible name -- never by position or CSS.
    page.get_by_role(role, name=name, exact=True).click(timeout=timeout)


def read_field(page, selector):
    loc = page.locator(selector)
    try:
        v = loc.input_value(timeout=10000)
        if v and v.strip():
            return v.strip()
    except Exception:
        pass
    try:
        return (loc.inner_text(timeout=10000) or "").strip()
    except Exception:
        return ""



def tab_inventory(browser):
    inv = []
    for ci, ctx in enumerate(browser.contexts):
        for pi, p in enumerate(ctx.pages):
            try:
                u, t = p.url, p.title()
            except Exception:
                u, t = "?", "?"
            inv.append({"context_index": ci, "page_index": pi,
                        "url": u, "title": t})
    return inv


def find_formentry_page(browser):
    """Return (page|None, formentry_id|None) scanning every open tab."""
    for ctx in browser.contexts:
        for p in ctx.pages:
            try:
                u = p.url
            except Exception:
                continue
            m = FORMENTRY_URL_RE.search(u or "")
            if m:
                return p, m.group(1)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    result = {
        "capture": "mint-formentry-retry-capture-coverages",
        "ok": False,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "requested_url": ENTRY_URL,
        "write_occurred": False,
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
        if "/applicantportal/Policy/" not in (url or ""):
            result["stopped_off_policy_path"] = True
            die("left /applicantportal/Policy/ at step " + name + ": " + url)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        owned_page = None
        try:
            contexts = browser.contexts
            if not contexts:
                die("no browser contexts on CDP")

            # ---- pre-click scan: is a FormEntry already open in any tab? ----
            fe_page, fe_id = find_formentry_page(browser)
            result["pre_click_tab_inventory"] = tab_inventory(browser)
            if fe_page is not None:
                result["write_skipped_reason"] = "formentry_already_open_in_tab"
                result["formentry_url_matched"] = True
                result["formentry_id"] = fe_id
                result["formentry_found_in"] = "open_tab"
                step("formentry_already_open", fe_page.url)
            else:
                ctx = contexts[0]
                result["chosen_context"] = 0
                page = ctx.new_page()
                owned_page = page
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

                # ---- runtime preconditions: hard stop before the write ----
                pre = {}
                all_pass = True
                for sel, want in EXPECT.items():
                    got = read_field(page, sel)
                    ok = (got == want)
                    pre[sel] = {"expected": want, "actual": got, "pass": ok}
                    if not ok:
                        all_pass = False
                result["preconditions"] = pre
                step("preconditions", page.url)
                if not all_pass:
                    result["abort_reason"] = "precondition_mismatch"
                    die("precondition mismatch; no click performed")

                # ---- THE ONE WRITE ----
                url_before = page.url
                click_by_exact_name(page, "button", "Save & Continue Edit")
                try:
                    page.wait_for_url(FORMENTRY_URL_RE, timeout=60000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                url_after = page.url
                result["write_occurred"] = True
                result["write_action"] = {
                    "clicked": "Save & Continue Edit",
                    "match": "exact accessible name",
                    "url_before": url_before,
                    "url_after": url_after,
                }
                step("write_click", page.url)

                # ---- post-click scan: the page AND every open tab ----
                result["post_click_tab_inventory"] = tab_inventory(browser)
                fe_page, fe_id = find_formentry_page(browser)
                if fe_page is not None:
                    result["formentry_found_in"] = (
                        "original_page" if fe_page is page else "open_tab")
                else:
                    m = FORMENTRY_URL_RE.search(url_after or "")
                    if m:
                        fe_page, fe_id = page, m.group(1)
                        result["formentry_found_in"] = "original_page"
                if fe_page is None:
                    result["formentry_url_matched"] = False
                    result["formentry_id"] = None
                    die("after the write no FormEntry URL was found on the "
                        "page or any open tab; last page URL: " + url_after)
                result["formentry_url_matched"] = True
                result["formentry_id"] = fe_id
                step("assert_formentry_url", fe_page.url)

            # ---- Coverages tab, exact accessible name (read-only) ----
            tab_result = {"activated": False, "role": None, "error": None}
            for role in ("tab", "button", "link"):
                try:
                    click_by_exact_name(fe_page, role, "Coverages", timeout=10000)
                    tab_result["activated"] = True
                    tab_result["role"] = role
                    break
                except Exception as exc:
                    tab_result["error"] = "%s: %s" % (
                        type(exc).__name__, str(exc)[:160])
            result["coverages_tab"] = tab_result
            fe_page.wait_for_timeout(2000)
            step("activate_coverages_tab", fe_page.url)

            cov = fe_page.evaluate(COVERAGES_JS)
            cov["step_url"] = fe_page.url
            result["coverages"] = cov
            step("sweep_coverages", fe_page.url)

            modal = fe_page.evaluate(MODAL_SWEEP_JS)
            result["modal_sweep"] = modal
            result["modal_found"] = bool(modal.get("modal_count"))
            result["modal_field_count"] = sum(
                len(x.get("fields", [])) for x in modal.get("modals", []))
            step("modal_sweep", fe_page.url)

            result["ok"] = True
            dump()
            print(json.dumps({
                "write_occurred": result["write_occurred"],
                "write_skipped_reason": result.get("write_skipped_reason"),
                "formentry_id": result["formentry_id"],
                "formentry_found_in": result.get("formentry_found_in"),
                "coverages_tab": tab_result,
                "coverages_field_count": cov.get("field_count"),
                "modal_found": result["modal_found"],
            }, indent=2))
        finally:
            try:
                if owned_page is not None:
                    owned_page.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
