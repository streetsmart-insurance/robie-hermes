#!/usr/bin/env python3
"""Read-only HO coverage-modal DOM capture via Robie's EXISTING CDP session.

The FormEntry Edit page for TEST-HO-20260912-D01 (policyId 83651751,
applicant 220250093) exposes policy-header fields at page level, but no
Coverage A-F or deductible fields (proven by capture run 34694728948:
42 fields, zero coverage fields). The auto precedent
(research/ezlynx-locators/diagnose_coverages_exact.py) found coverage
fields inside a modal via document.querySelector('.repeaterEntryModal.in,
.modal') -- this script applies the same idea to Homeowners.

Connects to the already-running Robie browser on this box
(http://127.0.0.1:9222), opens a NEW tab (leaving Robie's pages untouched),
navigates to the FormEntry Edit URL, then:
  Phase A: sweep for already-present modal/dialog containers and dump every
           input / select / textarea inside them.
  Phase B: enumerate clickable elements whose text mentions coverage /
           deductible / dwelling / limit; click AT MOST ONE, and only if it
           is a modal trigger (button, data-toggle="modal", or a
           non-navigating href). Real navigation links are recorded, never
           clicked.
  Phase C: re-sweep modals after the click, plus a full page sweep.

Waits use page.wait_for_timeout only -- never networkidle.

READ-ONLY CONTRACT (enforced by design, not just intent):
  - No field-filling calls, no typing, no checking boxes, no option selection.
  - At most one click, on a modal trigger, logged explicitly.
  - If the click navigates away instead of opening a modal, the script stops
    interacting and records the new URL.
  - The tab opened by this script is closed at the end.
  - Nothing is ever submitted or saved.

Output: JSON to stdout and to the path given by --out.
Exit 0 always on a completed capture; exit 1 with a literal error JSON
when the capture itself cannot run (no CDP, no playwright, login wall, ...).
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

ENTRY_URL = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83651751"
CDP_URL = "http://127.0.0.1:9222"

FIELD_JS = r"""
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
function __labelText(el) {
  try {
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && l.innerText.trim()) return l.innerText.trim().slice(0, 200);
    }
    const w = el.closest('label');
    if (w && w.innerText.trim()) return w.innerText.trim().slice(0, 200);
    const al = el.getAttribute('aria-label');
    if (al) return al.slice(0, 200);
    const td = el.closest('td,th');
    if (td && td.parentElement) {
      const idx = Array.prototype.indexOf.call(td.parentElement.children, td);
      const table = td.closest('table');
      const hdr = table ? table.querySelector('thead th:nth-child(' + (idx + 1) + ')') : null;
      if (hdr && hdr.innerText.trim()) return 'col:' + hdr.innerText.trim().slice(0, 100);
    }
  } catch (e) {}
  return '';
}
function __dumpField(el, i) {
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
    extra.multiple = !!el.multiple;
    extra.all_options = Array.from(el.options).slice(0, 40).map(function(o) {
      return {value: o.value, text: (o.innerText || '').trim().slice(0, 80)};
    });
  } else {
    value = el.value;
  }
  if (typeof value === 'string') value = value.slice(0, 200);
  return {
    tag: tag, dom_index: i,
    id: el.id || null, name: el.getAttribute('name'),
    type: tag === 'input' ? (el.type || 'text').toLowerCase() : tag,
    placeholder: el.getAttribute('placeholder'),
    label: __labelText(el),
    value: value,
    visibility: __vis(el),
    disabled: !!el.disabled,
    readonly: !!el.readOnly,
    extra: extra,
  };
}
"""

MODAL_SWEEP_JS = r"""
() => {
  const sels = ['.repeaterEntryModal.in', '.modal.in', '.modal.show',
                 '[role="dialog"]', '.modal-dialog', '.modal-content'];
  const seen = new Set();
  const modals = [];
  sels.forEach(function(s) {
    document.querySelectorAll(s).forEach(function(m) {
      if (seen.has(m)) return;
      seen.add(m);
      const fields = [];
      m.querySelectorAll('input, select, textarea').forEach(function(el, i) {
        fields.push(__dumpField(el, i));
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

CANDIDATES_JS = r"""
() => {
  const out = [];
  const els = [];
  document.querySelectorAll('a, button, [data-toggle], [data-bs-toggle], [role="button"]').forEach(function(el) {
    const text = (el.innerText || '').trim().slice(0, 120);
    const aria = (el.getAttribute('aria-label') || '').slice(0, 120);
    const title = (el.getAttribute('title') || '').slice(0, 120);
    const hay = (text + ' ' + aria + ' ' + title).toLowerCase();
    if (!/cover|deduct|dwelling|\blimit/.test(hay)) return;
    els.push(el);
    out.push({
      index: els.length - 1,
      tag: el.tagName.toLowerCase(), id: el.id || null,
      text: text, aria_label: aria, title_attr: title,
      href: el.getAttribute('href'),
      data_toggle: el.getAttribute('data-toggle') || el.getAttribute('data-bs-toggle'),
      data_target: el.getAttribute('data-target') || el.getAttribute('data-bs-target'),
      visibility: __vis(el),
    });
  });
  window.__covCandidates = els;
  return out;
}
"""

PAGE_SWEEP_JS = r"""
() => {
  const fields = [];
  document.querySelectorAll('input, select, textarea').forEach(function(el, i) {
    fields.push(__dumpField(el, i));
  });
  return {url: location.href, title: document.title,
          field_count: fields.length, fields: fields};
}
"""


def die(msg, **kw):
    err = {"capture": "coverage-modal", "ok": False, "error": msg}
    err.update(kw)
    print(json.dumps(err, indent=2))
    sys.exit(1)


def is_safe_trigger(c):
    """A click target is safe only if it cannot navigate away."""
    if c.get("visibility") != "VISIBLE":
        return False
    if c.get("tag") == "button":
        return True
    if (c.get("data_toggle") or "").lower() == "modal":
        return True
    href = (c.get("href") or "").strip()
    if href in ("", "#") or href.startswith("#") or href.lower().startswith("javascript:"):
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only HO coverage-modal DOM capture")
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
        "capture": "coverage-modal",
        "ok": True,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "requested_url": args.url,
        "landing_url": None,
        "modal_sweep_before": None,
        "coverage_candidates": [],
        "click": {"clicked": False},
        "modal_sweep_after": None,
        "page_sweep_after": None,
        "modal_found": False,
        "modal_field_count": 0,
        "duplicates": [],
    }

    prelude = FIELD_JS + "\n"

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
            # Settle rendering with a fixed wait -- never networkidle.
            page.wait_for_timeout(3000)
            result["landing_url"] = page.url
            result["landing_looks_authenticated"] = looks_authenticated(page.url)
            if not result["landing_looks_authenticated"]:
                die("landing is not authenticated", landing_url=page.url,
                    detail="REFUSED_UNAUTHENTICATED")

            # Phase A: modal sweep as-is.
            try:
                result["modal_sweep_before"] = page.evaluate(prelude + MODAL_SWEEP_JS)
            except Exception as exc:
                result["modal_sweep_before"] = {"evaluate_error": f"{type(exc).__name__}: {exc}"}

            # Phase B: enumerate coverage-ish clickables.
            try:
                cands = page.evaluate(prelude + CANDIDATES_JS)
            except Exception as exc:
                cands = []
                result["candidates_error"] = f"{type(exc).__name__}: {exc}"
            result["coverage_candidates"] = cands

            target = next((c for c in cands if is_safe_trigger(c)), None)
            if target is None:
                result["click"] = {"clicked": False,
                                   "reason": "no safe modal trigger among candidates"}
            else:
                idx = target["index"]
                try:
                    page.evaluate("(i) => window.__covCandidates[i].click()", idx)
                    result["click"] = {"clicked": True, "target": target}
                    page.wait_for_timeout(3000)
                    if page.url != result["landing_url"]:
                        result["click"]["navigated_to"] = page.url
                        result["click"]["note"] = (
                            "click navigated instead of opening a modal; "
                            "stopping interaction")
                    else:
                        # Phase C: modal sweep after click + full page sweep.
                        try:
                            result["modal_sweep_after"] = page.evaluate(prelude + MODAL_SWEEP_JS)
                        except Exception as exc:
                            result["modal_sweep_after"] = {
                                "evaluate_error": f"{type(exc).__name__}: {exc}"}
                        try:
                            result["page_sweep_after"] = page.evaluate(prelude + PAGE_SWEEP_JS)
                        except Exception as exc:
                            result["page_sweep_after"] = {
                                "evaluate_error": f"{type(exc).__name__}: {exc}"}
                except Exception as exc:
                    result["click"] = {"clicked": True, "target": target,
                                       "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                page.close()
            except Exception:
                pass

    # Modal verdict: any modal container holding fields, before or after.
    total = 0
    for sweep in (result.get("modal_sweep_before"), result.get("modal_sweep_after")):
        if not sweep or not isinstance(sweep, dict):
            continue
        for m in sweep.get("modals", []):
            total += m.get("field_count", 0)
    result["modal_field_count"] = total
    result["modal_found"] = total > 0

    # Duplicate analysis across modal fields.
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
                     "mixed_visibility": len(viss) > 1,
                     "occurrences": v})

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps({k: result[k] for k in
                      ("capture", "ok", "captured_at", "landing_url",
                       "landing_looks_authenticated", "chosen_context",
                       "modal_found", "modal_field_count",
                       "coverage_candidates", "click")}, indent=2))
    print(f"\n... full dump written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
