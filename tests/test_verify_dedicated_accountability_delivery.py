from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_dedicated_accountability_delivery.py"
    spec = importlib.util.spec_from_file_location("verify_delivery", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expected_falls_back_to_installed_dwd_credential(tmp_path, monkeypatch):
    module = _load_module()
    credential_path = tmp_path / "data" / "credentials" / "service_account.json"
    credential_path.parent.mkdir(parents=True)
    credential_path.write_text("{}", encoding="utf-8")

    calls = {}
    delegated = types.ModuleType("src.google_auth")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Google delegation credential is unavailable")

    delegated.delegated_credentials = unavailable

    date_utils = types.ModuleType("src.engine.date_utils")
    date_utils.get_previous_business_day = lambda value: value

    config = types.ModuleType("src.production_config")
    config.REPORTING_MAILBOX = "reports@example.test"

    delivery = types.ModuleType("src.reporters.email_delivery")
    delivery.RECIPIENTS = ["one@example.test", "two@example.test"]

    class Credentials:
        @staticmethod
        def from_service_account_file(path, *, scopes, subject):
            calls.update(path=path, scopes=scopes, subject=subject)
            return "credential"

    service_account = types.ModuleType("google.oauth2.service_account")
    service_account.Credentials = Credentials

    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda api, version, *, credentials, cache_discovery: {
        "api": api,
        "version": version,
        "credentials": credentials,
        "cache_discovery": cache_discovery,
    }

    for name in ("src", "src.engine", "src.reporters", "google", "google.oauth2", "googleapiclient"):
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, "src.google_auth", delegated)
    monkeypatch.setitem(sys.modules, "src.engine.date_utils", date_utils)
    monkeypatch.setitem(sys.modules, "src.production_config", config)
    monkeypatch.setitem(sys.modules, "src.reporters.email_delivery", delivery)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", service_account)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)

    _subject, recipients, gmail, _target = module._expected(tmp_path)

    assert recipients == {"one@example.test", "two@example.test"}
    assert calls == {
        "path": str(credential_path),
        "scopes": [module.GMAIL_READONLY_SCOPE],
        "subject": "reports@example.test",
    }
    assert gmail["credentials"] == "credential"
