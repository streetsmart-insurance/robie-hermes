"""Tests for ascend_notice_triage and the find_program_by_policy error fix.

Uses a fake transport: no network, no credentials.
"""

import pytest

from robie_job_engine.ascend_api import AscendApiClient, AscendApiError
from robie_job_engine import ascend_notice_triage as triage
from robie_job_engine import zapier_tasks


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


def test_classify_new_in_ascend_product_mail_is_ignored_type():
    subjects = (
        "New in Ascend: Controls Around Interest Rates",
        "New in Ascend: Controls Around Interest Rates and how loans show them",
        "NEW IN ASCEND: Faster reporting",
        "Re: New in Ascend: Controls Around Interest Rates",
        "Fwd: New in Ascend: Controls Around Interest Rates",
        "FW: Re: New in Ascend: checkout updates",
    )
    for subject in subjects:
        assert triage.classify_notice(subject, "") == triage.PRODUCT_MAIL, subject
        # A product subject that mentions a filing phrase still stays product mail.
        assert (
            triage.classify_notice(
                subject,
                "Shoreline Builders LLC has a past-due payment of $3,528.22",
            )
            == triage.PRODUCT_MAIL
        ), subject
    assert triage.PRODUCT_MAIL in triage.IGNORE_TYPES
    # A real notice that only mentions the phrase later is not product mail.
    assert (
        triage.classify_notice(
            "Past due payment for Shoreline Builders LLC",
            "See New in Ascend: Controls Around Interest Rates",
        )
        == triage.LATE_PAYMENT
    )
    assert (
        triage.classify_notice(
            "Payment failed for New in Ascend LLC",
            "",
        )
        == triage.LATE_PAYMENT
    )
    assert (
        triage.classify_notice(
            "",
            "New in Ascend: Controls Around Interest Rates",
        )
        == triage.UNKNOWN
    )


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


def test_insured_name_line_does_not_add_a_period():
    assert triage.insured_name_line("Shoreline Builders LLC") == "Shoreline Builders LLC"
    assert triage.insured_name_line("Shoreline Builders LLC.") == "Shoreline Builders LLC."
    assert triage.insured_name_line("Acme Transport Inc") == "Acme Transport Inc"
    assert triage.insured_name_line("Acme Transport Inc.") == "Acme Transport Inc."
    assert triage.build_staff_note(
        triage.LATE_PAYMENT,
        "Past due payment for Shoreline Builders LLC.",
        "Past-due payment of $10.00 which was due on 09/11/2026.\nPolicy ID ABC12345\n",
        ["ABC12345"],
        "Shoreline Builders LLC.",
    ).endswith("Shoreline Builders LLC.")
    note = triage.build_staff_note(
        triage.LATE_PAYMENT,
        "Past due payment for Acme Transport Inc",
        "Past-due payment of $10.00 which was due on 09/11/2026.\nPolicy ID ABC12345\n",
        ["ABC12345"],
        "Acme Transport Inc",
    )
    assert note.endswith("Acme Transport Inc")
    assert not note.endswith("Acme Transport Inc.")
    assert "Robie was here" not in note


def test_extract_insured_name_from_subject():
    assert (
        triage.extract_insured_name("Past due payment for Shoreline Builders LLC", "")
        == "Shoreline Builders LLC"
    )


def test_format_money_amount_under_over_and_cents():
    # Under $1,000, including the leading-zero bug "$0,241.00".
    assert triage.format_money_amount("0,241.00") == "$241.00"
    assert triage.format_money_amount("$0,241.00") == "$241.00"
    assert triage.format_money_amount("241.00") == "$241.00"
    assert triage.format_money_amount("$83.62") == "$83.62"
    # Over $1,000 keeps the thousands separator.
    assert triage.format_money_amount("3,528.22") == "$3,528.22"
    assert triage.format_money_amount("1000.50") == "$1,000.50"
    assert triage.format_money_amount("$8,155.52") == "$8,155.52"
    # Cents stay two digits.
    assert triage.format_money_amount("12.50") == "$12.50"
    assert triage.format_money_amount("1,234.56") == "$1,234.56"
    assert triage.format_money_amount("241") == "$241.00"
    assert triage._first_money("past-due payment of $0,241.00, due on 10/01/2026") == (
        "$241.00"
    )
    assert triage.format_money_amount("") is None
    assert triage.format_money_amount("not-money") is None


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
    assert result["recommendation"]["ezlynx_workflow"] == "Ascend NOC"
    assert result["note_text"] == (
        "LATE PAYMENT notice from Ascend. "
        "Policy MXL•••••56 is past due: $3,528.22 was due 09/11/2026.\n"
        "Shoreline Builders LLC"
    )
    assert "past_due" not in result["note_text"]
    assert "Email subject:" not in result["note_text"]
    assert "Ascend program" not in result["note_text"]
    assert SAMPLE_UUID not in result["note_text"]
    assert result["program_uuid"] == SAMPLE_UUID


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


def test_triage_new_in_ascend_product_mail_is_ignored_not_human_review():
    class _Boom:
        def get_program(self, *_args, **_kwargs):
            raise AssertionError("product mail must not call Ascend")

        def find_program_by_policy(self, *_args, **_kwargs):
            raise AssertionError("product mail must not call Ascend")

    result = triage.triage_notice(
        _Boom(),
        "New in Ascend: Controls Around Interest Rates",
        "Shoreline Builders LLC has a past-due payment of $3,528.22, due on 09/11/2026.",
    )
    assert result["notice_type"] == triage.PRODUCT_MAIL
    assert result["needs_human_review"] is False
    assert result["ignored"] is True
    assert result["review_reason"] == ""
    assert result["recommendation"]["zapier_task"] is False
    assert result["recommendation"]["ezlynx_workflow"] is None
    assert result["note_text"] == (
        "PRODUCT MAIL notice from Ascend. This is an Ascend product update."
    )
    # Unrecognized mail that is not product mail still needs a person.
    unknown = triage.triage_notice(_Boom(), "Your monthly statement", "nothing useful")
    assert unknown["notice_type"] == triage.UNKNOWN
    assert unknown["needs_human_review"] is True
    assert unknown["review_reason"] == "Unrecognized Ascend notice type"


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
    assert result["recommendation"]["ezlynx_label"] is None
    assert result["note_text"].startswith(
        "NON-PAY CANCELLATION notice from Ascend. The policy was canceled on 06/22/2026."
    )
    assert "Email subject:" not in result["note_text"]
    assert result["needs_human_review"] is False  # program resolved; action still advisory


# ---------------------------------------------------------------------------
# Cancellation -> Zapier task payload
# ---------------------------------------------------------------------------


def _cancellation_result():
    subject = (
        "The coverage policy for Stafford Adult Softball League LLC has been "
        "canceled due to non-payment"
    )
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
    return triage.triage_notice(make_client(routes=routes), subject, body)


def test_cancellation_recommendation_flags_zapier_task():
    result = _cancellation_result()
    assert result["recommendation"].get("zapier_task") is True


def test_cancellation_recommendation_encodes_alejandro_rules():
    # Alejandro (2026-09-14): manual policies only, Cancel Confirmation
    # always, return premium from the notice or $0, check History/notes
    # for duplicates, notes in the auto-run cancellation WF.
    instruction = _cancellation_result()["recommendation"]["instruction"]
    assert "Cancel Confirmation" in instruction
    assert "$0" in instruction
    assert "History of transactions" in instruction


def test_late_payment_uses_ascend_noc_label_and_est_cutoff():
    # Alejandro (2026-09-14): the label is spelled "Ascend NOC";
    # no task assignment after 4:30 PM EST.
    rec = triage.recommended_action(triage.LATE_PAYMENT)
    assert rec["ezlynx_workflow"] == "Ascend NOC"
    assert rec["ezlynx_label"] == "Ascend NOC"
    assert "4:30 PM EST" in rec["instruction"]


def test_build_cancellation_task_payload():
    result = _cancellation_result()
    payload = triage.build_cancellation_task_payload(
        result, applicant_id="220250093", account_csr="Erika Palacios",
        due_date="2026-09-20",
    )
    assert payload["applicant_id"] == "220250093"
    assert payload["assignee"] == "Erika Palacios"
    assert payload["source"] == "inbox-triage"
    assert payload["due_date"] == "2026-09-20"
    assert "Stafford Adult Softball League LLC" in payload["task_title"]
    assert payload["notice_type"] == triage.CANCELLATION
    assert payload["program_uuid"] == SAMPLE_UUID
    assert "canceled" in payload["email_subject"]


def test_build_cancellation_task_payload_rejects_bad_due_date():
    result = _cancellation_result()
    with pytest.raises(ValueError):
        triage.build_cancellation_task_payload(
            result, applicant_id="220250093", account_csr="Erika Palacios",
            due_date="tomorrow",
        )
    with pytest.raises(ValueError):
        triage.build_cancellation_task_payload(
            result, applicant_id="220250093", account_csr="Erika Palacios",
            due_date="",
        )


def test_build_cancellation_task_payload_rejects_wrong_type():
    subject, body = _late_payment_email()
    routes = {
        ("GET", f"/programs/{SAMPLE_UUID}"): {
            "data": {"id": SAMPLE_UUID, "status": "past_due"}
        },
    }
    result = triage.triage_notice(make_client(routes=routes), subject, body)
    with pytest.raises(ValueError):
        triage.build_cancellation_task_payload(
            result, applicant_id="220250093", account_csr="Erika Palacios",
            due_date="2026-09-20",
        )


def test_build_cancellation_task_payload_requires_ids():
    result = _cancellation_result()
    with pytest.raises(ValueError):
        triage.build_cancellation_task_payload(result, applicant_id="", account_csr="Erika Palacios", due_date="2026-09-20")
    with pytest.raises(ValueError):
        triage.build_cancellation_task_payload(result, applicant_id="220250093", account_csr="", due_date="2026-09-20")


# ---------------------------------------------------------------------------
# zapier_tasks firing helper
# ---------------------------------------------------------------------------


def test_fire_task_dry_run_validates_without_firing():
    payload = {
        "applicant_id": "220250093",
        "task_title": "Ascend cancellation notice - Test LLC",
        "assignee": "Erika Palacios",
        "source": "inbox-triage",
        "due_date": "2026-09-20",
    }
    result = zapier_tasks.fire_task(payload, dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True


def test_fire_task_rejects_incomplete_payload():
    with pytest.raises(ValueError):
        zapier_tasks.fire_task({"task_title": "no ids"}, dry_run=True)

