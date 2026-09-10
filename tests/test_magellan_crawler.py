"""Unit tests for Magellan Crawler and Auditor."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock

from src.portals.magellan_crawler import (
    MagellanCallRecord,
    MagellanAuditor,
    MagellanCrawler,
)
from src.database.models import PolicyRenewal
from src.ezlynx.note_builder import EZLynxNoteBuilder


def test_magellan_sentiment_classification():
    assert MagellanAuditor.classify_sentiment("Sad") == "SAD_FRUSTRATED"
    assert MagellanAuditor.classify_sentiment("Frustrated customer") == "SAD_FRUSTRATED"
    assert MagellanAuditor.classify_sentiment("Neutral call") == "NEUTRAL"
    assert MagellanAuditor.classify_sentiment("Very Satisfied") == "SATISFIED"
    assert MagellanAuditor.classify_sentiment("Gibberish") == "UNKNOWN"


def test_magellan_churn_risk_detection():
    rec_normal = MagellanCallRecord(
        date_time=datetime.now(timezone.utc),
        from_phone="7325551234",
        to_phone="7324628343",
        duration_seconds=60,
        sentiment="Satisfied",
        tags=["COI", "Billing"],
    )
    assert MagellanAuditor.is_churn_risk(rec_normal) is False

    rec_at_risk = MagellanCallRecord(
        date_time=datetime.now(timezone.utc),
        from_phone="7325551234",
        to_phone="7324628343",
        duration_seconds=120,
        sentiment="Sad",
        tags=["Cancellation", "High Rate"],
        cancellation_flag=True,
    )
    assert MagellanAuditor.is_churn_risk(rec_at_risk) is True


def test_magellan_correlation_with_renewal():
    call_dt = datetime(2026, 8, 30, 14, 0, tzinfo=timezone.utc)
    records = [
        MagellanCallRecord(
            date_time=call_dt,
            from_phone="(908) 416-1464",
            to_phone="(732) 462-8343",
            duration_seconds=180,
            sentiment="Frustrated",
            tags=["Cancellation", "Trucking"],
            caller_name="Acme Hauling LLC",
            transcript_summary="Insured unhappy with rate increase and requested cancellation options.",
            cancellation_flag=True,
        )
    ]

    matched = MagellanAuditor.correlate_renewal(
        insured_name="Acme Hauling LLC",
        insured_phone="9084161464",
        magellan_records=records,
    )

    assert matched is not None
    assert matched["has_recent_call"] is True
    assert matched["at_risk_churn"] is True
    assert matched["cancellation_intent"] is True
    assert "Cancellation" in matched["all_tags"]
    assert "Frustrated" in matched["latest_sentiment"]


def test_magellan_note_formatting():
    policy = PolicyRenewal(
        policy_number="GL-998877",
        carrier_name="Coterie",
        insured_name="Acme Hauling LLC",
    )
    mag_info = {
        "latest_sentiment": "Frustrated",
        "at_risk_churn": True,
        "cancellation_intent": True,
        "all_tags": ["Cancellation", "Rate Increase"],
        "summary": "Customer complaining about pricing.",
    }
    note = EZLynxNoteBuilder.format_magellan_intelligence_note(policy, mag_info)
    assert "MAGELLAN AI INTELLIGENCE" in note
    assert "Acme Hauling LLC" in note
    assert "⚠️ HIGH AT-RISK CHURN" in note
    assert "🚨 CANCELLATION REQUESTED" in note
    assert "Robie was here" in note


def test_magellan_credentials_resolution():
    with patch("src.portals.magellan_crawler.secrets_mgr.get_credential") as mock_cred:
        def cred_side_effect(service, key):
            if service == "magellan" and key == "username":
                return "magellan_user@streetsmart.insurance"
            if service == "magellan" and key == "password":
                return "MagellanSecret123"
            return None
        mock_cred.side_effect = cred_side_effect

        crawler = MagellanCrawler()
        creds = crawler.get_credentials()
        assert creds["username"] == "magellan_user@streetsmart.insurance"
        assert creds["password"] == "MagellanSecret123"
