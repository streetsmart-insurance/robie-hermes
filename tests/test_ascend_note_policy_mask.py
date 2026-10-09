"""Policy digits are masked in Ascend note text (stuck FA Group notice, 2026-10-09).

The phone-number filter is unchanged. It reads 1278263263 inside CT1278263263-2 as
a number, so the note builder masks policy digits the way cert_intake does.
"""
import pytest

from robie_job_engine import ascend_notice_triage as triage


# ---------------------------------------------------------------------------
# Policy digits are masked in note text (the stuck FA Group notice, 2026-10-09):
# the phone-number filter reads 1278263263 inside CT1278263263-2 as a number.


def test_mask_policy_matches_the_cert_intake_rule():
    from robie_job_engine.cert_intake import _mask_policy_for_note

    for policy in ("CT1278263263-2", "MXL0446256", "HO-998877", "ABC12", "WC5-33S-B276B9-026"):
        assert triage.mask_policy_for_note(policy) == _mask_policy_for_note(policy)
    assert triage.mask_policy_for_note("CT1278263263-2") == "CT" + "\u2022" * 8 + "63-2"


@pytest.mark.parametrize("notice_type", sorted(set(triage._NOTICE_HEADINGS) | {triage.CANCELLATION}))
def test_stuck_example_note_passes_the_unchanged_phone_filter(notice_type):
    from robie_job_engine import ezlynx_discussions as discussions

    note = triage.build_staff_note(
        notice_type,
        "Ascend notice",
        "Past-due payment of $241.00 which was due on 10/01/2026.\nCancel on 10/20/2026.",
        ["CT1278263263-2"],
        "FA Group LLC",
    )
    assert "CT1278263263" not in note
    assert "1278263263" not in note
    assert discussions.reject_phone_numbers(note) == note


def test_phone_filter_is_unchanged_and_still_refuses_the_raw_policy_number():
    from robie_job_engine import ezlynx_discussions as discussions

    with pytest.raises(discussions.DiscussionApiError):
        discussions.reject_phone_numbers("Policy CT1278263263-2 is past due.")
    for number in ("732-995-3409", "(732) 995-3409", "+1 732 995 3409"):
        with pytest.raises(discussions.DiscussionApiError):
            discussions.reject_phone_numbers(f"reach them at {number}")


def test_late_payment_note_for_the_stuck_example_names_the_masked_policy():
    note = triage.build_staff_note(
        triage.LATE_PAYMENT, "s", "Past-due payment of $241.00 which was due on 10/01/2026.",
        ["CT1278263263-2"], "FA Group LLC",
    )
    assert "Policy CT" + "\u2022" * 8 + "63-2 is past due" in note
