"""Tests for EZLynx note building and discussion title formatting."""

from datetime import date
from src.database.models import PolicyRenewal
from src.ezlynx.note_builder import EZLynxNoteBuilder

def test_note_builder_portal_check():
    policy = PolicyRenewal(
        policy_number="HO-9912",
        insured_name="Alice Brown",
        carrier_name="Travelers",
        expiration_date=date(2026, 10, 15),
        discussion_title="Manual Homeowners Renewal"
    )

    note_success = EZLynxNoteBuilder.format_portal_check_note(
        policy=policy,
        success=True,
        details="Quote document retrieved.",
        downloaded_file="Renewal_Quote_HO-9912.pdf"
    )

    assert "=== [CARRIER PORTAL AUTOMATION] ===" in note_success
    assert "✅ RENEWAL QUOTE RETRIEVED" in note_success
    assert "HO-9912" in note_success
    assert "Renewal_Quote_HO-9912.pdf" in note_success

def test_note_builder_outreach_and_followup():
    policy = PolicyRenewal(
        policy_number="CA-5521",
        insured_name="Acme Corp",
        carrier_name="Liberty Mutual",
        expiration_date=date(2026, 10, 20),
        discussion_title="Manual Commercial Auto Renewal"
    )

    note_init = EZLynxNoteBuilder.format_outreach_email_note(
        policy=policy,
        tracking_code="RENEWAL-REQ-101",
        recipient_email="uw@libertymutual.com",
        subject="Renewal Quote Request",
        is_followup=False,
        next_followup_date=date(2026, 9, 8)
    )

    assert "=== [INITIAL RENEWAL OUTREACH SENT] ===" in note_init
    assert "[RENEWAL-REQ-101]" in note_init
    assert "2026-09-08" in note_init

    note_followup = EZLynxNoteBuilder.format_outreach_email_note(
        policy=policy,
        tracking_code="RENEWAL-REQ-101",
        recipient_email="uw@libertymutual.com",
        subject="Re: Renewal Quote Request",
        is_followup=True,
        followup_number=2,
        next_followup_date=date(2026, 9, 15)
    )

    assert "=== [OUTREACH FOLLOW-UP #2 SENT] ===" in note_followup
    assert "2026-09-15" in note_followup

def test_note_builder_csr_escalation():
    policy = PolicyRenewal(
        policy_number="GL-8831",
        insured_name="Zenith Plumbing LLC",
        carrier_name="AmWINS MGA",
        expiration_date=date(2026, 9, 25),
        assigned_agent="Ferrara, Jake",
        discussion_title="Manual General Liability Renewal"
    )

    note_esc = EZLynxNoteBuilder.format_csr_escalation_note(
        policy=policy,
        days_to_expiration=23,
        reason="No renewal quote received by 23 days prior to expiration."
    )

    assert "⚠️ === [CSR ESCALATION - URGENT RENEWAL REVIEW] ===" in note_esc
    assert "GL-8831" in note_esc
    assert "Ferrara, Jake" in note_esc
    assert "23 days remaining" in note_esc
