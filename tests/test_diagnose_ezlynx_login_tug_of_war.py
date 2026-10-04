from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "diagnose_ezlynx_login_tug_of_war.py"
spec = importlib.util.spec_from_file_location("login_forensics", MODULE)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_route_class_never_returns_customer_url():
    assert mod.route_class("https://app.ezlynx.com/auth/account/login") == "ezlynx-login"
    assert (
        mod.route_class("https://app.ezlynx.com/web/account/26356199/policies")
        == "ezlynx-authenticated-web"
    )


def test_sanitize_redacts_network_and_long_identifiers():
    text = mod.sanitize(
        "Accepted publickey from 10.1.2.3 applicant=26356199 password=hunter2"
    )
    assert "10.1.2.3" not in text
    assert "26356199" not in text
    assert "hunter2" not in text


def test_workflow_is_read_only_and_fixed_to_both_hosts():
    workflow = (ROOT / ".github/workflows/diagnose-ezlynx-login-tug-of-war.yml").read_text()
    assert "hermes-poc-01" in workflow
    assert "hermes-test-01" in workflow
    assert "DIAGNOSE_EZLYNX_LOGIN_TUG_OF_WAR" in workflow
    assert "systemctl restart" not in workflow
    assert "ezlynx_login_bootstrap.py" not in workflow
    assert "gcloud secrets versions access" not in workflow
