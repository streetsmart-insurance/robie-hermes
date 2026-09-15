"""Regression tests for the configurable EZLynx write scope.

Covers: the default allowlist, env-var widening, whitespace/empty-entry
normalization, the empty-var fallback, and the fail-closed refusal path.
"""

import importlib
import os
from unittest import mock

import pytest

import robie_job_engine.ezlynx_write_scope as scope


def _fresh_scope(env_value):
    """Reload the module with EZLYNX_WRITE_APPLICANT_IDS set (or unset)."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR, None)
        if env_value is not None:
            os.environ[scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR] = env_value
        return importlib.reload(scope)


@pytest.fixture
def restore_scope():
    yield
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(scope.EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR, None)
        importlib.reload(scope)


def test_default_scope_allows_only_test_account(restore_scope):
    mod = _fresh_scope(None)
    assert mod.applicant_is_write_allowed("220250093") is True
    assert mod.applicant_is_write_allowed("999999999") is False
    with pytest.raises(mod.EzlynxWriteScopeError, match="EZLYNX_WRITE_SCOPE_REFUSED"):
        mod.require_allowed_ezlynx_write_applicant("999999999")


def test_env_var_widens_scope(restore_scope):
    mod = _fresh_scope("220250093,999999999")
    assert mod.applicant_is_write_allowed("220250093") is True
    assert mod.applicant_is_write_allowed("999999999") is True
    assert mod.applicant_is_write_allowed("111111111") is False
    assert mod.require_allowed_ezlynx_write_applicant("999999999") == "999999999"


def test_env_var_normalizes_whitespace_and_drops_empties(restore_scope):
    mod = _fresh_scope("  220250093 ,, 777777777 ,")
    assert mod.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS == frozenset(
        {"220250093", "777777777"}
    )


def test_empty_env_var_falls_back_to_default(restore_scope):
    mod = _fresh_scope("")
    assert mod.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS == frozenset({"220250093"})
    assert mod.applicant_is_write_allowed("999999999") is False


def test_require_returns_normalized_id(restore_scope):
    mod = _fresh_scope("220250093")
    assert mod.require_allowed_ezlynx_write_applicant("  220250093 ") == "220250093"


def test_refusal_names_the_applicant(restore_scope):
    mod = _fresh_scope(None)
    with pytest.raises(mod.EzlynxWriteScopeError) as exc_info:
        mod.require_allowed_ezlynx_write_applicant("424242424")
    assert "424242424" in str(exc_info.value)
