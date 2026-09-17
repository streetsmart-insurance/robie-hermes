"""Regression tests for the configurable EZLynx write scope.

Covers: agency-wide when unset/empty; restricted comma list; 220250093
still allowed; invalid IDs refused; ROBIE_ prefix preferred over the
legacy EZLYNX_ alias.

Does not reload the module: a reload would replace EzlynxWriteScopeError
and break later assertRaises in the same pytest process.
"""

import os
from unittest import mock

import pytest

import robie_job_engine.ezlynx_write_scope as scope


def _apply_allowlist(*, robie=None, legacy=None):
    """Recompute the compiled allowlist from env without reloading."""
    os.environ.pop(scope.ROBIE_EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR, None)
    os.environ.pop(scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR, None)
    if robie is not None:
        os.environ[scope.ROBIE_EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR] = robie
    if legacy is not None:
        os.environ[scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR] = legacy
    scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS = scope._load_allowed_applicant_ids()
    return scope


@pytest.fixture
def restore_scope():
    with mock.patch.dict(os.environ, {}, clear=False):
        yield
    os.environ.pop(scope.ROBIE_EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR, None)
    os.environ.pop(scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR, None)
    scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS = scope._load_allowed_applicant_ids()


def test_unset_allowlist_is_agency_wide(restore_scope):
    mod = _apply_allowlist()
    assert mod.write_allowlist_is_unrestricted() is True
    assert mod.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS is None
    assert mod.applicant_is_write_allowed("220250093") is True
    assert mod.applicant_is_write_allowed("999999999") is True
    assert mod.require_allowed_ezlynx_write_applicant("999999999") == "999999999"


def test_empty_robie_env_is_agency_wide(restore_scope):
    mod = _apply_allowlist(robie="")
    assert mod.write_allowlist_is_unrestricted() is True
    assert mod.applicant_is_write_allowed("220250093") is True
    assert mod.applicant_is_write_allowed("111111111") is True


def test_empty_legacy_env_is_agency_wide(restore_scope):
    mod = _apply_allowlist(legacy="")
    assert mod.write_allowlist_is_unrestricted() is True
    assert mod.applicant_is_write_allowed("220250093") is True


def test_restricted_list_allows_only_named_ids(restore_scope):
    mod = _apply_allowlist(robie="220250093,999999999")
    assert mod.write_allowlist_is_unrestricted() is False
    assert mod.applicant_is_write_allowed("220250093") is True
    assert mod.applicant_is_write_allowed("999999999") is True
    assert mod.applicant_is_write_allowed("111111111") is False
    assert mod.require_allowed_ezlynx_write_applicant("999999999") == "999999999"
    with pytest.raises(mod.EzlynxWriteScopeError, match="EZLYNX_WRITE_SCOPE_REFUSED"):
        mod.require_allowed_ezlynx_write_applicant("111111111")


def test_restricted_list_still_allows_test_account(restore_scope):
    mod = _apply_allowlist(robie="220250093")
    assert mod.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS == frozenset({"220250093"})
    assert mod.require_allowed_ezlynx_write_applicant("  220250093 ") == "220250093"
    assert mod.applicant_is_write_allowed("999999999") is False


def test_env_var_normalizes_whitespace_and_drops_empties(restore_scope):
    mod = _apply_allowlist(robie="  220250093 ,, 777777777 ,")
    assert mod.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS == frozenset(
        {"220250093", "777777777"}
    )


def test_legacy_env_var_still_restricts(restore_scope):
    mod = _apply_allowlist(legacy="220250093")
    assert mod.write_allowlist_is_unrestricted() is False
    assert mod.applicant_is_write_allowed("220250093") is True
    assert mod.applicant_is_write_allowed("999999999") is False


def test_robie_prefix_wins_over_legacy_alias(restore_scope):
    mod = _apply_allowlist(robie="220250093", legacy="999999999")
    assert mod.applicant_is_write_allowed("220250093") is True
    assert mod.applicant_is_write_allowed("999999999") is False


def test_invalid_ids_refused_even_when_unrestricted(restore_scope):
    mod = _apply_allowlist()
    for value in (None, "", "SANITIZED-001", "0", "applicant"):
        assert mod.applicant_is_write_allowed(value) is False
        with pytest.raises(mod.EzlynxWriteScopeError, match="EZLYNX_WRITE_SCOPE_REFUSED"):
            mod.require_allowed_ezlynx_write_applicant(value)


def test_refusal_names_the_applicant(restore_scope):
    mod = _apply_allowlist(robie="220250093")
    with pytest.raises(mod.EzlynxWriteScopeError) as exc_info:
        mod.require_allowed_ezlynx_write_applicant("424242424")
    assert "424242424" in str(exc_info.value)
