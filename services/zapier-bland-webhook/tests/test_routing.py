"""Tests for campaign routing and the freeform/deterministic split."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dispatcher import route_campaign, FREEFORM_CAMPAIGN
from prompts import deterministic_campaigns


def test_freeform_routing():
    assert route_campaign("robie-call") == "freeform"
    assert route_campaign("Robie-Call") == "freeform"  # case-insensitive
    assert route_campaign("  robie-call  ") == "freeform"  # whitespace-tolerant


def test_deterministic_routing():
    for cid in deterministic_campaigns():
        assert route_campaign(cid) == "deterministic", cid
    assert len(deterministic_campaigns()) == 10


def test_unknown_routing():
    assert route_campaign("bogus") == "unknown"
    assert route_campaign("") == "unknown"
    assert route_campaign(None) == "unknown"


def test_freeform_constant_matches():
    assert FREEFORM_CAMPAIGN == "robie-call"
