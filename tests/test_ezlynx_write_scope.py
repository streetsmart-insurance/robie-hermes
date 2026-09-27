"""Regression tests for the configurable EZLynx write scope.

Covers: the default allowlist, env-var widening, whitespace/empty-entry
normalization, the empty-var fallback, the fail-closed refusal path, and
the certificate-sweep applicant-index allowlist (Carlo 2026-09-27: the
index IS the allowlist for the cert sweep — any client in the directory
is a legitimate filing destination).
"""

import importlib
import os
from unittest import mock

import pytest

import robie_job_engine.ezlynx_write_scope as scope
from robie_job_engine.cert_applicant_index import build_index


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


# ---------------------------------------------------------------------------
# Certificate-sweep applicant-index allowlist (Carlo 2026-09-27)
#
# The cert sweep registers the full-book applicant index as its write
# allowlist. Any client in the directory may receive a filing; anyone not
# in the directory is refused. Third-party senders are never checked —
# only the destination applicant. Processes that never register the index
# (policy setup, Chat jobs) keep the restrictive compiled allowlist.
# ---------------------------------------------------------------------------

def _make_cert_index():
    return build_index(
        [
            {"account_name": "Top Notch Tree Service LLC",
             "applicant_id": 199729236,
             "email_primary": "office@topnotchtree.example", "phones": []},
            {"account_name": "Lanali Enterprises LLC",
             "applicant_id": 40280643,
             "email_primary": "info@lanali.example", "phones": []},
        ],
        source_path="test",
    )


def test_cert_sweep_index_allows_indexed_applicant(restore_scope):
    mod = _fresh_scope(None)  # restrictive default: test account only
    assert mod.applicant_is_write_allowed("199729236") is False
    mod.register_cert_sweep_applicant_index(_make_cert_index().all_applicant_ids())
    assert mod.cert_sweep_index_is_registered() is True
    assert mod.applicant_is_write_allowed("199729236") is True
    assert mod.applicant_is_write_allowed("40280643") is True
    assert mod.require_allowed_ezlynx_write_applicant("199729236") == "199729236"


def test_cert_sweep_index_refuses_non_indexed_applicant(restore_scope):
    mod = _fresh_scope(None)
    mod.register_cert_sweep_applicant_index(_make_cert_index().all_applicant_ids())
    assert mod.applicant_is_write_allowed("999999999") is False
    with pytest.raises(mod.EzlynxWriteScopeError,
                       match="EZLYNX_WRITE_SCOPE_REFUSED"):
        mod.require_allowed_ezlynx_write_applicant("999999999")


def test_cert_sweep_index_sender_never_checked(restore_scope):
    """A third-party sender name/email does not affect the allowlist check.

    The allowlist governs the DESTINATION applicant only. Registering an
    index that contains a client must not allow writes keyed by some other
    string (a holder/lender/broker name), and a non-client sender never
    becomes write-allowed through the index.
    """
    mod = _fresh_scope(None)
    mod.register_cert_sweep_applicant_index(_make_cert_index().all_applicant_ids())
    # Holder/lender/broker names are not applicant IDs — never plausible.
    assert mod.applicant_is_write_allowed("Christ Church - Rockaway Campus") is False
    assert mod.applicant_is_write_allowed("") is False
    # A sender email that is not an applicant ID is refused, not allowed.
    assert mod.applicant_is_write_allowed("lender@example.com") is False
    # The indexed destination applicant is allowed regardless of sender.
    assert mod.applicant_is_write_allowed(199729236) is True


def test_policy_setup_keeps_restrictive_allowlist_without_registration(
    restore_scope,
):
    """Processes that never register the cert index keep the default scope.

    Simulates a policy-setup job: no index registration, default env —
    only the test account is write-allowed, and real client IDs are
    refused. Registration is opt-in per process; nothing widens it by
    accident.
    """
    mod = _fresh_scope(None)
    assert mod.cert_sweep_index_is_registered() is False
    assert mod.applicant_is_write_allowed("220250093") is True
    assert mod.applicant_is_write_allowed("199729236") is False
    assert mod.applicant_is_write_allowed("40280643") is False
    with pytest.raises(mod.EzlynxWriteScopeError,
                       match="EZLYNX_WRITE_SCOPE_REFUSED"):
        mod.require_allowed_ezlynx_write_applicant("199729236")


def test_cert_sweep_index_registration_is_additive(restore_scope):
    """The compiled env allowlist still applies alongside the index."""
    mod = _fresh_scope("220250093,777777777")
    mod.register_cert_sweep_applicant_index(_make_cert_index().all_applicant_ids())
    assert mod.applicant_is_write_allowed("777777777") is True
    assert mod.applicant_is_write_allowed("199729236") is True
    assert mod.applicant_is_write_allowed("555555555") is False
