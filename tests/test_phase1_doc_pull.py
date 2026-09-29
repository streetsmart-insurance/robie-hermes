"""Unit tests for the Phase 1 read-only document-puller.

Everything here is hermetic: fake browsers, fake adapters, fake
credential accessors, fake PolicyApi clients. No test touches a live
portal, a live inbox, or Secret Manager.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine.phase1_adapters import base as adapter_base
from robie_job_engine.phase1_adapters.base import (
    AdapterSpec,
    DownloadResult,
    PolicyRef,
)
from robie_job_engine.phase1_credentials import (
    CredentialConfigurationError,
    load_portal_credentials,
)
from robie_job_engine.phase1_doc_pull import (
    EvidenceError,
    ReadOnlyViolation,
    load_pilot,
    run_pilot,
    verify_download,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PILOT_JSON = REPO_ROOT / "pilot" / "pilot-10.json"


# ---------------------------------------------------------------- fakes


class FakeBrowser:
    """Records calls; download() writes a minimal valid PDF."""

    def __init__(self, pdf_bytes: bytes = b"%PDF-1.4 fake\n"):
        self.calls: list = []
        self.pdf_bytes = pdf_bytes
        self.closed = False

    def goto(self, url): self.calls.append(("goto", url))
    def fill(self, selector, value): self.calls.append(("fill", selector))
    def click(self, selector): self.calls.append(("click", selector))
    def wait_for_selector(self, selector, timeout_ms=30000):
        self.calls.append(("wait", selector))

    def download(self, click_selector, dest_path):
        self.calls.append(("download", click_selector))
        Path(dest_path).write_bytes(self.pdf_bytes)
        return Path(dest_path)

    def close(self): self.closed = True


def _fake_portal_module(carrier_id, ok=True, pdf_bytes=b"%PDF-1.4 fake\n"):
    spec = AdapterSpec(
        carrier_id=carrier_id,
        carrier_name="Fake",
        portal_url="https://example.invalid",
        runtime="box",
        runtime_reason="test",
        username_env="",
        password_env="",
        doc_kind="audit_papers",
    )

    class Mod:
        ADAPTER = spec

        @staticmethod
        def download(policy, dest_dir, browser, accessor=None):
            if not ok:
                return DownloadResult(ok=False, detail="fake login failed")
            dest = Path(dest_dir) / "doc.pdf"
            browser.download("#dl", dest)
            return DownloadResult(ok=True, file_path=dest, detail="fake ok")

    return Mod()


def _fake_api_module(verdict="unknown"):
    spec = AdapterSpec(
        carrier_id="progressive_bor",
        carrier_name="Fake Progressive",
        portal_url="",
        runtime="api",
        runtime_reason="test",
        username_env="",
        password_env="",
        doc_kind="bor_download_check",
        allowed_actions=frozenset({"api_read"}),
    )

    class Mod:
        ADAPTER = spec

        @staticmethod
        def check(policy, dest_dir, ezlynx_client, accessor=None):
            return DownloadResult(
                ok=True,
                detail="fake check",
                extra={"renewal_downloaded": verdict},
            )

    return Mod()


class FakeAccessor:
    def __init__(self, values): self.values = values
    def access(self, ref): return self.values[ref]


def _row(carrier_id="amtrust", **kw):
    base = {
        "policy_number": "P1",
        "insured_name": "Test LLC",
        "carrier_id": carrier_id,
        "report": "4246",
        "doc_kind": "audit_papers",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- verify_download


def test_verify_download_accepts_valid_pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    data = b"%PDF-1.4 hello"
    p.write_bytes(data)
    sha, size = verify_download(p)
    assert sha == hashlib.sha256(data).hexdigest()
    assert size == len(data)


def test_verify_download_rejects_missing(tmp_path):
    with pytest.raises(EvidenceError):
        verify_download(tmp_path / "nope.pdf")


def test_verify_download_rejects_empty(tmp_path):
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(EvidenceError):
        verify_download(p)


def test_verify_download_rejects_non_pdf(tmp_path):
    p = tmp_path / "evil.html"
    p.write_bytes(b"<html>not a pdf</html>")
    with pytest.raises(EvidenceError):
        verify_download(p)


# ---------------------------------------------------------------- read-only gate


def test_read_only_gate_blocks_send_action(tmp_path):
    mod = _fake_portal_module("amtrust")
    object.__setattr__(
        mod.ADAPTER, "allowed_actions", frozenset({"portal_download", "send_email"})
    )
    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": mod},
        browser_factory=lambda runtime: FakeBrowser(),
    )[0]
    assert ev.status == "blocked"
    assert "send_email" in ev.failure_reason


def test_read_only_gate_allows_api_read_only(tmp_path):
    ev = run_pilot(
        [_row("progressive_bor", doc_kind="bor_download_check")],
        run_dir=tmp_path,
        registry={"progressive_bor": _fake_api_module()},
        ezlynx_client=object(),
    )[0]
    assert ev.status == "checked"
    assert ev.extra["renewal_downloaded"] == "unknown"


# ---------------------------------------------------------------- runner dispatch


def test_runner_downloads_and_writes_evidence(tmp_path):
    browser = FakeBrowser()
    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": _fake_portal_module("amtrust")},
        browser_factory=lambda runtime: browser,
    )[0]
    assert ev.status == "downloaded"
    assert ev.bytes > 0 and len(ev.sha256) == 64
    assert ev.file == "docs/doc.pdf"
    assert browser.closed  # runner closes the browser port
    evidence_file = tmp_path / "evidence" / "P1.json"
    assert evidence_file.is_file()
    assert json.loads(evidence_file.read_text())["sha256"] == ev.sha256
    assert (tmp_path / "summary.json").is_file()


def test_runner_records_adapter_failure(tmp_path):
    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": _fake_portal_module("amtrust", ok=False)},
        browser_factory=lambda runtime: FakeBrowser(),
    )[0]
    assert ev.status == "failed"
    assert "fake login failed" in ev.failure_reason
    assert ev.file == "" and ev.sha256 == ""


def test_runner_records_bad_pdf_as_failure(tmp_path):
    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": _fake_portal_module("amtrust", pdf_bytes=b"nope")},
        browser_factory=lambda runtime: FakeBrowser(pdf_bytes=b"nope"),
    )[0]
    assert ev.status == "failed"
    assert "not a PDF" in ev.failure_reason


def test_runner_refuses_without_browser_wiring(tmp_path):
    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": _fake_portal_module("amtrust")},
        browser_factory=None,
    )[0]
    assert ev.status == "failed"
    assert "browser_factory" in ev.failure_reason


# ---------------------------------------------------------------- pilot loading


def test_real_pilot_json_loads_and_has_ten_policies():
    pilot = load_pilot(PILOT_JSON)
    assert len(pilot) == 10
    reports = [p["report"] for p in pilot]
    assert reports.count("4246") == 4
    assert reports.count("4247") == 3
    assert reports.count("4744") == 3
    assert len({p["policy_number"] for p in pilot}) == 10


def test_load_pilot_rejects_missing_field(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"policies": [_row() | {"policy_number": ""}]}))
    with pytest.raises(ValueError, match="missing fields"):
        load_pilot(p)


def test_load_pilot_rejects_unknown_carrier(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"policies": [_row("acme")]}))
    with pytest.raises(ValueError, match="unknown carrier_id"):
        load_pilot(p)


# ---------------------------------------------------------------- credentials


def test_load_portal_credentials_uses_accessor(monkeypatch):
    monkeypatch.setenv("T_U", "projects/p/secrets/u/versions/1")
    monkeypatch.setenv("T_P", "projects/p/secrets/p/versions/1")
    creds = load_portal_credentials(
        "T_U", "T_P", FakeAccessor({"projects/p/secrets/u/versions/1": "user1",
                                    "projects/p/secrets/p/versions/1": "pw1"})
    )
    assert creds.username == "user1" and creds.password == "pw1"
    assert "pw1" not in repr(creds) and "user1" not in repr(creds)


def test_load_portal_credentials_requires_env(monkeypatch):
    monkeypatch.delenv("T_U", raising=False)
    monkeypatch.delenv("T_P", raising=False)
    with pytest.raises(CredentialConfigurationError):
        load_portal_credentials("T_U", "T_P", FakeAccessor({}))


# ---------------------------------------------------------------- real adapter specs (no live hits)


def test_all_real_adapters_are_read_only():
    from robie_job_engine.phase1_adapters import (
        amtrust, asi, njcrib, philadelphia, pie, progressive_bor, safeco, travelers,
    )

    for mod in (amtrust, asi, njcrib, philadelphia, pie, progressive_bor, safeco, travelers):
        spec = mod.ADAPTER
        illegal = set(spec.allowed_actions) - set(adapter_base.READ_ONLY_ACTIONS)
        assert not illegal, f"{spec.carrier_id} declares {illegal}"
        assert spec.runtime in ("box", "sandbox", "api"), spec.carrier_id
        assert spec.runtime_reason, spec.carrier_id
        if spec.runtime == "api":
            assert hasattr(mod, "check")
            assert spec.username_env == ""  # no portal creds for the API path
        else:
            assert hasattr(mod, "download")
            assert spec.username_env and spec.password_env


def test_only_amtrust_claims_proven_box_runtime():
    from robie_job_engine.phase1_adapters import amtrust, njcrib

    assert "sandbox" in amtrust.ADAPTER.runtime_reason  # names the blocked runtime
    assert "UNVERIFIED" in njcrib.ADAPTER.runtime_reason


# ---------------------------------------------------------------- CLI fails closed


def test_cli_refuses_without_browser_wiring(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "run_phase1_pilot.py"),
         "--pilot", str(PILOT_JSON), "--run-dir", str(tmp_path / "run")],
        capture_output=True, text=True, timeout=120,
    )
    # No browser_factory wired: every policy is recorded "failed", and the
    # CLI exits non-zero so a pilot run can never look green by accident.
    assert proc.returncode == 1
    assert '"failed": 10' in proc.stdout
