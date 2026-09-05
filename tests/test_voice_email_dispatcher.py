"""Unit tests for Email Call Dispatcher."""

import pytest
from src.voice.email_dispatcher import EmailCallDispatcher


def test_is_authorized_sender():
    dispatcher = EmailCallDispatcher()
    assert dispatcher.is_authorized_sender("jake@streetsmart.insurance") is True
    assert dispatcher.is_authorized_sender("carlo@streetsmart.insurance") is True
    assert dispatcher.is_authorized_sender("eimy@streetsmart.insurance") is True
    assert dispatcher.is_authorized_sender("random@gmail.com") is False
    assert dispatcher.is_authorized_sender("hacker@malicious.com") is False


def test_parse_call_command_policy_and_instructions():
    dispatcher = EmailCallDispatcher()
    cmd = dispatcher.parse_call_command(
        sender="jake@streetsmart.insurance",
        subject="Please call Travelers for policy #UB-6N448514",
        body="Robie, please call Travelers and ask if the renewal quote is out.",
    )
    assert cmd is not None
    assert cmd["policy_number"] == "UB-6N448514"
    assert "renewal quote" in cmd["instructions"]


def test_parse_call_command_with_phone_override():
    dispatcher = EmailCallDispatcher()
    cmd = dispatcher.parse_call_command(
        sender="eimy@streetsmart.insurance",
        subject="Call carrier: PWC1239278",
        body="Please call Associated Specialty at 866-513-5650. Ask if payroll audit was accepted for Yes We Do LLC.",
    )
    assert cmd is not None
    assert cmd["policy_number"] == "PWC1239278"
    assert cmd["phone_override"] == "866-513-5650"
    assert "payroll audit" in cmd["body"]
    assert cmd.get("call_type") is None


def test_parse_call_command_explicit_client_call_type():
    dispatcher = EmailCallDispatcher()
    cmd = dispatcher.parse_call_command(
        sender="jake@streetsmart.insurance",
        subject="Call the client for PWC1239278",
        body="Call type: client\nPlease call the insured and review the quote.",
    )
    assert cmd is not None
    assert cmd["call_type"] == "client_followup"


def test_parse_call_command_ignores_unauthorized():
    dispatcher = EmailCallDispatcher()
    cmd = dispatcher.parse_call_command(
        sender="outsider@external.com",
        subject="Call carrier: PWC1239278",
        body="Call them now.",
    )
    assert cmd is None


def test_parse_call_command_ignores_non_call_emails():
    dispatcher = EmailCallDispatcher()
    cmd = dispatcher.parse_call_command(
        sender="carlo@streetsmart.insurance",
        subject="Meeting schedule for tomorrow",
        body="Hey team, let's meet at 2pm in the conference room.",
    )
    assert cmd is None
