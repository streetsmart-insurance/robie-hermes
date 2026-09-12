#!/usr/bin/env python3
"""Fill FormEntry Coverages by literal label text.

Carlo 2026-09-12: the FormEntry Coverage tab labels are literal —
Dwelling, Other Structures, Personal Property, Loss of Use, Blanket,
Personal Liability EA OCC, Medical Payments EA PER — plus one free-text row
and deductibles with AMOUNT / PERCENT / TYPE. The invented HO Coverage A-F
id selectors were never real; they are not used here.

Strategy: for each label, find the visible label text exactly (case-insensitive),
then resolve the associated input through, in order:
  1. `label[for=id]` -> the input with that id
  2. a wrapping `<label>` element -> the input it contains
  3. `aria-label` / `aria-labelledby` on an input
  4. the nearest input in the same table row / form group (reported, strict)

A label that cannot be resolved is reported NOT_FOUND — never guessed.
Every fill is read back and reported.
"""
from __future__ import annotations

from typing import Any

# Literal labels from Carlo's training-video evidence, 2026-09-12.
# Order matches the FormEntry Coverages tab.
COVERAGE_LABELS = (
    "Dwelling",
    "Other Structures",
    "Personal Property",
    "Loss of Use",
    "Blanket",
    "Personal Liability EA OCC",
    "Medical Payments EA PER",
)

DEDUCTIBLE_COLUMNS = ("AMOUNT", "PERCENT", "TYPE")

FIND_INPUT_JS = r"""
(label) => {
  const norm = (s) => (s || "").trim().replace(/\s+/g, " ").toLowerCase();
  const want = norm(label);
  if (!want) return {found: false, reason: "empty label"};
  // Candidate label elements: label, th, td, div, span with exact text.
  const els = Array.from(document.querySelectorAll("label, th, td, div, span, legend"));
  const matches = els.filter((el) => {
    // Only leaf-ish elements: skip containers whose text comes from children
    // that themselves contain the label (avoids matching whole tables).
    const t = norm(el.innerText);
    if (t !== want) return false;
    const kids = Array.from(el.querySelectorAll("label, th, td, div, span"));
    return !kids.some((k) => norm(k.innerText) === want);
  });
  if (matches.length === 0) return {found: false, reason: "label text not found"};
  const labelEl = matches[0];
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    name: el.getAttribute("name"),
    type: (el.type || "").toLowerCase() || null,
  });
  // 1. label[for] -> input#id
  if (labelEl.tagName === "LABEL" && labelEl.getAttribute("for")) {
    const target = document.getElementById(labelEl.getAttribute("for"));
    if (target && /^(input|select|textarea)$/i.test(target.tagName))
      return {found: true, via: "label-for", input: describe(target)};
  }
  // 2. input inside the label element
  const inner = labelEl.querySelector("input, select, textarea");
  if (inner) return {found: true, via: "label-wrap", input: describe(inner)};
  // 3. row / group scope: nearest input in the same tr, fieldset, or .form-group
  const scope = labelEl.closest("tr, fieldset, .form-group, .row, li");
  if (scope) {
    const cands = Array.from(scope.querySelectorAll("input, select, textarea"))
      .filter((el) => !el.disabled && el.type !== "hidden");
    if (cands.length === 1)
      return {found: true, via: "single-input-in-scope", input: describe(cands[0])};
    if (cands.length > 1)
      return {found: false, reason: "ambiguous: " + cands.length + " inputs in scope",
              candidates: cands.map(describe)};
  }
  // 4. aria-label / aria-labelledby match on inputs directly
  const aria = Array.from(document.querySelectorAll("input, select, textarea"))
    .filter((el) => norm(el.getAttribute("aria-label")) === want);
  if (aria.length === 1)
    return {found: true, via: "aria-label", input: describe(aria[0])};
  return {found: false, reason: "no associated input resolved"};
}
"""


def find_input_for_label(page: Any, label: str) -> dict[str, Any]:
    """Resolve the input associated with a literal label. Read-only."""
    result = page.evaluate(FIND_INPUT_JS, label)
    return {"label": label, **(result or {})}


def fill_input_by_descriptor(
    page: Any, descriptor: dict[str, Any], value: str
) -> dict[str, Any]:
    """Fill the input identified by descriptor; read back the value."""
    input_id = descriptor.get("id")
    input_name = descriptor.get("name")
    if input_id:
        locator = page.locator(f"#{input_id}")
    elif input_name:
        locator = page.locator(f'[name="{input_name}"]')
    else:
        return {"filled": False, "reason": "descriptor has no id or name"}
    tag = (descriptor.get("tag") or "").lower()
    try:
        if tag == "select":
            locator.select_option(value)
        else:
            locator.fill(str(value))
        read_back = locator.input_value()
    except Exception as exc:  # noqa: BLE001 - report, don't raise
        return {"filled": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "filled": True,
        "value_sent": str(value),
        "value_read_back": read_back,
        "matches": read_back == str(value),
    }


def fill_coverages_by_label(
    page: Any, values: dict[str, str]
) -> dict[str, Any]:
    """Fill each labeled coverage input. Returns per-label evidence.

    `values` maps literal label -> value. Labels not in COVERAGE_LABELS are
    still attempted (covers the free-text row and deductible columns) but the
    call never invents selectors.
    """
    report: dict[str, Any] = {"labels": {}, "filled_count": 0, "not_found": []}
    for label, value in values.items():
        found = find_input_for_label(page, label)
        entry: dict[str, Any] = {"found": bool(found.get("found")), "via": found.get("via")}
        if not found.get("found"):
            entry["reason"] = found.get("reason")
            report["not_found"].append(label)
        else:
            fill = fill_input_by_descriptor(page, found["input"], value)
            entry.update(fill)
            if fill.get("filled"):
                report["filled_count"] += 1
        report["labels"][label] = entry
    return report
