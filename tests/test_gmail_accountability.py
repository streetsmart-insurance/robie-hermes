from datetime import datetime, timezone

import pytest

from robie_job_engine.gmail_accountability import (
    GmailAccountabilityError,
    approved_mailboxes_from_role_registry,
    collect_agency_summary,
    mailbox_allowlist,
    summarize_mailbox_threads,
)


AS_OF = datetime(2026, 8, 30, 17, tzinfo=timezone.utc)


def _thread(thread_id: str, sender: str, when_ms: int):
    return {
        "id": thread_id,
        "messages": [{
            "internalDate": str(when_ms),
            "payload": {"headers": [{"name": "From", "value": sender}]},
        }],
    }


def _millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def test_metadata_summary_classifies_reply_owner_without_bodies_or_subjects():
    summary = summarize_mailbox_threads(
        "jackie@streetsmart.insurance",
        [
            _thread("customer-last", "Client <client@example.com>", _millis(datetime(2026, 8, 28, 17, tzinfo=timezone.utc))),
            _thread("employee-last", "Jackie <jackie@streetsmart.insurance>", _millis(datetime(2026, 8, 30, 12, tzinfo=timezone.utc))),
        ],
        as_of=AS_OF,
    )
    assert summary["awaiting_employee"] == 1
    assert summary["awaiting_customer"] == 1
    assert summary["stalled_threads"] == 1
    assert "subject" not in str(summary).lower()
    assert "body" not in str(summary).lower()


def test_mailbox_allowlist_is_explicit_normalized_and_deduplicated():
    assert mailbox_allowlist(" Jackie@StreetSmart.Insurance, jazmin@streetsmart.insurance, jackie@streetsmart.insurance ") == (
        "jackie@streetsmart.insurance",
        "jazmin@streetsmart.insurance",
    )


def test_approved_mailboxes_are_derived_from_complete_active_role_registry():
    assert approved_mailboxes_from_role_registry({
        "source_status": "available",
        "employees": {
            "Jazmin Molina": {"email": "Jazmin@StreetSmart.Insurance"},
            "Karla Brown": {"email": "karla@streetsmart.insurance"},
        },
    }) == (
        "jazmin@streetsmart.insurance",
        "karla@streetsmart.insurance",
    )


def test_approved_mailboxes_fail_closed_when_roster_email_is_missing():
    with pytest.raises(GmailAccountabilityError, match="missing work email"):
        approved_mailboxes_from_role_registry({
            "source_status": "available",
            "employees": {"Jazmin Molina": {"email": ""}},
        })


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Threads:
    def list(self, **_kwargs):
        return _Request({"threads": []})


class _Users:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def getProfile(self, **_kwargs):
        return _Request({"emailAddress": self.mailbox})

    def threads(self):
        return _Threads()


class _Service:
    def __init__(self, mailbox):
        self.mailbox = mailbox

    def users(self):
        return _Users(self.mailbox)


def test_agency_summary_verifies_every_roster_mailbox_with_metadata_only():
    summary = collect_agency_summary(
        environment={"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "hermes@test.invalid"},
        approved_users=["karla@streetsmart.insurance", "jazmin@streetsmart.insurance"],
        service_factory=lambda _service_account, mailbox: _Service(mailbox),
        as_of=AS_OF,
    )
    assert summary["source_status"] == "available"
    assert summary["mailboxes_verified"] == 2
    assert summary["allowlist_source"] == "approved_active_employee_roster"
    assert summary["scope"].endswith("gmail.metadata")
    assert summary["body_access"] is False


def test_agency_summary_fails_closed_on_delegated_mailbox_mismatch():
    with pytest.raises(GmailAccountabilityError, match="verification mismatch"):
        collect_agency_summary(
            environment={"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "hermes@test.invalid"},
            approved_users=["karla@streetsmart.insurance"],
            service_factory=lambda *_args: _Service("someone-else@streetsmart.insurance"),
            as_of=AS_OF,
        )
