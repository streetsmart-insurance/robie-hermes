"""CR-1 regression tests. The first test is the 2026-09 incident itself."""

import pytest

from robie_guard import ActionIntent, CardinalViolation, assert_allowed, classify_intent
from robie_guard.destructive_guard import guarded_transaction_delete


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------

def test_the_incident_deleting_the_whole_policy_is_denied():
    """Agent was told 'remove the duplicate renewal transaction' and clicked the
    policy-level Delete. Correct intent, wrong target. Must be denied."""
    decision = classify_intent(ActionIntent(
        kind="click",
        intent="delete_renewal_transaction",   # the intent was RIGHT
        object_class="policy",                  # the target was WRONG
        target_text="Delete Policy",
        applicant_id="102351931",
        policy_number="GAT0003099-01",
    ))
    assert not decision.allowed
    assert decision.rule_id == "CR-1"
    assert "never-allowed phrase" in decision.reason


def test_under_described_destructive_click_is_denied():
    """No object_class declared. Denied for being unprovable, not allowed by default."""
    decision = classify_intent(ActionIntent(kind="click", target_text="Delete"))
    assert not decision.allowed


@pytest.mark.parametrize("object_class,text", [
    ("policy", "Delete"),
    ("applicant", "Remove"),
    ("document", "Delete"),
    ("contact", "Delete"),
    ("directory_entry", "Remove"),
    ("note", "Delete"),
    ("task", "Delete"),
    ("driver", "Remove"),
    ("vehicle", "Remove"),
    ("mortgagee", "Delete"),
])
def test_every_protected_class_is_undeletable(object_class, text):
    decision = classify_intent(ActionIntent(
        kind="click", intent="cleanup", object_class=object_class, target_text=text,
    ))
    assert not decision.allowed, f"{object_class} must never be deletable"


def test_http_delete_verb_is_denied_by_default():
    decision = classify_intent(ActionIntent(
        kind="http", method="DELETE",
        url="https://app.ezlynx.com/api/policy/12345",
    ))
    assert not decision.allowed


def test_delete_shaped_url_is_denied_even_with_post():
    decision = classify_intent(ActionIntent(
        kind="http", method="POST",
        url="https://app.ezlynx.com/applicantportal/DeletePolicy?id=9",
    ))
    assert not decision.allowed


# ---------------------------------------------------------------------------
# The one permitted deletion, and everything that must be true first
# ---------------------------------------------------------------------------

def _good_tx_delete(**overrides):
    base = dict(
        kind="click",
        intent="delete_renewal_transaction",
        object_class="policy_transaction",
        target_text="Delete",
        applicant_id="102351931",
        policy_number="GAT0003099-01",
        transaction_type="RWL",
        transaction_status="Pending",
        matched_row_count=1,
        duplicate_count=2,
        created_by="SSRobie",
        scope_confirmed=True,
        unlock_token="tx-del-102351931-1",
    )
    base.update(overrides)
    return ActionIntent(**base)


def test_duplicate_pending_rwl_delete_is_allowed_when_fully_proven():
    decision = classify_intent(_good_tx_delete())
    assert decision.allowed, decision.reason
    assert decision.rule_id == "CR-1-EXCEPTION"


@pytest.mark.parametrize("override,expect_in_reason", [
    ({"unlock_token": None}, "guarded_transaction_delete"),
    ({"scope_confirmed": False}, "scope_confirmed is False"),
    ({"matched_row_count": 2}, "resolved to 2 rows"),
    ({"matched_row_count": None}, "resolved to None rows"),
    ({"transaction_type": "NEW"}, "only RWL"),
    ({"transaction_status": "Active"}, "only a Pending shell"),
    ({"duplicate_count": 1}, "no duplicate to remove"),
    ({"duplicate_count": None}, "no duplicate to remove"),
    ({"created_by": "sandy.santana"}, "human's shell is a human's to remove"),
    ({"policy_number": None}, "both required"),
    ({"applicant_id": None}, "both required"),
    ({"intent": "cleanup"}, "requires intent="),
])
def test_each_missing_precondition_denies(override, expect_in_reason):
    decision = classify_intent(_good_tx_delete(**override))
    assert not decision.allowed
    assert expect_in_reason in decision.reason, decision.reason


def test_http_transaction_delete_needs_an_observed_signature():
    """Even fully proven, an HTTP-level delete needs a human-approved captured
    signature. Ships empty, so this is denied until Carlo approves one."""
    decision = classify_intent(_good_tx_delete(kind="http", method="POST",
                                               url="https://app.ezlynx.com/x/RemoveTransaction"))
    assert not decision.allowed
    assert "never been observed" in decision.reason


# ---------------------------------------------------------------------------
# CR-2 / CR-7
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("intent", [
    "bind_policy", "issue_policy", "collect_payment", "cancel_policy", "email_insured",
])
def test_forbidden_intents_are_denied(intent):
    decision = classify_intent(ActionIntent(kind="api", intent=intent))
    assert not decision.allowed
    assert decision.rule_id == "CR-2"


def test_kill_switch_blocks_destructive_actions(monkeypatch):
    monkeypatch.setenv("ROBIE_HALT", "1")
    decision = classify_intent(_good_tx_delete())
    assert not decision.allowed
    assert decision.rule_id == "CR-7"


def test_kill_switch_does_not_block_reads(monkeypatch):
    monkeypatch.setenv("ROBIE_HALT", "1")
    decision = classify_intent(ActionIntent(kind="api", intent="get_applicant_policies"))
    assert decision.allowed


def test_broken_config_is_treated_as_empty_allowlist(tmp_path):
    bad = tmp_path / "guard_config.json"
    bad.write_text("{ not json")
    decision = classify_intent(
        _good_tx_delete(kind="http", method="POST", url="https://app.ezlynx.com/x"),
        config_path=bad,
    )
    assert not decision.allowed


def test_unapproved_signature_is_ignored(tmp_path):
    cfg = tmp_path / "guard_config.json"
    cfg.write_text('{"allowed_destructive_requests":['
                   '{"id":"x","method":"POST","url_pattern":"app.ezlynx.com"}]}')
    decision = classify_intent(
        _good_tx_delete(kind="http", method="POST", url="https://app.ezlynx.com/x"),
        config_path=cfg,
    )
    assert not decision.allowed, "a rule without approved_by must not take effect"


def test_approved_signature_permits_the_request(tmp_path):
    cfg = tmp_path / "guard_config.json"
    cfg.write_text('{"allowed_destructive_requests":['
                   '{"id":"tx-del","method":"POST",'
                   '"url_pattern":"app\\\\.ezlynx\\\\.com/.*RemoveTransaction",'
                   '"approved_by":"carlo","approved_at":"2026-09-09"}]}')
    decision = classify_intent(
        _good_tx_delete(kind="http", method="POST",
                        url="https://app.ezlynx.com/x/RemoveTransaction?id=1"),
        config_path=cfg,
    )
    assert decision.allowed, decision.reason


# ---------------------------------------------------------------------------
# Post-condition: the policy must still be there afterwards
# ---------------------------------------------------------------------------

def test_catastrophe_when_policy_disappears_after_deletion():
    states = iter([
        {"policy_exists": True, "transaction_count": 2},
        {"policy_exists": False, "transaction_count": 0},   # what actually happened
    ])
    with pytest.raises(CardinalViolation) as err:
        with guarded_transaction_delete(applicant_id="1", policy_number="P1",
                                        verifier=lambda: next(states)):
            pass
    assert "CATASTROPHE" in str(err.value)


def test_catastrophe_when_more_than_one_row_disappears():
    states = iter([
        {"policy_exists": True, "transaction_count": 3},
        {"policy_exists": True, "transaction_count": 1},
    ])
    with pytest.raises(CardinalViolation):
        with guarded_transaction_delete(applicant_id="1", policy_number="P1",
                                        verifier=lambda: next(states)):
            pass


def test_clean_single_row_removal_passes_postcondition():
    states = iter([
        {"policy_exists": True, "transaction_count": 2},
        {"policy_exists": True, "transaction_count": 1},
    ])
    with guarded_transaction_delete(applicant_id="1", policy_number="P1",
                                    verifier=lambda: next(states)) as g:
        assert g.token


def test_refuses_to_start_when_policy_missing_up_front():
    with pytest.raises(CardinalViolation):
        with guarded_transaction_delete(applicant_id="1", policy_number="P1",
                                        verifier=lambda: {"policy_exists": False}):
            pass


def test_assert_allowed_raises():
    with pytest.raises(CardinalViolation):
        assert_allowed(ActionIntent(kind="click", object_class="policy", target_text="Delete"))
