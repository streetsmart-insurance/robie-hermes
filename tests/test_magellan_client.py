from datetime import datetime, timezone
from robie_job_engine.magellan_client import MagellanAuditor, MagellanCallRecord

def test_magellan_auditor_correlation():
    now = datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc)
    
    magellan_records = [
        MagellanCallRecord(
            date_time=now,
            from_phone="(908) 416-1464",
            to_phone="(732) 462-8343",
            duration_seconds=100,
            sentiment="Sad",
            tags=["Trucking Department", "Urgent", "Cancellation"],
            is_handled=False,
            at_risk_flag=True,
        ),
        MagellanCallRecord(
            date_time=now,
            from_phone="(201) 850-0229",
            to_phone="(732) 462-8343",
            duration_seconds=121,
            sentiment="Satisfied",
            tags=["Quote", "Client", "COI"],
            is_handled=True,
        ),
    ]

    unreturned_calls = [
        {"from": "9084161464", "rep": "Maria Bara", "time": "06:04 PM"},
        {"from": "5551234567", "rep": "Jackie Arriola", "time": "10:00 AM"},
    ]

    enriched = MagellanAuditor.correlate_with_ringcentral(magellan_records, unreturned_calls)
    
    assert len(enriched) == 2
    # Odell Logistics matched
    assert enriched[0]["magellan_sentiment"] == "Sad"
    assert enriched[0]["magellan_at_risk"] is True
    assert "Cancellation" in enriched[0]["magellan_tags"]
    
    # Unknown caller
    assert enriched[1]["magellan_sentiment"] == "UNRECORDED"
    assert enriched[1]["magellan_at_risk"] is False
