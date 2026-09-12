#!/usr/bin/env python3
"""Mint the FormEntry for TEST-HO-20260912-D01 with ONE approved write, then capture Coverages read-only.

The Edit Policy header screen for TEST-HO-20260912-D01 (policyId 83651751,
applicant 220250093) exposes policy-header fields but no coverage fields:
D01 has never been through APE, so no FormEntry exists yet, and the
coverage screen cannot be reached from that page by any read-only control
(proven by run 34696287868, whose clean enumeration found zero
coverage-entry candidates). The green "Save & Continue Edit" control on the
header screen mints the FormEntry; the page then lands on
  /applicantportal/Policy/{policyId}/FormEntry/Index/{formEntryId}
(the training video shows policy 33322876 minting formEntryId 164253324,
and the FormEntry page carrying a tab strip: Insured Information |
Dwelling Info | Coverages | Underwriting | Additional Interest).

This script performs exactly ONE write -- activating "Save & Continue Edit"
resolved by EXACT accessible name -- and only after three runtime
preconditions pass: #PolicyNumber reads TEST-HO-20260912-D01, #ApplicantID
reads 220250093, #PolicyMasterID reads 83651751. Any mismatch aborts with no
click and a red run.

After the click the script asserts the FormEntry URL shape and records the
formEntryId literally in the dump (future runs navigate straight there
read-only and never activate the mint control again), activates the
Coverages tab by exact accessible name, then sweeps the Coverages fields
and the modal containers (.repeaterEntryModal.in, .modal) with
per-container attribution. No other header control is ever activated; the
tab is closed at the end.

The dump states plainly that a write occurred: what was activated and the
URL before and after. A write that isn't recorded as a write is the failure
mode this script exists to avoid.

Waits use page.wait_for_timeout and wait_for_url only.

SINGLE-WRITE CONTRACT (enforced by the workflow's gate greps, blunt style):
  - No field-filling, no typing, no checking boxes, no option selection.
  - Exactly one click call site; the target is always resolved by exact
    accessible name, never by position or CSS.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    result = {
        "capture": "mint-formentry-capture-coverages",
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

    def step(name, page):
        result["steps"].append({"step": name, "url": page.url})
        if "/applicantportal/Policy/" not in page.url:
            result["stopped_off_policy_path"] = True
            die("left /applicantportal/Policy/ at step " + name + ": " + page.url)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        try:
            contexts = browser.contexts
            if not contexts:
                die("no browser contexts on CDP")
            inv = []
            for ci, ctx in enumerate(contexts):
                for pi, p in enumerate(ctx.pages):
                    try:
                        inv.append({"context_index": ci, "page_index": pi,
                                    "url": p.url, "title": p.title()})
                    except Exception:
                        inv.append({"context_index": ci, "page_index": pi,
                                    "url": "?", "title": "?"})
            result["inventory"] = inv
            ctx = contexts[0]
            result["chosen_context"] = 0
            page = ctx.new_page()
            try:
                page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                step("goto_edit", page)
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
                step("preconditions", page)
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
                step("write_click", page)

                m = FORMENTRY_URL_RE.search(url_after)
                if not m:
                    result["formentry_url_matched"] = False
                    result["formentry_id"] = None
                    die("after the write the URL did not match the FormEntry shape: "
                        + url_after)
                result["formentry_url_matched"] = True
                result["formentry_id"] = m.group(1)
                step("assert_formentry_url", page)

                # ---- Coverages tab, exact accessible name ----
                tab_result = {"activated": False, "role": None, "error": None}
                for role in ("tab", "button", "link"):
                    try:
                        click_by_exact_name(page, role, "Coverages", timeout=10000)
                        tab_result["activated"] = True
                        tab_result["role"] = role
                        break
                    except Exception as exc:
                        tab_result["error"] = "%s: %s" % (
                            type(exc).__name__, str(exc)[:160])
                result["coverages_tab"] = tab_result
                page.wait_for_timeout(2000)
                step("activate_coverages_tab", page)

                cov = page.evaluate(COVERAGES_JS)
                cov["step_url"] = page.url
                result["coverages"] = cov
                step("sweep_coverages", page)

                modal = page.evaluate(MODAL_SWEEP_JS)
                result["modal_sweep"] = modal
                result["modal_found"] = bool(modal.get("modal_count"))
                result["modal_field_count"] = sum(
                    len(x.get("fields", [])) for x in modal.get("modals", []))
                step("modal_sweep", page)

                result["ok"] = True
                dump()
                print(json.dumps({
                    "write_occurred": True,
                    "formentry_id": result["formentry_id"],
                    "url_after": url_after,
                    "coverages_tab": tab_result,
                    "coverages_field_count": cov.get("field_count"),
                    "modal_found": result["modal_found"],
                }, indent=2))
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
