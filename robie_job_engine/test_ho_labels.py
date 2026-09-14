"""Regression tests for the HO coverage root-cause fix.

Provenance: manual browser run 2026-09-13 ~22:05 EDT on applicant 220250093,
policy TEST-HO-20260913-E02 (ID 83670183). The run proved:
  1. The FormEntry "Dwelling Information / Coverages" tab silently refuses to
     open while the policy's Locations grid is empty (root cause of every
     "cannot open Coverages" worker failure). ensure_dwelling_location()
     encodes the fix.
  2. The live DOM does NOT say "Coverage A". The real texts are the
     "Enter limit: ..." hint strings and short labels below. The
     _LIVE_LETTER_RE (^coverage\\s+([A-F])) path can never match them, so
     proven_label_for_coverage_letter() runs first as the deterministic path.

No browser needed.
"""

from __future__ import annotations

from .ezlynx_ho_labels import HO_COVERAGE_FIELDS, coverage_field
from .formentry_coverages import (
    live_label_for_coverage_letter,
    proven_label_for_coverage_letter,
)

# Live label strings observed verbatim on policy 83670183.
LIVE_LABELS = [
    "Enter limit: The limit associated with dwelling coverage.",
    "Other Structures",
    "Enter limit: The limit associated with other structures coverage.",
    "Personal Property Coverage",
    "Enter limit: The limit associated with personal property coverage.",
    "Loss of Use",
    "Enter limit: The limit associated with loss of use coverage.",
    "Personal Liability Each Occurrence",
    "Enter limit: The limit associated with personal liability each occurrence coverage.",
    "Medical Payments Each Person",
    "Enter limit: The limit associated with medical payments each person coverage.",
]


def test_label_map_covers_all_six_coverages():
    letters = [f.letter for f in HO_COVERAGE_FIELDS]
    assert letters == ["A", "B", "C", "D", "E", "F"], letters
    for f in HO_COVERAGE_FIELDS:
        assert f.label.strip(), f"empty label for coverage {f.letter}"
        assert f.textbox_hint.strip(), f"empty hint for coverage {f.letter}"


def test_proven_matcher_resolves_all_six():
    assert proven_label_for_coverage_letter("A", LIVE_LABELS) == (
        "Enter limit: The limit associated with dwelling coverage."
    )
    assert proven_label_for_coverage_letter("B", LIVE_LABELS) == "Other Structures"
    assert (
        proven_label_for_coverage_letter("C", LIVE_LABELS)
        == "Personal Property Coverage"
    )
    assert proven_label_for_coverage_letter("D", LIVE_LABELS) == "Loss of Use"
    assert (
        proven_label_for_coverage_letter("E", LIVE_LABELS)
        == "Personal Liability Each Occurrence"
    )
    assert (
        proven_label_for_coverage_letter("F", LIVE_LABELS)
        == "Medical Payments Each Person"
    )


def test_proven_matcher_rejects_unknown():
    assert proven_label_for_coverage_letter("Z", LIVE_LABELS) is None
    assert proven_label_for_coverage_letter("A", []) is None
    assert proven_label_for_coverage_letter("A", ["Name", "Address", "City"]) is None


def test_live_label_lookup_prefers_proven():
    # The deterministic proven path must win over the "Coverage A" guess.
    assert live_label_for_coverage_letter("A", LIVE_LABELS) == (
        "Enter limit: The limit associated with dwelling coverage."
    )
    assert live_label_for_coverage_letter("E", LIVE_LABELS) == (
        "Personal Liability Each Occurrence"
    )


def test_location_prerequisite_helper_exists():
    from . import ezlynx_policy_setup as psu

    assert callable(psu.EzlynxPolicySetupPage.ensure_dwelling_location)
    import inspect

    src = inspect.getsource(psu.EzlynxPolicySetupPage.ensure_dwelling_location)
    assert "Same As Mailing" in src
