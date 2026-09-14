"""Tests for ascend_notice_triage and the find_program_by_policy error fix.

Uses a fake transport: no network, no credentials.
"""

import pytest

from robie_job_engine.ascend_api import AscendApiClient, AscendApiError
from robie_job_engine import ascend_notice_triage as triage


SAMPLE_UUID = "e46ca1f6-1e2d-491b-8944-3cf06e82e312"
SAMPLE_BILLABLE_ID = "4d774d15-4fe0-4442-8053-074e7ebaede5"


class FakeTransport:
    """Canned Ascend API responses keyed by (method, path)."""

    def __init__(self, routes=None, fail_with=None):
        self.routes = routes or {}
        self.fail_with = fail_with
        self.calls = []

    def request(self, method, path, *, query=None, json_body=None):
        self.calls.append((method, path, query))
        if self.fail_with is not None:
            raise self.fail_with
        key = (method, path)
        if key in self.routes:
            return self.routes[key]
        return {}


def make_client(routes=None, fail_with=None):
    return AscendApiClient(FakeTransport(routes=routes, fail_with=fail_with))


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def test_classify_late_payment_subject():
    assert (
        triage.classify_notice("Past due payment for Shoreline Builders LLC", "")
        == triage.LATE_PAYMENT
    )


def test_classify_late_payment_body():
    assert (
        triage.classify_notice(
            "Statement", "Shoreline Builders LLC has a past-due payment of $3,528.22"
        )
        == triage.LATE_PAYMENT
    )


def test_classify_cancellation_subject():
    assert (
        triage.classify_notice(
            "The coverage policy for Stafford Adult Softball League LLC has been "
            "canceled due to non-payment",
            "",
        )
        == triage.CANCELLATION
    )


def test_classify_return_premium_subject():
    assert (
        triage.classify_notice("Return premium received for Policy CVX0005542A", "")
        == triage.RETURN_PREMIUM
    )


def test_classify_unknown_goes_to_human():
    assert triage.classify_notice("Your monthly statement is ready", "hello") == triage.UNKNOWN
    assert triage.classify_notice("", "") == triage.UNKNOWN


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_extract_program_uuid_from_dashboard_link():
    text = f"View details ( https://dashboard.useascend.com/programs/{SAMPLE_UUID} )"
    assert triage.extract_program_uuid(text) == SAMPLE_UUID


def test_extract_program_uuid_rejects_garbage():
    assert triage.extract_program_uuid("no link here") is None
    assert (
        triage.extract_program_uuid(
            "https://dashboard.useascend.com/programs/not-a-uuid-at-all-xxxx"
        )
        is None
    )


def test_extract_policy_numbers_deduped():
    body = "Policy IDMXL0446256 blah Policy IDMGL0200092 blah Policy IDMXL0446256"
    assert triage.extract_policy_numbers(body) == ["MXL0446256", "MGL0200092"]


def test_extract_insured_name_from_subject():
    assert (
        triage.extract_insured_name("Past due payment for Shoreline Builders LLC", "")
        == "Shoreline Builders LLC"
    )


# ---------------------------------------------------------------------------
# find_program_by_policy error semantics (regression: never swallow failures)
# ---------------------------------------------------------------------------


def test_find_program_by_policy_raises_on_transport_failure():
    client = make_client(fail_with=AscendApiError(None, "boom"))
    with pytest.raises(AscendApiError):
        client.find_program_by_policy("MXL0446256")


def test_find_program_by_policy_returns_none_when_truly_missing():
    routes = {
        ("GET", "/billables"): {"data": []},
        ("GET", "/programs"): {"data": []},
    }
    client = make_client(routes=routes)
    assert client.find_program_by_policy("NOPE123") is None


def test_find_program_by_policy_hits_billable_search():
    program_id = SAMPLE_UUID
    routes = {
        ("GET", "/billables"): {
            "data": [{"id": SAMPLE_BILLABLE_ID, "program_id": program_id}]
        },
        ("GET", f"/programs/{program_id}"): {"data": {"id": program_id, "status": "active"}},
    }
    client = make_client(routes=routes)
    found = client.find_program_by_policy("mxl0446256")
    assert found is not None
    assert found["program_id"] == program_id


def test_extract_program_uuid_static_helper():
    text = f"https://dashboard.useascend.com/programs/{SAMPLE_UUID}/reinstate"
    assert AscendApiClient.extract_program_uuid(text) == SAMPLE_UUID


# ---------------------------------------------------------------------------
# triage_notice end to end
# ---------------------------------------------------------------------------


def _late_payment_email():
    subject = "Past due payment for Shoreline Builders LLC"
    body = (
        "Hi Carlo,\n\nShoreline Builders LLC has a past-due payment of $3,528.22, "
        "which was due on 09/11/2026.\n\n"
        "CustomerShoreline Builders LLCReferenceI0HBLJHYIJTotal $3,528.22\n\n"
        "PolicyExcess Liability (RT Specialty)Policy IDMXL0446256Effective date08/20/2026\n\n"
        f"View details ( https://dashboard.useascend.com/programs/{SAMPLE_UUID} )\n"
    )
    return subject, body


def test_triage_late_payment_resolves_by_uuid():
    subject, body = _late_payment_email()
    routes = {
        ("GET", f"/programs/{SAMPLE_UUID}"): {
            "data": {"id": SAMPLE_UUID, "status": "past_due"}
        },
    }
    client = make_client(routes=routes)
    result = triage.triage_notice(client, subject, body)

    assert result["notice_type"] == triage.LATE_PAYMENT
    assert result["insured_name"] == "Shoreline Builders LLC"
    assert result["policy_numbers"] == ["MXL0446256"]
    assert result["program_uuid"] == SAMPLE_UUID
    assert result["lookup_method"] == "program_uuid_from_email"
    assert result["needs_human_review"] is False
    assert result["recommendation"]["ezlynx_workflow"] == "AscendNOC"
    assert "past_due" in result["note_text"]
    assert "$3,528.22" in result["note_text"]


def test_triage_api_failure_flags_human_review():
    subject, body = _late_payment_email()
    client = make_client(fail_with=AscendApiError(401, "unauthorized"))
    result = triage.triage_notice(client, subject, body)

    assert result["needs_human_review"] is True
    assert "failed" in result["review_reason"].lower()
    assert result["program"] is None


def test_triage_unknown_notice_flags_human_review():
    client = make_client()
    result = triage.triage_notice(client, "Your monthly statement", "nothing useful")
    assert result["notice_type"] == triage.UNKNOWN
    assert result["needs_human_review"] is True


def test_triage_no_resolvable_program_flags_human_review():
    subject = "Past due payment for Ghost LLC"
    body = "Ghost LLC has a past-due payment of $10.00, due on 09/11/2026."
    routes = {
        ("GET", "/billables"): {"data": []},
        ("GET", "/programs"): {"data": []},
    }
    client = make_client(routes=routes)
    result = triage.triage_notice(client, subject, body)
    assert result["needs_human_review"] is True
    assert "no ascend program" in result["review_reason"].lower()


def test_triage_cancellation_recommends_human_check():
    subject = "The coverage policy for Stafford Adult Softball League LLC has been canceled due to non-payment"
    body = (
        "Stafford Adult Softball League LLC canceled for non-payment and still has "
        "an overdue balance of $44.70. The loan has been canceled effective 06/22/2026.\n"
        f"Complete the cancelation here ( https://dashboard.useascend.com/programs/{SAMPLE_UUID} )."
    )
    routes = {
        ("GET", f"/programs/{SAMPLE_UUID}"): {
            "data": {"id": SAMPLE_UUID, "status": "canceled"}
        },
    }
    client = make_client(routes=routes)
    result = triage.triage_notice(client, subject, body)
    assert result["notice_type"] == triage.CANCELLATION
    assert result["recommendation"]["ezlynx_workflow"] == "Service-Cancellation"
    assert result["needs_human_review"] is False  # program resolved; action still advisory
