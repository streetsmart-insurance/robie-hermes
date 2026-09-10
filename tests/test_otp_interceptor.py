import pytest
from src.email_outreach.otp_interceptor import MultiInboxOTPInterceptor

def test_parse_standard_6_digit_otp():
    text = "Your verification code is 482910. Please do not share it with anyone."
    code, link = MultiInboxOTPInterceptor.parse_otp_and_link(text)
    assert code == "482910"
    assert link is None

def test_parse_otp_with_prefix():
    text = "StreetSmart Insurance Security Alert\nOne-Time Passcode: SS-938102\nExpires in 10 minutes."
    code, link = MultiInboxOTPInterceptor.parse_otp_and_link(text)
    assert code in ["SS-938102", "938102"]

def test_parse_activation_link():
    text = """
    Welcome to Neptune Flood!
    Click the link below to accept the invitation:
    https://policylink.neptuneflood.com/ls/click?upn=xyz123456
    """
    code, link = MultiInboxOTPInterceptor.parse_otp_and_link(text)
    assert link == "https://policylink.neptuneflood.com/ls/click?upn=xyz123456"

def test_parse_clerk_reset_link():
    text = "Reset your password by visiting: https://clerk.magellan.insure/v1/verify?token=abc-token-99."
    code, link = MultiInboxOTPInterceptor.parse_otp_and_link(text)
    assert link == "https://clerk.magellan.insure/v1/verify?token=abc-token-99"

def test_parse_hartford_otp():
    text = "The Hartford eBusiness Security Code: 712044. Valid for 15 minutes."
    code, link = MultiInboxOTPInterceptor.parse_otp_and_link(text)
    assert code == "712044"
