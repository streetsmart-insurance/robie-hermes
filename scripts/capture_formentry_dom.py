#!/usr/bin/env python3
"""Read-only FormEntry DOM capture via Robie's EXISTING CDP session.

Connects to the already-running Robie browser on this box
(http://127.0.0.1:9222), opens a NEW tab (leaving Robie's pages untouched),
navigates to either the D01 Edit URL (policyId 83651751) or a literal
allowlisted FormEntry URL
(/applicantportal/Policy/83651751/FormEntry/Index/<id>), and dumps every
input / select / textarea: id, name, type, placeholder, label, value,
VISIBLE/HIDDEN, iframe, tab/section. Optional ``--prefer-tab Coverages``
clicks that tab first. Select ``all_options`` is uncapped.

READ-ONLY CONTRACT (enforced by design, not just intent):
  - No field-filling calls, no typing, no checking boxes, no option selection.
  - The only clicks are on [role=tab] elements, logged explicitly.
  - The tab opened by this script is closed at the end.
  - Nothing is ever submitted or saved.
  - FormEntry ids are never invented.

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
FORMENTRY_URL_RE = re.compile(
    r"^https://app\.ezlynx\.com/applicantportal/Policy/83651751/FormEntry/Index/\d+(?:[/?#].*)?$"
)
CDP_URL = "http://127.0.0.1:9222"

DUMP_JS = r"""
() => {
  function vis(el) {
    try {
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse')
        return 'HIDDEN';
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return 'HIDDEN';
      return 'VISIBLE';
    } catch (e) { return 'UNKNOWN'; }
  }
  function labelText(el) {
    try {
      if (el.id) {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l && l.innerText.trim()) return l.innerText.trim().slice(0, 200);
      }
      const w = el.closest('label');
      if (w && w.innerText.trim()) return w.innerText.trim().slice(0, 200);
      const al = el.getAttribute('aria-label');
      if (al) return al.slice(0, 200);
      const lb = el.getAttribute('aria-labelledby');
      if (lb) {
        const t = lb.split(/\s+/).map(function(id) {
          const n = document.getElementById(id);
          return n ? n.innerText.trim() : '';
        }).filter(Boolean).join(' ');
        if (t) return t.slice(0, 200);
      }
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
  function section(el) {
    try {
      let n = el;
      while (n && n !== document.body) {
        n = n.parentElement;
        if (!n || !n.getAttribute) break;
        if (n.getAttribute('role') === 'tabpanel') {
          const lab = n.getAttribute('aria-label') || n.getAttribute('aria-labelledby') || n.id || '';
          return 'tabpanel:' + String(lab).slice(0, 120);
        }
        const h = n.querySelector(':scope > h1, :scope > h2, :scope > h3, :scope > h4');
        if (h && h.innerText.trim()) return 'heading:' + h.innerText.trim().slice(0, 120);
        const leg = n.tagName === 'FIELDSET' ? n.querySelector(':scope > legend') : null;
        if (leg && leg.innerText.trim()) return 'fieldset:' + leg.innerText.trim().slice(0, 120);
      }
    } catch (e) {}
    return '';
  }
  const fields = [];
  document.querySelectorAll('input, select, textarea').forEach(function(el, i) {
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
      extra.all_options = Array.from(el.options).map(function(o) {
        return {value: o.value, text: (o.innerText || '').trim().slice(0, 200)};
      });
    } else {
      value = el.value;
    }
    if (typeof value === 'string') value = value.slice(0, 200);
    fields.push({
      tag: tag, dom_index: i,
      id: el.id || null, name: el.getAttribute('name'),
      type: tag === 'input' ? (el.type || 'text').toLowerCase() : tag,
      placeholder: el.getAttribute('placeholder'),
      label: labelText(el),
      value: value,
      visibility: vis(el),
      disabled: !!el.disabled,
      readonly: !!el.readOnly,
      section: section(el),
      extra: extra,
    });
  });
  const tabs = [];
  document.querySelectorAll('[role="tab"]').forEach(function(el) {
    tabs.push({
      id: el.id || null,
      text: (el.innerText || '').trim().slice(0, 120),
      selected: el.getAttribute('aria-selected'),
      controls: el.getAttribute('aria-controls'),
      visibility: vis(el),
    });
  });
  const tabish = [];
  document.querySelectorAll('.nav-tabs a, .nav-tabs button, [data-toggle="tab"], [data-bs-toggle="tab"]').forEach(function(el) {
    if (el.getAttribute('role') === 'tab') return;
    tabish.push({
      tag: el.tagName.toLowerCase(), id: el.id || null,
      text: (el.innerText || '').trim().slice(0, 120),
      visibility: vis(el),
    });
  });
  return {
    url: location.href, title: document.title,
    field_count: fields.length, fields: fields,
    tabs: tabs, tablike_not_clicked: tabish,
  };
}
"""


def die(msg, **kw):
    err = {"capture": "formentry-dom", "ok": False, "error": msg}
    err.update(kw)
    print(json.dumps(err, indent=2))
    sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only FormEntry DOM capture")
    ap.add_argument("--out", required=True, help="path to write dump JSON")
    ap.add_argument("--cdp", default=CDP_URL)
    ap.add_argument("--url", default=ENTRY_URL)
    ap.add_argument(
        "--prefer-tab",
        default="",
        help="If set (e.g. Coverages), click that [role=tab] first before dumps",
    )
    args = ap.parse_args()

    allowlisted = args.url == ENTRY_URL or bool(FORMENTRY_URL_RE.match(args.url))
    if not allowlisted:
        die("refusing non-allowlisted URL", url=args.url)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        die("playwright not importable with this interpreter")

    result = {
        "capture": "formentry-dom",
        "ok": True,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "requested_url": args.url,
        "prefer_tab": args.prefer_tab or None,
        "landing_url": None,
        "clicks": [],
        "frames": [],
        "duplicates": [],
    }

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
        ctx = contexts[chosen] if chosen is not None else contexts[0]
        page = ctx.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            # let the server redirect settle and the form render
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    n = page.evaluate(
                        "() => document.querySelectorAll("
                        "'input,select,textarea').length")
                    if n and n > 0:
                        break
                except Exception:
                    pass
                time.sleep(2)
            time.sleep(3)
            result["landing_url"] = page.url
            result["landing_looks_authenticated"] = looks_authenticated(page.url)

            # Optional: open Coverages (or other) tab first for reference dumps.
            prefer = (args.prefer_tab or "").strip()
            if prefer and result["landing_looks_authenticated"]:
                try:
                    tab = page.get_by_role("tab", name=prefer, exact=True)
                    if tab.count() == 1:
                        before = page.url
                        tab.click(timeout=8000)
                        time.sleep(2)
                        result["clicks"].append({
                            "clicked_tab": prefer,
                            "selector": f'role=tab[name="{prefer}"]',
                            "url_before": before,
                            "url_after": page.url,
                        })
                    else:
                        result["clicks"].append({
                            "clicked_tab": prefer,
                            "error": f"prefer-tab matched {tab.count()} elements",
                        })
                except Exception as exc:
                    result["clicks"].append({
                        "clicked_tab": prefer,
                        "error": f"{type(exc).__name__}: {exc}",
                    })

            # Pass 1: dump every frame as-is.
            for fi, frame in enumerate(page.frames):
                try:
                    d = frame.evaluate(DUMP_JS)
                except Exception as exc:
                    d = {"evaluate_error": f"{type(exc).__name__}: {exc}"}
                d["frame_index"] = fi
                try:
                    d["frame_url"] = frame.url
                    d["frame_name"] = frame.name
                except Exception:
                    pass
                result["frames"].append(d)

            # Pass 2: click each unselected [role=tab] once, re-dump main frame,
            # attributing newly visible fields to that tab. Read-only: tabs only.
            try:
                main_tabs = page.evaluate(
                    "() => Array.from(document.querySelectorAll('[role=\"tab\"]'))"
                    ".map(e => ({id: e.id || null, "
                    "text: (e.innerText||'').trim().slice(0,80), "
                    "selected: e.getAttribute('aria-selected')}))")
            except Exception:
                main_tabs = []
            for t in main_tabs:
                if t["selected"] == "true":
                    continue
                sel = f'[role="tab"]#{t["id"]}' if t["id"] else None
                try:
                    if sel:
                        page.click(sel, timeout=8000)
                    else:
                        # no id: click by text among role=tab (best effort)
                        page.get_by_role("tab", name=t["text"]).first.click(timeout=8000)
                    result["clicks"].append(
                        {"clicked_tab": t["text"], "selector": sel or ("text:" + t["text"])})
                    time.sleep(2)
                    d = page.main_frame.evaluate(DUMP_JS)
                    d["after_tab_click"] = t["text"]
                    result["frames"].append(d)
                except Exception as exc:
                    result["clicks"].append(
                        {"clicked_tab": t["text"],
                         "error": f"{type(exc).__name__}: {exc}"})
        finally:
            try:
                page.close()
            except Exception:
                pass

    # Duplicate analysis across all dumped frames/passes.
    seen_id = {}
    seen_name = {}
    for f in result["frames"]:
        for fld in f.get("fields", []):
            key = (f.get("frame_url"), f.get("after_tab_click"))
            if fld.get("id"):
                seen_id.setdefault(fld["id"], []).append(
                    {"visibility": fld["visibility"], "frame": f.get("frame_url"),
                     "after_tab": f.get("after_tab_click"), "label": fld["label"]})
            if fld.get("name"):
                seen_name.setdefault(fld["name"], []).append(
                    {"visibility": fld["visibility"], "frame": f.get("frame_url"),
                     "after_tab": f.get("after_tab_click"), "label": fld["label"]})
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
    print(json.dumps(result, indent=2)[:20000])
    print(f"\n... full dump written to {args.out} "
          f"({sum(len(f.get('fields', [])) for f in result['frames'])} field rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
