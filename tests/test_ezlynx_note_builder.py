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

    assert "Emailed uw@libertymutual.com at Liberty Mutual." in note_init
    assert "[RENEWAL-REQ-101]" in note_init
    assert "Robie was here" in note_init
    assert "09/08/2026" in note_init

    note_followup = EZLynxNoteBuilder.format_outreach_email_note(
        policy=policy,
        tracking_code="RENEWAL-REQ-101",
        recipient_email="uw@libertymutual.com",
        subject="Re: Renewal Quote Request",
        is_followup=True,
        followup_number=2,
        next_followup_date=date(2026, 9, 15)
    )

    assert "follow-up #2 email to uw@libertymutual.com" in note_followup
    assert "Robie was here" in note_followup
    assert "09/15/2026" in note_followup

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

def test_note_builder_reply_received_with_clean_text():
    policy = PolicyRenewal(
        policy_number="CCP35165-01",
        insured_name="Kodomo Education Services LLC",
        carrier_name="Markel Insurance",
        line_of_business="Commercial",
        expiration_date=date(2026, 10, 1)
    )

    clean_text = "Greetings, Please find attached the Loss Run Report that you requested."
    note = EZLynxNoteBuilder.format_reply_received_note(
        policy=policy,
        tracking_code="REQ-12345",
        sender_email="lossruns@markel.com",
        intent="QUOTE_ATTACHED",
        summary="Markel sent 5-year loss run report",
        has_attachment=True,
        attachment_name="CCP35165.pdf",
        clean_reply_text=clean_text
    )

    assert note.startswith("Policy: #CCP35165-01 (Commercial - Markel Insurance)")
    assert "Underwriter Message:" in note
    assert clean_text in note
    assert note.rstrip().endswith("Robie was here")


def test_note_builder_carrier_voice_cadence_and_phone_needed():
    policy = PolicyRenewal(
        policy_number="UB-6N448514-25-42-V",
        insured_name="Yes We Do LLC",
        carrier_name="The Hartford",
        line_of_business="Workers Comp",
        expiration_date=date(2026, 10, 15),
        discussion_title="Manual Workers Compensation Renewal",
    )
    dispatched = EZLynxNoteBuilder.format_carrier_voice_cadence_note(
        policy, phone="+18005551234", call_id="sim_1", attempts=2
    )
    assert dispatched.startswith("Policy: #UB-6N448514-25-42-V (Workers Comp - The Hartford)")
    assert "Call type: carrier" in dispatched
    assert "+18005551234" in dispatched
    assert dispatched.rstrip().endswith("Robie was here")

    needed = EZLynxNoteBuilder.format_carrier_voice_phone_needed_note(policy)
    assert needed.startswith("Policy: #UB-6N448514-25-42-V")
    assert "never invented" in needed.lower()
    assert needed.rstrip().endswith("Robie was here")
