import pytest
import os
from src.security.secrets_manager import SecretsManager

def test_secrets_manager_env_override(monkeypatch):
    monkeypatch.setenv("THE_HARTFORD_USERNAME", "agent_rob")
    monkeypatch.setenv("THE_HARTFORD_PASSWORD", "SuperSecureHartfordPass!")

    sm = SecretsManager(provider="auto")
    pair = sm.get_login_pair("The Hartford")

    assert pair["username"] == "agent_rob"
    assert pair["password"] == "SuperSecureHartfordPass!"

def test_secrets_manager_coterie(monkeypatch):
    monkeypatch.setenv("COTERIE_USERNAME", "coterie_user")
    monkeypatch.setenv("COTERIE_PASSWORD", "CoteriePass123")

    sm = SecretsManager(provider="auto")
    assert sm.get_credential("Coterie", "username") == "coterie_user"
    assert sm.get_credential("Coterie", "password") == "CoteriePass123"
