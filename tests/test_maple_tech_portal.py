"""Tests for Maple-Tech / Hyundai Marine & Fire carrier portal crawler and routing."""

from decimal import Decimal
from pathlib import Path
import json
import pytest

from src.extractor.quote_parser import extract_policy_total_premium
from src.ezlynx.manual_renewal_gate import (
    canonical_lob_display,
    candidate_existing_lob_renewal_titles,
    is_existing_lob_renewal_title,
)
from src.portals.base_portal import PortalSearchResult
from src.portals.carrier_agents import get_carrier_crawler
from src.portals.maple_tech import MapleTechPortalCrawler, select_best_aspire_document


def test_get_carrier_crawler_resolves_maple_tech():
    crawler1 = get_carrier_crawler("Hyundai Marine & Fire Insurance Company")
    assert isinstance(crawler1, MapleTechPortalCrawler)

    crawler2 = get_carrier_crawler("HYUNDAI MARINE & FIRE INS CO LTD")
    assert isinstance(crawler2, MapleTechPortalCrawler)

    crawler3 = get_carrier_crawler("Maple-Tech Aspire")
    assert isinstance(crawler3, MapleTechPortalCrawler)


def test_select_best_aspire_document():
    docs = [
        {"DocumentHandle": "h1", "DocumentDescription": "APPLICATION"},
        {"DocumentHandle": "h2", "DocumentDescription": "QUOTATION"},
        {"DocumentHandle": "h3", "DocumentDescription": "POLICY DEC PAGES"},
        {"DocumentHandle": "h4", "DocumentDescription": "POLICY"},
    ]
    best = select_best_aspire_document(docs)
    assert best is not None
    assert best["DocumentHandle"] == "h3"
    assert best["DocumentDescription"] == "POLICY DEC PAGES"


def test_select_best_aspire_document_fallback_to_quote():
    docs = [
        {"DocumentHandle": "h1", "DocumentDescription": "APPLICATION"},
        {"DocumentHandle": "h2", "DocumentDescription": "QUOTATION"},
    ]
    best = select_best_aspire_document(docs)
    assert best is not None
    assert best["DocumentHandle"] == "h2"


def test_dwelling_fire_lob_aliases_and_display():
    assert canonical_lob_display("Dwelling Fire") == "Dwelling Fire"
    assert canonical_lob_display("dwelling fire") == "Dwelling Fire"
    assert canonical_lob_display("dwelling") == "Dwelling Fire"
    assert canonical_lob_display("dp") == "Dwelling Fire"

    candidates = candidate_existing_lob_renewal_titles("Dwelling Fire")
    assert "Dwelling Fire Manual Renewal" in candidates
    assert "Dwelling Fire Renewal" in candidates

    assert is_existing_lob_renewal_title("Dwelling Fire Manual Renewal", "Dwelling Fire") is True
    # stripped id


def test_extract_premium_dwelling_fire_hua_li():
    sample_text = """
    HYUNDAI MARINE & FIRE INSURANCE CO., LTD.
    POLICY NUMBER: DPNJ2024100008-26
    NAMED INSURED: Hua Li & Simon Zhou
    COVERAGES AND LIMITS:
    Coverage A - Dwelling: Limit $250,000 Premium $817.00
    Coverage C - Personal Property: Limit $50,000 Premium Included
    Coverage E - Personal Liability: Limit $300,000 Premium Included
    Basic Policy Premium: $996.00
    Additional Coverages: $110.00
    Policy Total Premium: $1,106.00
    Total Annual Policy Premium: $1,106.00
    """
    premium = extract_policy_total_premium(sample_text)
    assert premium == Decimal("1106.00")


def test_carrier_directory_routes_hyundai_to_portal():
    with open("data/carrier_directory.json", "r") as f:
        data = json.load(f)
    assert data["Hyundai Marine & Fire Insurance Company"]["channel"] == "PORTAL"
    assert "maple-tech" in data["Hyundai Marine & Fire Insurance Company"]["portal_url"]
    assert data["HYUNDAI MARINE & FIRE INS CO LTD"]["channel"] == "PORTAL"
