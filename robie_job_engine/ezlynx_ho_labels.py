"""Proven EZLynx homeowners (HO) FormEntry labels and navigation.

Source: manual browser run 2026-09-13 ~22:05 EDT on applicant 220250093
(ROBIE Test LLC), policy TEST-HO-20260913-E02, Policy ID 83670183.
Every string below was read verbatim from the live DOM during that run,
which filled all six coverages, saved, and re-read them from the policy
Summary ("COVERAGE LIMITS") with all six matching.

Provenance rule: entries here are EVIDENCE, not guesses. Do not add a label
without a live-DOM read to back it. See ~/workspace/robie-manual-ops/wiki/
patterns/ezlynx-coverages-navigation.md for the full trace.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HoCoverageField:
    """One coverage line on the 'Coverages' sub-tab.

    `label` is the visible label text; `textbox_hint` is the verbatim hint
    text shown on the textbox itself ("Enter limit: ...").
    """

    letter: str
    name: str
    label: str
    textbox_hint: str


HO_COVERAGE_FIELDS: tuple[HoCoverageField, ...] = (
    HoCoverageField(
        letter="A",
        name="Dwelling",
        label="Dwelling",
        textbox_hint="Enter limit: The limit associated with dwelling coverage.",
    ),
    HoCoverageField(
        letter="B",
        name="Other Structures",
        label="Other Structures",
        textbox_hint="Enter limit: The limit associated with other structures coverage.",
    ),
    HoCoverageField(
        letter="C",
        name="Personal Property",
        label="Personal Property Coverage",
        textbox_hint="Enter limit: The limit associated with personal property coverage.",
    ),
    HoCoverageField(
        letter="D",
        name="Loss of Use",
        label="Loss of Use",
        textbox_hint="Enter limit: The limit associated with loss of use coverage.",
    ),
    HoCoverageField(
        letter="E",
        name="Personal Liability",
        label="Personal Liability Each Occurrence",
        textbox_hint=(
            "Enter limit: The limit associated with personal liability "
            "each occurrence coverage."
        ),
    ),
    HoCoverageField(
        letter="F",
        name="Medical Payments",
        label="Medical Payments Each Person",
        textbox_hint=(
            "Enter limit: The limit associated with medical payments "
            "each person coverage."
        ),
    ),
)

# Verbatim tab strip on the FormEntry page
# (Policy/83670183/FormEntry/Index/482889733?prevApplied=482889733).
HO_FORM_TABS: tuple[str, ...] = (
    "Insured Information",
    "Dwelling Information / Coverages",
    "Underwriting",
    "Additional Interests",
)

# Verbatim sub-tabs in the dwelling editor (location "Actions" -> "Edit").
HO_DWELLING_SUBTABS: tuple[str, ...] = (
    "Dwelling Information",
    "Coverages",
    "Optional Coverages / Endorsements",
)

# Section heading on the "Coverages" sub-tab.
HO_COVERAGES_SECTION = "Coverages / Limits of Liability"

# The Coverage E field sits under this section heading.
HO_LIABILITY_SECTION = "Personal Liability Coverage"

# "Add/Edit Location" modal: this checkbox must be UNCHECKED before the
# address fields become editable.
HO_LOCATION_SAME_AS_MAILING = "Same As Mailing"

# Edit Policy page dropdown ground truth (also proven 2026-09-13):
# "Billing Type" options are "---Select---", "Agency", "Direct" — "Direct",
# NOT "Direct Bill".
HO_BILLING_TYPE_DIRECT = "Direct"


def coverage_field(letter: str) -> HoCoverageField:
    """Return the proven field record for coverage letter A-F (case-insensitive)."""
    wanted = letter.strip().upper()
    for field in HO_COVERAGE_FIELDS:
        if field.letter == wanted:
            return field
    raise KeyError(f"no proven HO coverage label for letter {letter!r}")
