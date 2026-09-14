"""Regression tests for the HO coverage label + location-prerequisite fix.

Provenance: manual browser run 2026-09-13 ~22:05 EDT on applicant 220250093,
policy TEST-HO-20260913-E02 (ID 83670183). The run proved:
  1. The FormEntry "Dwelling Information / Coverages" tab silently refuses to
     open while the policy's Locations grid is empty (root cause of every
     "cannot open Coverages" worker failure).
  2. The #HO_CoverageA/#HO_CoverageE id selectors never matched the live DOM;
     the real fields are found by their verbatim labels / hint text.

These tests pin the proven labels and the label-based fill path so the
unproven selectors cannot come back. No browser needed.
"""

from __future__ import annotations

import asyncio
import inspect

from .ezlynx_ho_labels import (
    HO_COVERAGE_FIELDS,
    HO_DWELLING_SUBTABS,
    HO_FORM_TABS,
    coverage_field,
)
from . import ezlynx_policy_setup as psu


def test_label_map_covers_all_six_coverages():
    letters = [f.letter for f in HO_COVERAGE_FIELDS]
    assert letters == ["A", "B", "C", "D", "E", "F"], letters
    for f in HO_COVERAGE_FIELDS:
        assert f.label.strip(), f"empty label for coverage {f.letter}"
        assert f.textbox_hint.strip(), f"empty hint for coverage {f.letter}"
        assert f.textbox_hint.startswith("Enter limit: "), f.hint


def test_coverage_field_lookup():
    assert coverage_field("a").letter == "A"
    assert coverage_field("F").name == "Medical Payments"
    try:
        coverage_field("Z")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for unknown letter")


def test_form_tabs_verbatim():
    assert "Dwelling Information / Coverages" in HO_FORM_TABS
    assert "Coverages" in HO_DWELLING_SUBTABS


class _FakeLocator:
    def __init__(self, calls, kind, text):
        self._calls = calls
        self._kind = kind
        self._text = text

    async def count(self):
        return 1

    @property
    def first(self):
        return self

    async def fill(self, value):
        self._calls.append((self._kind, self._text, value))


class _FakePage:
    def __init__(self, calls):
        self._calls = calls

    def get_by_label(self, text, exact=False):
        return _FakeLocator(self._calls, "label", text)

    def get_by_placeholder(self, text, exact=False):
        return _FakeLocator(self._calls, "placeholder", text)


class _FakeSetup(psu.EzlynxPolicySetupPage):
    def __init__(self, calls):
        self.page = _FakePage(calls)


def test_fill_homeowners_coverages_uses_verbatim_labels():
    """All six coverages are filled via their proven labels (not id selectors)."""
    calls: list = []
    worker = _FakeSetup(calls)
    ho = psu.HomeownersCoverageItem(
        dwelling_a="$1,200,000",
        other_structures_b="120000",
        personal_property_c="500000",
        loss_of_use_d="500000",
        liability_e="10000",
        med_pay_f="10000",
    )
    asyncio.run(psu.EzlynxPolicySetupPage.fill_homeowners_coverages(worker, ho))
    assert len(calls) == 6, f"expected 6 fills, got {len(calls)}: {calls}"
    by_label = {text: value for kind, text, value in calls}
    assert by_label["Dwelling"] == "1200000"
    assert by_label["Other Structures"] == "120000"
    assert by_label["Personal Property Coverage"] == "500000"
    assert by_label["Loss of Use"] == "500000"
    assert by_label["Personal Liability Each Occurrence"] == "10000"
    assert by_label["Medical Payments Each Person"] == "10000"


def test_unproven_id_selectors_are_gone():
    """The #HO_Coverage* id selectors never matched the live DOM (Phase 0 gate
    finding). They must not reappear in the fill path."""
    src = inspect.getsource(psu.EzlynxPolicySetupPage.fill_homeowners_coverages)
    assert 'locator("#HO_Coverage' not in src, "unproven id selector is back"
    assert 'name=\'Coverage' not in src, "unproven name selector is back"


def test_location_prerequisite_helper_exists():
    assert callable(psu.EzlynxPolicySetupPage.ensure_dwelling_location)
    src = inspect.getsource(psu.EzlynxPolicySetupPage.ensure_dwelling_location)
    assert "Same As Mailing" in src or "HO_LOCATION_SAME_AS_MAILING" in src
