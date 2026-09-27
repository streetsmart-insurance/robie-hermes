"""Regression tests for robie_job_engine.email_sender_policy.

2026-09-14: the Robie email agent replied to its own robie@streetsmart.insurance
messages ~40 times in one night because is_allowed_sender() accepted any
@streetsmart.insurance address with no self-exclusion. These tests pin the fix:
the mailbox's own address must never be treated as an allowed sender.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine import email_sender_policy as policy  # noqa: E402


def test_self_address_is_rejected():
    assert policy.is_allowed_sender("robie@streetsmart.insurance") is False


def test_self_address_case_and_whitespace_variants_rejected():
    assert policy.is_allowed_sender(" Robie@StreetSmart.Insurance ") is False


def test_is_self_sender():
    assert policy.is_self_sender("robie@streetsmart.insurance") is True
    assert policy.is_self_sender("carlo@streetsmart.insurance") is False


def test_explicitly_allowed_senders():
    assert policy.is_allowed_sender("carlo@streetsmart.insurance") is True
    assert policy.is_allowed_sender("jake@streetsmart.insurance") is True


def test_other_agency_addresses_allowed():
    assert policy.is_allowed_sender("nicole@streetsmart.insurance") is True
    assert policy.is_allowed_sender("someone@streetsmartinsurance.com") is True


def test_outsiders_rejected():
    assert policy.is_allowed_sender("vendor@gmail.com") is False
    assert policy.is_allowed_sender("") is False
