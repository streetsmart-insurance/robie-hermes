"""The write gate must fail closed. These tests exist because the charter
documented a --write-notes gate that did not exist in the code for weeks."""

import pytest

from robie_guard import (
    WriteNotAuthorized, assert_write_allowed, disable_writes,
    enable_writes, enable_writes_from_env, writes_enabled,
)


@pytest.fixture(autouse=True)
def _closed_gate():
    disable_writes()
    yield
    disable_writes()


def test_default_is_deny():
    assert not writes_enabled("post_note")
    with pytest.raises(WriteNotAuthorized):
        assert_write_allowed("post_note", applicant_id="149367863")


@pytest.mark.parametrize("intent", [
    "post_note", "add_note_to_discussion", "confirm_change_request",
    "apply_download", "place_call", "send_email",
])
def test_every_write_intent_is_denied_by_default(intent):
    with pytest.raises(WriteNotAuthorized):
        assert_write_allowed(intent)


def test_unknown_intent_denied_even_when_open():
    enable_writes(authorized_by="carlo@streetsmart.insurance", reason="test")
    with pytest.raises(WriteNotAuthorized, match="denied for being unknown"):
        assert_write_allowed("drop_database")


def test_allowed_after_explicit_authorization():
    enable_writes(authorized_by="carlo@streetsmart.insurance", reason="test")
    assert_write_allowed("post_note", applicant_id="149367863")
    assert writes_enabled("post_note")


def test_authorization_requires_a_named_human():
    with pytest.raises(ValueError):
        enable_writes(authorized_by="", reason="test")
    with pytest.raises(ValueError):
        enable_writes(authorized_by="carlo", reason="   ")


def test_scoped_authorization_does_not_leak_to_other_intents():
    enable_writes(authorized_by="carlo", reason="notes only", intents=["post_note"])
    assert_write_allowed("post_note")
    with pytest.raises(WriteNotAuthorized):
        assert_write_allowed("place_call")


def test_env_optin_needs_both_variables(monkeypatch):
    monkeypatch.setenv("ROBIE_ALLOW_WRITES", "1")
    monkeypatch.delenv("ROBIE_WRITE_AUTHORIZED_BY", raising=False)
    assert enable_writes_from_env() is False
    with pytest.raises(WriteNotAuthorized):
        assert_write_allowed("post_note")

    monkeypatch.setenv("ROBIE_WRITE_AUTHORIZED_BY", "carlo@streetsmart.insurance")
    assert enable_writes_from_env() is True
    assert_write_allowed("post_note")


def test_disable_closes_the_gate_again():
    enable_writes(authorized_by="carlo", reason="test")
    assert_write_allowed("post_note")
    disable_writes()
    with pytest.raises(WriteNotAuthorized):
        assert_write_allowed("post_note")
