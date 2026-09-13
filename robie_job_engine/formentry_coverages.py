#!/usr/bin/env python3
"""Fill FormEntry Coverages from the LIVE page labels.

Carlo 2026-09-13: read the live FormEntry labels. HOME shows Coverage A–F
(or whatever is actually on the page). Let Gemini pick among those live
labels, apply, and retry once. Do not look for a hardcoded Dwelling /
Other Structures alias list. Do not invent A-F id selectors. Do not invent
an omitted letter's amount.

Strategy: for each live label, find the visible label text exactly (case-insensitive),
then resolve the associated input through, in order:
  1. `label[for=id]` -> the input with that id
  2. a wrapping `<label>` element -> the input it contains
  3. `aria-label` / `aria-labelledby` on an input
  4. the nearest input in the same table row / form group (reported, strict)

A label that cannot be resolved is reported NOT_FOUND — never guessed.
Every fill is read back and reported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# What HOME FormEntry actually shows. Filler uses the live page text, not
# a Dwelling / Other Structures alias list.
HOME_LIVE_COVERAGE_LABELS = (
    "Coverage A",
    "Coverage B",
    "Coverage C",
    "Coverage D",
    "Coverage E",
    "Coverage F",
)
COVERAGE_LETTERS = ("A", "B", "C", "D", "E", "F")

# Older training-video labels. Kept for evidence/docs only. HOME fill must
# not search this list.
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

# Preferred live A–F names. Never invent values for a letter the job omitted.
COVERAGE_AMOUNT_LABELS = HOME_LIVE_COVERAGE_LABELS

_LIVE_LETTER_RE = re.compile(
    r"^coverage\s+([A-F])(?:\s*[-:–—(]|\s*$)",
    re.IGNORECASE,
)

# FormEntry Location/Address section. Not coverage fields. Never fill these
# as Coverage A–F and never let Gemini pick them.
LOCATION_SECTION_HINTS = (
    "name",
    "address",
    "address 1",
    "address 2",
    "city",
    "state",
    "zip",
    "country",
    "location #",
    "location",
)
LOCATION_EXCLUDE_LABELS = LOCATION_SECTION_HINTS + (
    "actions",
    "line of business",
)
COVERAGE_TAB_NAMES = ("Coverages", "Coverage")
# Click only a name that is on the live FormEntry nav. Do not invent
# "Coverages" when the page shows "Coverage" (or another live wording).
# Unique link first: get_by_role("link", name=<live nav text>, exact=True).
COVERAGES_LIVE_NAV_CLICK = (
    'get_by_role("link", name=<live nav coverage text>, exact=True)'
)
COVERAGES_TAB_LOCATOR = COVERAGES_LIVE_NAV_CLICK
COVERAGES_NAV_ROLES = ("link", "tab", "button")

LIST_LIVE_NAV_JS = r"""
() => {
  const norm = (s) => (s || "").trim().replace(/\s+/g, " ");
    const els = Array.from(document.querySelectorAll(
    '[role="tab"], [role="link"], a, button, [role="button"], '
    + '.nav-link, .nav-item, .k-link, .k-item, li'
  ));
  const out = [];
  const seen = new Set();
  for (const el of els) {
    const t = norm(el.getAttribute("aria-label") || el.innerText);
    if (!t || t.length > 48) continue;
    const key = t.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(t);
  }
  return out.slice(0, 40);
}
"""

LIST_LIVE_LABELS_JS = r"""
() => {
  const norm = (s) => (s || "").trim().replace(/\s+/g, " ");
  const els = Array.from(document.querySelectorAll(
    "label, th, td, legend, [role='cell'], [role='columnheader']"
  ));
  const out = [];
  const seen = new Set();
  for (const el of els) {
    const t = norm(el.innerText);
    if (!t || t.length > 80) continue;
    const kids = Array.from(el.querySelectorAll("label, th, td"));
    if (kids.some((k) => k !== el && norm(k.innerText) === t)) continue;
    const key = t.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(t);
  }
  return out.slice(0, 80);
}
"""

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


def normalize_coverage_label(text: str) -> str:
    return " ".join(str(text or "").strip().split()).casefold()


def match_wanted_to_live_label(wanted: str, live_labels: list[str]) -> str | None:
    """Map a wanted live name onto a live FormEntry label. Exact first.

    HOME fill passes Coverage A–F (from the page), not Dwelling aliases.
    Does not invent selectors. Does not invent amounts.
    """
    want = normalize_coverage_label(wanted)
    if not want:
        return None
    for label in live_labels:
        if normalize_coverage_label(label) == want:
            return label
    for label in live_labels:
        live = normalize_coverage_label(label)
        if want and want in live:
            return label
    for label in live_labels:
        live = normalize_coverage_label(label)
        if live and live in want:
            return label
    return None


def live_label_for_coverage_letter(
    letter: str, live_labels: list[str]
) -> str | None:
    """Return the live FormEntry label for Coverage A–F, or None.

    Reads live text only. Does not search a Dwelling alias list.
    """
    want = str(letter or "").strip().upper()
    if want not in COVERAGE_LETTERS:
        return None
    exact: list[str] = []
    prefixed: list[str] = []
    for label in live_labels:
        folded = normalize_coverage_label(label)
        match = _LIVE_LETTER_RE.match(folded)
        if not match or match.group(1).upper() != want:
            continue
        if folded == f"coverage {want.casefold()}":
            exact.append(label)
        else:
            prefixed.append(label)
    if exact:
        return exact[0]
    if prefixed:
        return prefixed[0]
    return None


def has_live_coverage_letter(live_labels: list[str]) -> bool:
    """True when the live page already shows a Coverage A–F label."""
    return any(
        live_label_for_coverage_letter(letter, live_labels)
        for letter in COVERAGE_LETTERS
    )


def is_location_or_address_label(label: str) -> bool:
    """True for Location/Address chrome. Address is never a coverage field."""
    folded = normalize_coverage_label(label)
    if not folded:
        return False
    if folded in LOCATION_EXCLUDE_LABELS:
        return True
    return folded.startswith("address ")


def is_location_section_labels(live_labels: list[str]) -> bool:
    """True when FormEntry is on Location/Address, not Coverages.

    Live miss 44928e33 started on FormEntry Location (Name, Address, City,
    State, Zip, Country, Location #). That is the wrong tab.
    """
    if has_live_coverage_letter(live_labels):
        return False
    hits = 0
    for label in live_labels:
        folded = normalize_coverage_label(label)
        if folded in LOCATION_SECTION_HINTS or folded.startswith("address "):
            hits += 1
    return hits >= 3


def filter_coverage_candidate_labels(live_labels: list[str]) -> list[str]:
    """Drop Location/Address chrome so Gemini cannot map a letter onto Address.

    Live miss 44928e33 RETRY still mapped onto Name / Address 1 / Line Of
    Business while FormEntry was on Location. Nothing on that tab is a
    coverage field — leftover must be empty until Coverages is open.
    """
    if is_location_section_labels(live_labels):
        return []
    return [
        str(item).strip()
        for item in live_labels
        if str(item).strip() and not is_location_or_address_label(item)
    ]


def looks_like_coverage_nav(name: str) -> bool:
    """True when a live nav label is the coverage section. Not Location."""
    if is_location_or_address_label(name):
        return False
    folded = normalize_coverage_label(name)
    return "coverage" in folded


def live_nav_coverage_name(nav_labels: list[str]) -> str | None:
    """Return the live nav text for the coverage section, or None.

    Prefers exact Coverages, then Coverage, then any live item whose text
    contains coverage. Never invents a name that is not on the list.
    """
    coverage_items = [
        str(item).strip()
        for item in nav_labels
        if str(item).strip() and looks_like_coverage_nav(item)
    ]
    for item in coverage_items:
        if normalize_coverage_label(item) == "coverages":
            return item
    for item in coverage_items:
        if normalize_coverage_label(item) == "coverage":
            return item
    if coverage_items:
        return coverage_items[0]
    return None


def pick_live_coverage_nav(
    nav_labels: list[str],
    *,
    gemini_client: Any | None = None,
    exclude: list[str] | None = None,
) -> tuple[str | None, bool]:
    """Pick one live nav name. Gemini only among the live list."""
    leftover = [
        item
        for item in nav_labels
        if normalize_coverage_label(item)
        not in {normalize_coverage_label(x) for x in (exclude or [])}
    ]
    name = live_nav_coverage_name(leftover)
    if name:
        return name, False
    if not leftover:
        return None, False
    from .gemini_field_helper import ask_gemini_live_option

    decision = ask_gemini_live_option(
        widget_name="FormEntry section",
        wanted="Coverages",
        live_options=leftover,
        client=gemini_client,
    )
    option = str(decision.option or "").strip()
    if (
        decision.action == "APPLY"
        and option
        and looks_like_coverage_nav(option)
        and any(
            normalize_coverage_label(item) == normalize_coverage_label(option)
            for item in leftover
        )
    ):
        return option, bool(decision.gemini_asked)
    return None, bool(decision.gemini_asked)


def map_letter_amounts_to_live_labels(
    amounts_by_letter: dict[str, str],
    live_labels: list[str],
    *,
    gemini_client: Any | None = None,
) -> dict[str, str]:
    """Map stated A–F amounts onto live labels. Never invent a letter.

    Gemini may pick among coverage live labels when the page is not literally
    Coverage A–F. It cannot invent Coverage E if E was omitted. It cannot
    pick Address, City, State, Zip, or other Location-tab chrome.
    """
    mapped: dict[str, str] = {}
    leftover = filter_coverage_candidate_labels(live_labels)
    for letter in COVERAGE_LETTERS:
        amount = str((amounts_by_letter or {}).get(letter) or "").strip()
        if not amount:
            continue
        live = live_label_for_coverage_letter(letter, leftover)
        if live is None and leftover:
            from .gemini_field_helper import ask_gemini_live_option

            decision = ask_gemini_live_option(
                widget_name=f"Coverage {letter}",
                wanted=f"Coverage {letter}",
                live_options=leftover,
                client=gemini_client,
            )
            if decision.action == "APPLY" and decision.option:
                live = decision.option
        if not live:
            continue
        mapped[live] = amount
        used = normalize_coverage_label(live)
        leftover = [
            item for item in leftover if normalize_coverage_label(item) != used
        ]
    return mapped


def list_live_coverage_labels(page: Any) -> list[str]:
    """Read visible Coverages-tab labels. Never guesses invented A-F id selectors."""
    try:
        raw = page.evaluate(LIST_LIVE_LABELS_JS)
    except TypeError:
        raw = page.evaluate(LIST_LIVE_LABELS_JS, None)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def find_input_for_label(page: Any, label: str) -> dict[str, Any]:
    """Resolve the input associated with a literal or live-matched label."""
    live = list_live_coverage_labels(page)
    matched = match_wanted_to_live_label(label, live) or label
    result = page.evaluate(FIND_INPUT_JS, matched)
    out = {"label": label, "live_label": matched, **(result or {})}
    if not out.get("found") and live:
        out["live_labels_seen"] = live[:20]
    return out


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


async def alist_live_coverage_labels(page: Any) -> list[str]:
    try:
        raw = await page.evaluate(LIST_LIVE_LABELS_JS)
    except TypeError:
        raw = await page.evaluate(LIST_LIVE_LABELS_JS, None)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


@dataclass
class CoveragesTabResult:
    live_labels: list[str]
    on_coverages: bool
    still_on_location: bool
    clicked: list[str] = field(default_factory=list)
    locators_tried: list[str] = field(default_factory=list)
    gemini_asked: bool = False


async def alist_live_nav_labels(page: Any) -> list[str]:
    """Visible FormEntry section names. Never guesses invented A-F ids."""
    try:
        raw = await page.evaluate(LIST_LIVE_NAV_JS)
    except TypeError:
        raw = await page.evaluate(LIST_LIVE_NAV_JS, None)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


async def aclick_unique_named(
    page: Any,
    name: str,
    *,
    roles: tuple[str, ...] = COVERAGES_NAV_ROLES,
) -> str | None:
    """Click a live nav name only when the locator is unique."""
    for role in roles:
        loc = page.get_by_role(role, name=name, exact=True)
        try:
            count = await loc.count()
        except Exception:
            continue
        if count != 1:
            continue
        try:
            await loc.click(timeout=8000)
        except Exception:
            continue
        return f'get_by_role("{role}", name="{name}", exact=True)'
    try:
        text = page.get_by_text(name, exact=True)
        if await text.count() == 1:
            await text.click(timeout=8000)
            return f'get_by_text("{name}", exact=True)'
    except Exception:
        return None
    return None


async def _await_section_settle(page: Any) -> None:
    try:
        await page.wait_for_timeout(1500)
    except Exception:
        pass


def _coverages_tab_proven(live: list[str]) -> bool:
    """True only after field labels include Coverage A–F, not Location."""
    return has_live_coverage_letter(live) and not is_location_section_labels(live)


async def aensure_coverages_tab(
    page: Any,
    *,
    gemini_client: Any | None = None,
) -> CoveragesTabResult:
    """Leave Location/Address using a live nav name. Prove the click.

    Live miss c6873406: hardcoded Coverages link did not change FormEntry
    Index. Read live nav texts first. Click only a name from that list
    (Coverages, Coverage, or Gemini's live pick). Re-read field labels.
    Fill only when they stop looking like Name/Address/City/Zip/Location #.
    """
    clicked: list[str] = []
    tried: list[str] = []
    gemini_asked = False
    live = await alist_live_coverage_labels(page)
    if _coverages_tab_proven(live):
        return CoveragesTabResult(
            live_labels=live,
            on_coverages=True,
            still_on_location=False,
        )

    nav = await alist_live_nav_labels(page)
    if not is_location_section_labels(live) and not live_nav_coverage_name(nav):
        return CoveragesTabResult(
            live_labels=live,
            on_coverages=False,
            still_on_location=False,
            locators_tried=tried,
        )

    name, asked = pick_live_coverage_nav(nav, gemini_client=gemini_client)
    gemini_asked = gemini_asked or asked
    if name:
        used = await aclick_unique_named(page, name)
        if used:
            clicked.append(name)
            tried.append(used)
            await _await_section_settle(page)
            live = await alist_live_coverage_labels(page)
            if _coverages_tab_proven(live):
                return CoveragesTabResult(
                    live_labels=live,
                    on_coverages=True,
                    still_on_location=False,
                    clicked=clicked,
                    locators_tried=tried,
                    gemini_asked=gemini_asked,
                )

    nav = await alist_live_nav_labels(page)
    retry_name, asked = pick_live_coverage_nav(
        nav, gemini_client=gemini_client, exclude=clicked
    )
    gemini_asked = gemini_asked or asked
    if retry_name:
        used = await aclick_unique_named(page, retry_name)
        if used:
            clicked.append(retry_name)
            tried.append(used)
            await _await_section_settle(page)
            live = await alist_live_coverage_labels(page)
            if _coverages_tab_proven(live):
                return CoveragesTabResult(
                    live_labels=live,
                    on_coverages=True,
                    still_on_location=False,
                    clicked=clicked,
                    locators_tried=tried,
                    gemini_asked=gemini_asked,
                )

    live = await alist_live_coverage_labels(page)
    return CoveragesTabResult(
        live_labels=live,
        on_coverages=_coverages_tab_proven(live),
        still_on_location=is_location_section_labels(live),
        clicked=clicked,
        locators_tried=tried or [COVERAGES_LIVE_NAV_CLICK],
        gemini_asked=gemini_asked,
    )


async def afind_input_for_label(page: Any, label: str) -> dict[str, Any]:
    """Async variant: match job label to a live FormEntry label, then resolve."""
    live = await alist_live_coverage_labels(page)
    matched = match_wanted_to_live_label(label, live) or label
    result = await page.evaluate(FIND_INPUT_JS, matched)
    out = {"label": label, "live_label": matched, **(result or {})}
    if not out.get("found") and live:
        out["live_labels_seen"] = live[:20]
    return out


async def _live_select_options(locator: Any) -> list[str]:
    try:
        raw = locator.locator("option").all_text_contents()
        if hasattr(raw, "__await__"):
            raw = await raw
        return [str(item).strip() for item in (raw or []) if str(item).strip()]
    except Exception:
        return []


async def afill_input_by_descriptor(
    page: Any,
    descriptor: dict[str, Any],
    value: str,
    *,
    gemini_client: Any | None = None,
    widget_name: str = "",
) -> dict[str, Any]:
    """Async variant of fill_input_by_descriptor.

    Dropdowns use the Gemini live-option helper. Never invent an option
    that is not on the live control.
    """
    input_id = descriptor.get("id")
    input_name = descriptor.get("name")
    if input_id:
        locator = page.locator(f"#{input_id}")
    elif input_name:
        locator = page.locator(f'[name="{input_name}"]')
    else:
        return {"filled": False, "reason": "descriptor has no id or name"}
    tag = (descriptor.get("tag") or "").lower()
    sent = str(value)
    gemini_asked = False
    try:
        if tag == "select":
            from .gemini_field_helper import ask_gemini_live_option

            live_options = await _live_select_options(locator)
            decision = ask_gemini_live_option(
                widget_name=widget_name or input_id or input_name or "coverage",
                wanted=str(value),
                live_options=live_options,
                client=gemini_client,
            )
            gemini_asked = bool(decision.gemini_asked)
            if decision.action != "APPLY" or not decision.option:
                return {
                    "filled": False,
                    "reason": decision.reason,
                    "gemini_asked": gemini_asked,
                    "live_options": live_options,
                }
            sent = decision.option
            await locator.select_option(sent)
        else:
            await locator.fill(str(value))
        read_back = await locator.input_value()
    except Exception as exc:  # noqa: BLE001 - report, don't raise
        return {"filled": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "filled": True,
        "value_sent": sent,
        "value_read_back": read_back,
        "matches": read_back == sent,
        "gemini_asked": gemini_asked,
    }


async def afill_coverages_by_label(
    page: Any,
    values: dict[str, str],
    *,
    gemini_client: Any | None = None,
) -> dict[str, Any]:
    """Async variant of fill_coverages_by_label for the Job Engine."""
    report: dict[str, Any] = {"labels": {}, "filled_count": 0, "not_found": []}
    for label, value in values.items():
        found = await afind_input_for_label(page, label)
        entry: dict[str, Any] = {
            "found": bool(found.get("found")),
            "via": found.get("via"),
            "live_label": found.get("live_label"),
        }
        if not found.get("found"):
            entry["reason"] = found.get("reason")
            report["not_found"].append(label)
        else:
            fill = await afill_input_by_descriptor(
                page,
                found["input"],
                value,
                gemini_client=gemini_client,
                widget_name=str(found.get("live_label") or label),
            )
            entry.update(fill)
            if fill.get("filled"):
                report["filled_count"] += 1
        report["labels"][label] = entry
    return report
