"""Every redacted Ascend notice fixture classifies and extracts policy numbers.

The JSON files are synthetic ("Fixture Insured A LLC", scrambled policy
digits, fake UUIDs). They are not a second copy of production mail.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from robie_job_engine import ascend_notice_triage as triage

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ascend_notices"

# family on the fixture -> notice type the driver must use.
FAMILY_TYPE = {
    "intent_to_cancel_copy": triage.INTENT_TO_CANCEL,
    "loan_canceled_nonpayment": triage.CANCELLATION,
    "past_due_payment": triage.LATE_PAYMENT,
    "payment_failed": triage.LATE_PAYMENT,
    "return_premium_received": triage.RETURN_PREMIUM,
    "processing_payment": triage.PROCESSING_PAYMENT,
    "payment_confirmation_copy": triage.PAYMENT_CONFIRMATION,
    "refund_initiated": triage.REFUND,
    "refund_to_customer": triage.REFUND,
    "potential_policies_unpurchased": triage.POTENTIAL_POLICIES,
    "programs_ready": triage.PROGRAMS_READY,
    "underwriting_request": triage.UNDERWRITING,
    "underwriting_counteroffer": triage.UNDERWRITING,
    "loan_paid_off": triage.PAID_OFF,
    "disputed_charge": triage.UNKNOWN,
    "reinstatement_approved": triage.UNKNOWN,
}

# Frozen Policy ID captures. Spaced ("Policy ID X") and jammed
# ("Policy IDXEffective") both have to keep these values.
EXPECTED_POLICY_NUMBERS = {
    "disputed_charge_01.json": [],
    "intent_to_cancel_copy_01.json": ["DSLA97258206-00"],
    "intent_to_cancel_copy_02.json": ["CPS6534227"],
    "intent_to_cancel_copy_03.json": ["23611941"],
    "intent_to_cancel_copy_04.json": ["BCGL86103185"],
    "intent_to_cancel_copy_05.json": ["BDG964786692"],
    "intent_to_cancel_copy_06.json": ["NBP5050152A"],
    "intent_to_cancel_copy_07.json": ["SUB7255255-6"],
    "intent_to_cancel_copy_08.json": [],
    "intent_to_cancel_copy_09.json": ["WS362630"],
    "intent_to_cancel_copy_10.json": ["DSLA97258206-00"],
    "loan_canceled_nonpayment_01.json": ["CPS6534227"],
    "loan_paid_off_01.json": ["PAV1425221"],
    "past_due_payment_01.json": ["GAT5643640-26"],
    "past_due_payment_02.json": ["NN0851210"],
    "past_due_payment_03.json": ["NN0851210"],
    "past_due_payment_04.json": ["NN0851210"],
    "payment_confirmation_copy_01.json": ["DSLA97258206-00"],
    "payment_confirmation_copy_02.json": ["NRG-DBG-GL46021"],
    "payment_confirmation_copy_03.json": ["AVBWA"],
    "payment_confirmation_copy_04.json": ["381346"],
    "payment_confirmation_copy_05.json": ["CPS6356718"],
    "payment_failed_01.json": ["DSLA97258206-00"],
    "payment_failed_02.json": ["AHVDL-X"],
    "payment_failed_03.json": ["AHVDL-X"],
    "payment_failed_04.json": ["CBL58682451P-85"],
    "potential_policies_unpurchased_01.json": [],
    "potential_policies_unpurchased_02.json": [],
    "potential_policies_unpurchased_03.json": [],
    "potential_policies_unpurchased_04.json": [],
    "potential_policies_unpurchased_05.json": [],
    "potential_policies_unpurchased_06.json": [],
    "processing_payment_01.json": ["NRG-DBG-GL46021"],
    "processing_payment_02.json": [],
    "processing_payment_03.json": ["0199940"],
    "processing_payment_04.json": ["Q080431"],
    "programs_ready_01.json": [],
    "programs_ready_02.json": [],
    "refund_initiated_01.json": ["231776-300APD-92181-SSRM"],
    "refund_initiated_02.json": ["ADTIS-R"],
    "refund_initiated_03.json": ["WS679493"],
    "refund_initiated_04.json": ["WS362630"],
    "refund_to_customer_01.json": [],
    "refund_to_customer_02.json": [],
    "refund_to_customer_03.json": [],
    "reinstatement_approved_01.json": [],
    "return_premium_received_01.json": ["2AB975879"],
    "return_premium_received_02.json": ["EZXS602697"],
    "return_premium_received_03.json": ["PAV1425221"],
    "return_premium_received_04.json": ["WS679493"],
    "underwriting_counteroffer_01.json": [],
    "underwriting_counteroffer_02.json": [],
    "underwriting_request_01.json": [],
    "underwriting_request_02.json": [],
}


def _load_all():
    paths = sorted(FIXTURE_DIR.glob("*.json"))
    assert len(paths) == 54, len(paths)
    loaded = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data) == {"family", "from", "subject", "mime_structure", "text_plain"}
        loaded.append((path.name, data))
    return loaded


def test_fixture_count_and_schema():
    loaded = _load_all()
    assert len(loaded) == 54
    assert set(EXPECTED_POLICY_NUMBERS) == {name for name, _ in loaded}


@pytest.mark.parametrize("name,data", _load_all(), ids=[name for name, _ in _load_all()])
def test_fixture_family_classifies_to_notice_type(name, data):
    notice_type = triage.classify_notice(data["subject"], data["text_plain"])
    assert notice_type == FAMILY_TYPE[data["family"]], name
    assert triage.extract_policy_numbers(data["text_plain"]) == EXPECTED_POLICY_NUMBERS[name]


def test_spaced_and_jammed_policy_id_forms():
    assert triage.extract_policy_numbers(
        "Policy ID ABC123-00\nEffective date 01/01/2026"
    ) == ["ABC123-00"]
    assert triage.extract_policy_numbers(
        "Policy IDABC123Effective date01/01/2026"
    ) == ["ABC123"]
    by_name = dict(_load_all())
    jammed = by_name["intent_to_cancel_copy_02.json"]["text_plain"]
    assert "Policy IDCPS6534227Effective" in jammed
    assert triage.extract_policy_numbers(jammed) == ["CPS6534227"]
    spaced = by_name["past_due_payment_01.json"]["text_plain"]
    assert "Policy ID GAT5643640-26" in spaced
    assert triage.extract_policy_numbers(spaced) == ["GAT5643640-26"]


def test_intent_subject_wins_over_cancellation_word_and_never_builds_a_task():
    by_name = dict(_load_all())
    for name, data in by_name.items():
        if data["family"] != "intent_to_cancel_copy":
            continue
        notice_type = triage.classify_notice(data["subject"], data["text_plain"])
        assert notice_type == triage.INTENT_TO_CANCEL, name
        with pytest.raises(ValueError, match="Intent-to-cancel"):
            triage.build_cancellation_task_payload(
                {"notice_type": notice_type, "email_subject": data["subject"]},
                applicant_id="220250093",
                account_csr="KarlaSS",
                due_date="2026-10-06",
            )
    # The generic "cancellation" subject is not allowed to win this one.
    assert (
        triage.classify_notice(
            "[URGENT] Policy(s) at risk for cancellation",
            "The loan has been canceled effective 01/01/2026.",
        )
        == triage.INTENT_TO_CANCEL
    )


def test_ignore_types_do_not_ask_for_human_review():
    class _Boom:
        def get_program(self, *_args, **_kwargs):
            raise AssertionError("ignored mail must not call Ascend")

        def find_program_by_policy(self, *_args, **_kwargs):
            raise AssertionError("ignored mail must not call Ascend")

    saw_ignore = False
    saw_unknown = False
    for _name, data in _load_all():
        notice_type = FAMILY_TYPE[data["family"]]
        if notice_type not in triage.IGNORE_TYPES and notice_type != triage.UNKNOWN:
            continue
        result = triage.triage_notice(_Boom(), data["subject"], data["text_plain"])
        assert result["notice_type"] == notice_type
        if notice_type in triage.IGNORE_TYPES:
            saw_ignore = True
            assert result["needs_human_review"] is False
            assert result.get("ignored") is True
        elif notice_type == triage.UNKNOWN:
            saw_unknown = True
            assert result["needs_human_review"] is True
    assert saw_ignore and saw_unknown


class _ResolvedProgram:
    def __init__(self, status):
        self.status = status

    def get_program(self, program_uuid):
        return {"id": program_uuid, "status": self.status}

    def find_program_by_policy(self, policy_number):
        return {"program": {"status": self.status}, "program_id": "prog-fixture"}


def test_cancellation_fixture_note_is_plain_and_skips_the_label():
    data = dict(_load_all())["loan_canceled_nonpayment_01.json"]
    result = triage.triage_notice(
        _ResolvedProgram("canceled"), data["subject"], data["text_plain"]
    )
    assert result["notice_type"] == triage.CANCELLATION
    assert result["recommendation"]["ezlynx_label"] is None
    assert result["note_text"] == (
        "NON-PAY CANCELLATION notice from Ascend. "
        "Policy CPS6534227 was canceled on 09/23/2026.\n"
        "Fixture Insured A LLC still has an overdue balance of $133.42.\n"
        "The loan was canceled because the payment was not made."
    )
    for banned in ("Email subject:", "Insured:", "Policies:", "Amount:", "Ascend program"):
        assert banned not in result["note_text"]


def test_late_payment_fixture_note():
    data = dict(_load_all())["past_due_payment_01.json"]
    result = triage.triage_notice(
        _ResolvedProgram("past_due"), data["subject"], data["text_plain"]
    )
    assert result["notice_type"] == triage.LATE_PAYMENT
    assert result["program_uuid"] == "51e835b5-b6ae-43a1-ab48-cef007d242db"
    assert result["note_text"] == (
        "LATE PAYMENT notice from Ascend. "
        "Policy GAT5643640-26 is past due: $241.00 was due 10/01/2026.\n"
        "Fixture Insured A LLC."
    )
    for banned in (
        "Email subject:",
        "Insured:",
        "Policies:",
        "Amount:",
        "Date:",
        "Ascend program",
        "past_due",
        "$0,241.00",
        "51e835b5-b6ae-43a1-ab48-cef007d242db",
    ):
        assert banned not in result["note_text"]


# One rendered note per notice type. Filing types use a resolved program.
# Informational and unrecognized mail still render a note; the driver does
# not file them.
EXPECTED_NOTES = {
    "loan_canceled_nonpayment_01.json": (
        "NON-PAY CANCELLATION notice from Ascend. "
        "Policy CPS6534227 was canceled on 09/23/2026.\n"
        "Fixture Insured A LLC still has an overdue balance of $133.42.\n"
        "The loan was canceled because the payment was not made."
    ),
    "past_due_payment_01.json": (
        "LATE PAYMENT notice from Ascend. "
        "Policy GAT5643640-26 is past due: $241.00 was due 10/01/2026.\n"
        "Fixture Insured A LLC."
    ),
    "payment_failed_01.json": (
        "LATE PAYMENT notice from Ascend. "
        "Policy DSLA97258206-00 payment failed: $567.65 could not be processed.\n"
        "Fixture Insured B LLC."
    ),
    "payment_failed_02.json": (
        "LATE PAYMENT notice from Ascend. "
        "Policy AHVDL-X payment failed: $59.79 could not be processed.\n"
        "Fixture Insured A LLC."
    ),
    "payment_failed_04.json": (
        "LATE PAYMENT notice from Ascend. "
        "Policy CBL58682451P-85 payment failed: $290.87 could not be processed.\n"
        "Fixture Insured A LLC."
    ),
    "intent_to_cancel_copy_01.json": (
        "INTENT TO CANCEL notice from Ascend. "
        "Policy DSLA97258206-00 is at risk of cancellation. "
        "$525.30 was due 09/21/2026. "
        "Coverage cancels on 10/14/2026 if it stays unpaid."
    ),
    "return_premium_received_01.json": (
        "RETURN PREMIUM notice from Ascend. "
        "Policy 2AB975879. Ascend received $8,155.52 to apply to the loan.\n"
        "Fixture Insured A LLC."
    ),
    "payment_confirmation_copy_01.json": (
        "PAYMENT CONFIRMATION notice from Ascend. "
        "Policy DSLA97258206-00. Payment of $567.65 was received.\n"
        "Fixture Insured B LLC."
    ),
    "processing_payment_01.json": (
        "PROCESSING PAYMENT notice from Ascend. "
        "Policy NRG-DBG-GL46021. A payment of $6,628.98 is processing.\n"
        "Fixture Insured A LLC."
    ),
    "refund_to_customer_01.json": (
        "REFUND notice from Ascend. "
        "A refund of $989.91 was issued.\n"
        "Fixture Insured A LLC."
    ),
    "refund_initiated_01.json": (
        "REFUND notice from Ascend. "
        "Policy 231776-300APD-92181-SSRM. A refund of $5.51 was stopped.\n"
        "Fixture Other LLC."
    ),
    "potential_policies_unpurchased_01.json": (
        "POTENTIAL POLICIES notice from Ascend. "
        "Some policies produced recently have not been purchased."
    ),
    "programs_ready_01.json": (
        "PROGRAMS READY notice from Ascend. "
        "Coverage will end within 90 days and can be renewed.\n"
        "Fixture Insured A LLC."
    ),
    "underwriting_request_01.json": (
        "UNDERWRITING notice from Ascend. "
        "An underwriting request is in review.\n"
        "Fixture Insured A LLC."
    ),
    "underwriting_counteroffer_01.json": (
        "UNDERWRITING notice from Ascend. "
        "A counteroffer was approved on an underwriting request.\n"
        "Fixture Insured A LLC."
    ),
    "loan_paid_off_01.json": (
        "LOAN PAID OFF notice from Ascend. "
        "Policy PAV1425221. The loan is paid in full.\n"
        "Fixture Insured A LLC."
    ),
    "disputed_charge_01.json": (
        "UNRECOGNIZED notice from Ascend. "
        "A customer disputed a payment of $165.27.\n"
        "Fixture Insured A LLC."
    ),
    "reinstatement_approved_01.json": (
        "UNRECOGNIZED notice from Ascend. "
        "A reinstatement was requested. The carrier still has to accept it.\n"
        "Fixture Insured A LLC."
    ),
}


class _NoApi:
    def get_program(self, *_args, **_kwargs):
        raise AssertionError("this notice must not call Ascend")

    def find_program_by_policy(self, *_args, **_kwargs):
        raise AssertionError("this notice must not call Ascend")


def _triage_fixture(data):
    notice_type = FAMILY_TYPE[data["family"]]
    if notice_type in triage.IGNORE_TYPES or notice_type == triage.UNKNOWN:
        client = _NoApi()
    else:
        client = _ResolvedProgram("active")
    return triage.triage_notice(client, data["subject"], data["text_plain"])


def test_each_notice_type_renders_plain_english():
    by_name = dict(_load_all())
    uuid_re = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.IGNORECASE,
    )
    banned = (
        "Email subject:",
        "Insured:",
        "Policies:",
        "Amount:",
        "Date:",
        "Ascend program",
        "past_due",
        "$0,",
    )
    for name, expected in EXPECTED_NOTES.items():
        result = _triage_fixture(by_name[name])
        assert result["note_text"] == expected, name
        if result["notice_type"] in triage.IGNORE_TYPES:
            assert result.get("ignored") is True
            assert result["needs_human_review"] is False
        if result["notice_type"] == triage.UNKNOWN:
            assert result["needs_human_review"] is True
    for name, data in by_name.items():
        result = _triage_fixture(data)
        note = result["note_text"]
        assert note, name
        assert "notice from Ascend." in note, name
        for token in banned:
            assert token not in note, (name, token)
        assert uuid_re.search(note) is None, name
        if result.get("program_uuid"):
            assert result["program_uuid"] not in note
        if result["notice_type"] == triage.INTENT_TO_CANCEL:
            assert "was canceled" not in note.lower()
            assert "NON-PAY CANCELLATION" not in note
    # Types with no fixture still use the same sentences.
    assert triage.build_staff_note(
        triage.NEW_PROGRAM,
        "Finance agreement for Fixture Insured A LLC",
        "Policy ID ABC123\n",
        ["ABC123"],
        "Fixture Insured A LLC",
    ) == (
        "NEW PROGRAM notice from Ascend. "
        "Policy ABC123. Ascend opened a new premium finance program.\n"
        "Fixture Insured A LLC."
    )
    assert triage.build_staff_note(triage.SIGN_IN, "Sign in to Ascend", "", [], None) == (
        "SIGN IN notice from Ascend. This is a sign-in message."
    )
    assert triage.build_staff_note(triage.MSA, "Your MSA is ready", "", [], None) == (
        "MSA notice from Ascend. This is a master service agreement message."
    )


def test_sign_in_and_msa_are_explicit_ignores():
    assert triage.classify_notice("Sign in to Ascend", "") == triage.SIGN_IN
    assert triage.classify_notice("", "Please sign in to continue") == triage.SIGN_IN
    assert triage.classify_notice("Your MSA is ready", "") == triage.MSA
    assert (
        triage.classify_notice("", "Master service agreement attached") == triage.MSA
    )
    for subject in ("Sign in to Ascend", "MSA update"):
        result = triage.triage_notice(object(), subject, "")
        assert result["needs_human_review"] is False
        assert result["ignored"] is True
