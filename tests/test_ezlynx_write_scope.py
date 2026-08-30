from __future__ import annotations

import pytest

from robie_job_engine.ezlynx_write_scope import (
    ALLOWED_EZLYNX_WRITE_APPLICANT_IDS,
    EZLYNX_WRITE_SCOPE_REFUSED,
    EzlynxWriteScopeError,
    applicant_id_from_ezlynx_url,
    ezlynx_control_scope_block_reason,
    require_allowed_ezlynx_write_applicant,
)


ALLOWED = "220250093"


def test_compiled_allowlist_contains_only_designated_dummy_applicant():
    assert ALLOWED_EZLYNX_WRITE_APPLICANT_IDS == frozenset({ALLOWED})
    assert require_allowed_ezlynx_write_applicant(f"  {ALLOWED}  ") == ALLOWED


@pytest.mark.parametrize("value", [None, "", "220250094", "SANITIZED-001", "220-250-093"])
def test_every_other_applicant_refuses_without_identifier_rewriting(value):
    with pytest.raises(EzlynxWriteScopeError, match=EZLYNX_WRITE_SCOPE_REFUSED):
        require_allowed_ezlynx_write_applicant(value)


def test_url_scope_requires_job_and_page_to_match_exact_allowed_applicant():
    url = f"https://app.ezlynx.com/web/account/{ALLOWED}/policies"
    assert applicant_id_from_ezlynx_url(url) == ALLOWED
    assert (
        ezlynx_control_scope_block_reason(url, requested_applicant_id=ALLOWED)
        is None
    )
    for requested in ("", "220250094"):
        reason = ezlynx_control_scope_block_reason(
            url, requested_applicant_id=requested
        )
        assert reason and EZLYNX_WRITE_SCOPE_REFUSED in reason


def test_applicant_portal_edit_and_form_entry_urls_are_scoped():
    assert (
        applicant_id_from_ezlynx_url(
            f"https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/{ALLOWED}/83184565"
        )
        == ALLOWED
    )
    assert (
        applicant_id_from_ezlynx_url(
            f"https://app.ezlynx.com/applicantportal/FormEntry/{ALLOWED}"
        )
        == ALLOWED
    )


def test_wrong_or_unscoped_ezlynx_page_refuses_but_other_sites_are_not_reclassified():
    wrong = ezlynx_control_scope_block_reason(
        "https://app.ezlynx.com/web/account/220250094/policies",
        requested_applicant_id=ALLOWED,
    )
    unscoped = ezlynx_control_scope_block_reason(
        "https://app.ezlynx.com/web/policies",
        requested_applicant_id=ALLOWED,
    )
    ascend = ezlynx_control_scope_block_reason(
        "https://app.ascend.com/programs/new",
        requested_applicant_id=ALLOWED,
    )
    assert wrong and "does not match" in wrong
    assert unscoped and "not scoped" in unscoped
    assert ascend is None


def test_login_controls_are_authentication_not_applicant_business_writes():
    assert (
        ezlynx_control_scope_block_reason(
            "https://app.ezlynx.com/login", requested_applicant_id=""
        )
        is None
    )
