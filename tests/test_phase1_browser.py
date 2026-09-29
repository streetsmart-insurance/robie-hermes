"""Unit tests for the Phase 1 browser port and reliability hardening.

Everything here is hermetic: the Playwright page surface is faked, the
``playwright`` package itself is stubbed via sys.modules, Secret Manager
is stubbed, and sleeps are injected. No test touches a live portal, a
live inbox, or Secret Manager.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine.phase1_adapters.base import (
    AdapterSpec,
    DownloadResult,
    SessionExpiredError,
    TransientBrowserError,
)
from robie_job_engine.phase1_browser import (
    BOX_PROXY,
    CarrierSession,
    PlaywrightBrowserPort,
    default_browser_factory,
    is_transient,
    new_browser,
    run_with_retries,
)
from robie_job_engine.phase1_credentials import credential_preflight
from robie_job_engine.phase1_doc_pull import (
    pdf_contains_policy_number,
    run_pilot,
)


# ---------------------------------------------------------------- helpers


def _make_pdf_with_text(text: str) -> bytes:
    """Build a minimal but VALID single-page PDF containing ``text``."""
    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(pdf)


class FakePage:
    """Records calls; failures can be injected per method."""

    def __init__(self):
        self.calls: list = []
        self.url = "https://example.invalid/start"
        self.failures: dict = {}
        self.download_bytes = _make_pdf_with_text("Policy Number: PWC1303622")

    def _maybe_fail(self, method):
        if method in self.failures:
            raise self.failures[method]

    def goto(self, url, wait_until=None, timeout=None):
        self.calls.append(("goto", url))
        self._maybe_fail("goto")
        self.url = url

    def fill(self, selector, value, timeout=None):
        self.calls.append(("fill", selector))
        self._maybe_fail("fill")

    def click(self, selector, timeout=None):
        self.calls.append(("click", selector))
        self._maybe_fail("click")

    def wait_for_selector(self, selector, state=None, timeout=None):
        self.calls.append(("wait", selector))
        self._maybe_fail("wait")

    def screenshot(self, path=None):
        self.calls.append(("screenshot", str(path)))
        Path(path).write_bytes(b"PNG-fake")

    def expect_download(self, timeout=None):
        page = self

        class _Ctx:
            def __enter__(self_inner):
                page.calls.append(("expect_download",))
                page._maybe_fail("expect_download")
                return self_inner

            def __exit__(self_inner, *a):
                return False

            @property
            def value(self_inner):
                class _Dl:
                    def save_as(self_inner2, dest):
                        page.calls.append(("save_as", str(dest)))
                        Path(dest).write_bytes(page.download_bytes)

                return _Dl()

        return _Ctx()


class FakeSessionPort:
    """Port double for runner tests: log, screenshot, close tracking."""

    instances: list = []

    def __init__(self):
        self.closed = False
        self._log: list = []
        FakeSessionPort.instances.append(self)

    def reset_log(self):
        self._log = []

    @property
    def action_log(self):
        return list(self._log)

    def note(self, action, detail=""):
        self._log.append({"t": "2026-01-01T00:00:00+00:00", "action": action,
                          "detail": detail})

    def screenshot(self, dest_path):
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PNG-fake")
        return p

    def close(self):
        self.closed = True


def _portal_spec(carrier_id="amtrust"):
    return AdapterSpec(
        carrier_id=carrier_id,
        carrier_name="Fake",
        portal_url="https://example.invalid",
        runtime="box",
        runtime_reason="test",
        username_env="",
        password_env="",
        doc_kind="audit_papers",
    )


def _ok_module(pdf_bytes=None, carrier_id="amtrust"):
    spec = _portal_spec(carrier_id)

    class Mod:
        ADAPTER = spec
        downloads = 0

        @staticmethod
        def download(policy, dest_dir, browser, accessor=None):
            Mod.downloads += 1
            dest = Path(dest_dir) / "doc.pdf"
            dest.write_bytes(pdf_bytes if pdf_bytes is not None
                             else _make_pdf_with_text(f"Policy Number: {policy.policy_number}"))
            return DownloadResult(ok=True, file_path=dest, detail="fake ok")

    return Mod()


def _raising_module(exc_factory, carrier_id="amtrust"):
    spec = _portal_spec(carrier_id)

    class Mod:
        ADAPTER = spec

        @staticmethod
        def download(policy, dest_dir, browser, accessor=None):
            raise exc_factory()

    return Mod()


def _flaky_module(failures, carrier_id="amtrust"):
    """Fails ``failures`` times with TransientBrowserError, then succeeds."""
    spec = _portal_spec(carrier_id)

    class Mod:
        ADAPTER = spec
        calls = 0

        @staticmethod
        def download(policy, dest_dir, browser, accessor=None):
            Mod.calls += 1
            if Mod.calls <= failures:
                raise TransientBrowserError("simulated navigation timeout")
            dest = Path(dest_dir) / "doc.pdf"
            dest.write_bytes(_make_pdf_with_text(f"Policy Number: {policy.policy_number}"))
            return DownloadResult(ok=True, file_path=dest, detail="fake ok")

    return Mod()


def _row(carrier_id="amtrust", **kw):
    base = {
        "policy_number": "PWC1303622",
        "insured_name": "Test LLC",
        "carrier_id": carrier_id,
        "report": "4246",
        "doc_kind": "audit_papers",
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _reset_fake_port_instances():
    FakeSessionPort.instances.clear()
    yield
    FakeSessionPort.instances.clear()


# ---------------------------------------------------------------- new_browser


def _stub_playwright(monkeypatch):
    """Stub the playwright.sync_api module; returns launch recorder."""
    launched = {}

    class FakeBrowser:
        def new_context(self, accept_downloads=None):
            launched["accept_downloads"] = accept_downloads

            class FakeContext:
                def new_page(self):
                    return FakePage()

                def close(self):
                    pass

            return FakeContext()

        def close(self):
            pass

    class FakeChromium:
        def launch(self, **kwargs):
            launched.update(kwargs)
            return FakeBrowser()

    stub = types.ModuleType("playwright.sync_api")

    class _PW:
        chromium = FakeChromium()

        def start(self):
            return self

        def stop(self):
            pass

    stub.sync_playwright = lambda: _PW()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", stub)
    return launched


def test_new_browser_rejects_unknown_runtime():
    with pytest.raises(ValueError, match="unknown browser runtime"):
        new_browser("mars")


def test_new_browser_box_uses_proxy_and_antidetection(monkeypatch):
    launched = _stub_playwright(monkeypatch)
    port = new_browser("box")
    assert launched["proxy"] == {"server": BOX_PROXY}
    assert "--disable-blink-features=AutomationControlled" in launched["args"]
    assert launched["accept_downloads"] is True
    port.close()


def test_new_browser_sandbox_has_no_proxy(monkeypatch):
    launched = _stub_playwright(monkeypatch)
    port = new_browser("sandbox")
    assert "proxy" not in launched
    assert "--disable-blink-features=AutomationControlled" in launched["args"]
    port.close()


def test_default_browser_factory_rejects_bad_runtime():
    factory = default_browser_factory()
    with pytest.raises(ValueError, match="unknown browser runtime"):
        factory("bogus")


# ---------------------------------------------------------------- the port


def test_port_wraps_navigation_timeout_as_transient():
    page = FakePage()
    page.failures["goto"] = TimeoutError("Navigation timed out")
    port = PlaywrightBrowserPort(runtime="box", page=page)
    with pytest.raises(TransientBrowserError):
        port.goto("https://example.invalid/login")


def test_fill_never_logs_credential_values():
    page = FakePage()
    port = PlaywrightBrowserPort(runtime="box", page=page)
    port.fill("#username", "agent007")
    port.fill("#password", "s3cr3t-hunter2")
    blob = json.dumps(port.action_log)
    assert "#username" in blob and "#password" in blob
    assert "agent007" not in blob and "s3cr3t-hunter2" not in blob
    assert all("t" in entry and "action" in entry for entry in port.action_log)


def test_download_captures_file(tmp_path):
    page = FakePage()
    port = PlaywrightBrowserPort(runtime="box", page=page)
    dest = tmp_path / "doc.pdf"
    out = port.download("#download", dest)
    assert out == dest and dest.is_file() and dest.stat().st_size > 0


def test_download_with_no_file_raises_transient(tmp_path):
    page = FakePage()
    page.download_bytes = b""  # save_as writes nothing
    port = PlaywrightBrowserPort(runtime="box", page=page)
    with pytest.raises(TransientBrowserError, match="no file"):
        port.download("#download", tmp_path / "doc.pdf")


# ---------------------------------------------------------------- retry policy


def test_run_with_retries_succeeds_after_transients():
    calls = []
    delays = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise TransientBrowserError("timeout")
        return "ok"

    result = run_with_retries(fn, sleep=delays.append)
    assert result == "ok"
    assert len(calls) == 3
    assert delays == [2.0, 4.0]  # exponential backoff


def test_run_with_retries_raises_permanent_immediately():
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("bad credentials")

    with pytest.raises(ValueError, match="bad credentials"):
        run_with_retries(fn, sleep=lambda s: calls.append(("sleep", s)))
    assert calls == [1]


def test_run_with_retries_does_not_retry_session_expiry():
    calls = []

    def fn():
        calls.append(1)
        raise SessionExpiredError("login screen reappeared")

    with pytest.raises(SessionExpiredError):
        run_with_retries(fn, sleep=lambda s: None)
    assert calls == [1]


def test_run_with_retries_gives_up_after_attempts():
    calls = []

    def fn():
        calls.append(1)
        raise TransientBrowserError("still timing out")

    with pytest.raises(TransientBrowserError):
        run_with_retries(fn, attempts=3, sleep=lambda s: None)
    assert len(calls) == 3


def test_is_transient_classification():
    assert is_transient(TimeoutError("x"))
    assert is_transient(TransientBrowserError("x"))
    assert is_transient(RuntimeError("net::ERR_CONNECTION_CLOSED"))
    assert not is_transient(SessionExpiredError("x"))
    assert not is_transient(ValueError("invalid login"))
    assert not is_transient(RuntimeError("document not found"))


# ---------------------------------------------------------------- CarrierSession


def test_carrier_session_reuses_port():
    created = []

    def factory(runtime):
        created.append(runtime)
        return FakeSessionPort()

    session = CarrierSession("amtrust", "box", factory)
    assert session.port() is session.port()
    assert created == ["box"]
    session.close()
    assert FakeSessionPort.instances[0].closed


def test_carrier_session_reset_builds_fresh():
    created = []

    def factory(runtime):
        created.append(runtime)
        return FakeSessionPort()

    session = CarrierSession("amtrust", "box", factory)
    first = session.port()
    second = session.reset()
    assert second is not first
    assert first.closed and not second.closed
    assert created == ["box", "box"]
    session.close()


# ---------------------------------------------------------------- runner: session + retry


def test_runner_relogin_exactly_once_on_session_expiry(tmp_path):
    created = []

    def factory(runtime):
        created.append(runtime)
        return FakeSessionPort()

    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": _raising_module(
            lambda: SessionExpiredError("portal session expired"))},
        browser_factory=factory,
        sleep=lambda s: None,
    )[0]
    assert created == ["box", "box"]  # initial + exactly one re-login
    assert ev.status == "failed"
    assert "expired" in ev.failure_reason
    assert all(p.closed for p in FakeSessionPort.instances)


def test_runner_retries_transient_download(tmp_path):
    created = []

    def factory(runtime):
        created.append(runtime)
        return FakeSessionPort()

    mod = _flaky_module(failures=2)
    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": mod},
        browser_factory=factory,
        sleep=lambda s: None,
    )[0]
    assert mod.calls == 3
    assert created == ["box"]  # same session reused, no re-login
    assert ev.status == "downloaded"


def test_runner_reuses_one_session_per_carrier(tmp_path):
    created = []

    def factory(runtime):
        created.append(runtime)
        return FakeSessionPort()

    rows = [_row(policy_number="P1"), _row(policy_number="P2")]
    out = run_pilot(
        rows,
        run_dir=tmp_path,
        registry={"amtrust": _ok_module()},
        browser_factory=factory,
        sleep=lambda s: None,
    )
    assert created == ["box"]  # signed in once, context reused
    assert [e.status for e in out] == ["downloaded", "downloaded"]


# ---------------------------------------------------------------- runner: idempotent skip


def test_runner_skips_policy_with_valid_prior_evidence(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    (docs / "PWC1303622.pdf").write_bytes(b"%PDF-1.4 already-have-it\n")
    ev_dir = tmp_path / "evidence"
    ev_dir.mkdir(parents=True)
    (ev_dir / "PWC1303622.json").write_text(json.dumps({
        "policy_number": "PWC1303622", "insured_name": "Test LLC",
        "carrier_id": "amtrust", "carrier_name": "Fake", "report": "4246",
        "doc_kind": "audit_papers", "runtime": "box", "status": "downloaded",
        "file": "docs/PWC1303622.pdf", "sha256": "abc", "bytes": 25,
        "downloaded_at": "2026-01-01T00:00:00+00:00", "screenshot": "",
        "detail": "earlier", "failure_reason": "", "extra": {},
    }))
    created = []

    def factory(runtime):
        created.append(runtime)
        return FakeSessionPort()

    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": _ok_module()},
        browser_factory=factory,
        sleep=lambda s: None,
    )[0]
    assert ev.status == "skipped"
    assert created == []  # no browser launched at all
    assert "already completed" in ev.detail


# ---------------------------------------------------------------- PDF content check


def test_pdf_content_check_finds_policy_number(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(_make_pdf_with_text("Policy Number: PWC1303622"))
    assert pdf_contains_policy_number(p, "PWC1303622") is True
    # punctuation/whitespace/case-insensitive
    assert pdf_contains_policy_number(p, "pwc 1303622") is True


def test_pdf_content_check_rejects_wrong_document(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(_make_pdf_with_text("Generic marketing brochure"))
    assert pdf_contains_policy_number(p, "PWC1303622") is False


def test_pdf_content_check_inconclusive_on_garbage(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake\n")
    assert pdf_contains_policy_number(p, "PWC1303622") is None


def test_runner_marks_needs_review_for_wrong_document(tmp_path):
    wrong_pdf = _make_pdf_with_text("Some other policy's dec page")

    def factory(runtime):
        return FakeSessionPort()

    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": _ok_module(pdf_bytes=wrong_pdf)},
        browser_factory=factory,
        sleep=lambda s: None,
    )[0]
    assert ev.status == "needs_review"
    assert ev.file != "" and len(ev.sha256) == 64  # file KEPT as evidence
    assert "PWC1303622" in ev.failure_reason
    assert ev.extra["content_check"] == "policy number NOT found in PDF text"


# ---------------------------------------------------------------- credential preflight


def test_credential_preflight_lists_missing_env(monkeypatch):
    from robie_job_engine.phase1_adapters import amtrust

    monkeypatch.delenv("PHASE1_AMTRUST_USERNAME_SECRET", raising=False)
    monkeypatch.delenv("PHASE1_AMTRUST_PASSWORD_SECRET", raising=False)
    missing = credential_preflight(
        [_row("amtrust")], {"amtrust": amtrust}, secret_exists=lambda ref: True
    )
    assert any("PHASE1_AMTRUST_USERNAME_SECRET" in m for m in missing)
    assert any("PHASE1_AMTRUST_PASSWORD_SECRET" in m for m in missing)


def test_credential_preflight_detects_missing_secret(monkeypatch):
    from robie_job_engine.phase1_adapters import amtrust

    monkeypatch.setenv("PHASE1_AMTRUST_USERNAME_SECRET",
                       "projects/p/secrets/u/versions/1")
    monkeypatch.setenv("PHASE1_AMTRUST_PASSWORD_SECRET",
                       "projects/p/secrets/p/versions/1")
    missing = credential_preflight(
        [_row("amtrust")], {"amtrust": amtrust}, secret_exists=lambda ref: False
    )
    assert len(missing) == 2
    assert all("not found in Secret Manager" in m for m in missing)


def test_credential_preflight_passes_when_refs_exist(monkeypatch):
    from robie_job_engine.phase1_adapters import amtrust

    monkeypatch.setenv("PHASE1_AMTRUST_USERNAME_SECRET",
                       "projects/p/secrets/u/versions/1")
    monkeypatch.setenv("PHASE1_AMTRUST_PASSWORD_SECRET",
                       "projects/p/secrets/p/versions/1")
    assert credential_preflight(
        [_row("amtrust")], {"amtrust": amtrust}, secret_exists=lambda ref: True
    ) == []


def test_runner_preflight_fails_closed_without_browser_work(tmp_path, monkeypatch):
    from robie_job_engine.phase1_adapters import amtrust

    monkeypatch.setenv("PHASE1_AMTRUST_USERNAME_SECRET",
                       "projects/p/secrets/u/versions/1")
    monkeypatch.setenv("PHASE1_AMTRUST_PASSWORD_SECRET",
                       "projects/p/secrets/p/versions/1")
    created = []

    def factory(runtime):
        created.append(runtime)
        return FakeSessionPort()

    ev = run_pilot(
        [_row("amtrust")],
        run_dir=tmp_path,
        registry={"amtrust": amtrust},
        browser_factory=factory,
        secret_exists=lambda ref: False,  # refs do not exist
        sleep=lambda s: None,
    )[0]
    assert ev.status == "failed"
    assert "preflight" in ev.failure_reason
    assert created == []  # no browser launched, no adapter executed


# ---------------------------------------------------------------- failure artifacts


def test_screenshot_saved_on_failure(tmp_path):
    def factory(runtime):
        return FakeSessionPort()

    class FailMod:
        ADAPTER = _portal_spec()

        @staticmethod
        def download(policy, dest_dir, browser, accessor=None):
            return DownloadResult(ok=False, detail="fake login failed")

    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": FailMod()},
        browser_factory=factory,
        sleep=lambda s: None,
    )[0]
    assert ev.status == "failed"
    assert ev.screenshot == "screenshots/PWC1303622.png"
    assert (tmp_path / ev.screenshot).is_file()


def test_action_log_attached_to_evidence(tmp_path):
    class LoggingPort(FakeSessionPort):
        def download(self, click_selector, dest_path):
            self.note("goto", "https://example.invalid")
            self.note("click", "#download")

    logged = {}

    class LogMod:
        ADAPTER = _portal_spec()

        @staticmethod
        def download(policy, dest_dir, browser, accessor=None):
            browser.download("#download", Path(dest_dir) / "doc.pdf")
            dest = Path(dest_dir) / "doc.pdf"
            dest.write_bytes(_make_pdf_with_text(f"Policy Number: {policy.policy_number}"))
            return DownloadResult(ok=True, file_path=dest, detail="fake ok")

    def factory(runtime):
        port = LoggingPort()
        logged["port"] = port
        return port

    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": LogMod()},
        browser_factory=factory,
        sleep=lambda s: None,
    )[0]
    assert ev.status == "downloaded"
    actions = ev.extra["action_log"]
    assert [a["action"] for a in actions] == ["goto", "click"]
    assert all("t" in a for a in actions)


# ---------------------------------------------------------------- FIX 1: session-expiry probe


class ProbingPort(FakeSessionPort):
    """FakeSessionPort with a controllable has_selector probe."""

    def __init__(self, probe_results=()):
        super().__init__()
        self._probe_results = list(probe_results)
        self.probe_calls = []

    def has_selector(self, selector, timeout_ms=5000):
        self.probe_calls.append(selector)
        self.note("probe", selector)
        if self._probe_results:
            return self._probe_results.pop(0)
        return False


def _probed_ok_module(probe_indicator="#logout", carrier_id="amtrust"):
    spec = AdapterSpec(
        carrier_id=carrier_id,
        carrier_name="Fake",
        portal_url="https://example.invalid",
        runtime="box",
        runtime_reason="test",
        username_env="",
        password_env="",
        doc_kind="audit_papers",
        logged_in_indicator=probe_indicator,
    )

    class Mod:
        ADAPTER = spec

        @staticmethod
        def download(policy, dest_dir, browser, accessor=None):
            dest = Path(dest_dir) / "doc.pdf"
            dest.write_bytes(_make_pdf_with_text(
                f"Policy Number: {policy.policy_number}"))
            return DownloadResult(ok=True, file_path=dest, detail="fake ok")

    return Mod()


def test_probe_unavailable_adapter_keeps_old_behavior(tmp_path):
    """No indicator declared: old behavior + honest 'unavailable' flag."""
    created = []

    def factory(runtime):
        created.append(runtime)
        return FakeSessionPort()

    rows = [_row(policy_number="P1"), _row(policy_number="P2")]
    out = run_pilot(
        rows,
        run_dir=tmp_path,
        registry={"amtrust": _ok_module()},
        browser_factory=factory,
        sleep=lambda s: None,
    )
    assert [e.status for e in out] == ["downloaded", "downloaded"]
    # P1: fresh session, probe deferred; P2: reused session, no indicator
    # declared -> honest "unavailable", old behavior otherwise.
    assert out[0].extra["session_probe"].startswith("deferred")
    assert out[1].extra["session_probe"] == "unavailable"
    assert created == ["box"]  # one session, no probe-driven re-login


def test_probe_deferred_on_fresh_session(tmp_path):
    """Indicator declared but session brand new: probe deferred, no raise."""
    created = []

    def factory(runtime):
        created.append(runtime)
        return ProbingPort()

    ev = run_pilot(
        [_row()],
        run_dir=tmp_path,
        registry={"amtrust": _probed_ok_module()},
        browser_factory=factory,
        sleep=lambda s: None,
    )[0]
    assert ev.status == "downloaded"
    assert ev.extra["session_probe"].startswith("deferred: fresh session")
    assert created == ["box"]
    assert FakeSessionPort.instances[0].probe_calls == []


def test_probe_checked_when_indicator_present(tmp_path):
    """Reused session, indicator present: 'checked', no re-login."""
    created = []

    def factory(runtime):
        created.append(runtime)
        return ProbingPort(probe_results=[True])

    rows = [_row(policy_number="P1"), _row(policy_number="P2")]
    out = run_pilot(
        rows,
        run_dir=tmp_path,
        registry={"amtrust": _probed_ok_module()},
        browser_factory=factory,
        sleep=lambda s: None,
    )
    assert [e.status for e in out] == ["downloaded", "downloaded"]
    assert out[0].extra["session_probe"].startswith("deferred")
    assert out[1].extra["session_probe"] == "checked"
    assert created == ["box"]  # same session reused, no re-login
    assert FakeSessionPort.instances[0].probe_calls == ["#logout"]


def test_probe_detects_logged_out_triggers_single_relogin(tmp_path):
    """Absent indicator on a reused session: exactly one re-login, then OK."""
    ports = []

    def factory(runtime):
        port = ProbingPort(probe_results=[False])  # reused session looks logged out
        ports.append(port)
        return port

    rows = [_row(policy_number="P1"), _row(policy_number="P2")]
    out = run_pilot(
        rows,
        run_dir=tmp_path,
        registry={"amtrust": _probed_ok_module()},
        browser_factory=factory,
        sleep=lambda s: None,
    )
    assert [e.status for e in out] == ["downloaded", "downloaded"]
    assert len(ports) == 2  # initial + exactly one re-login
    # the probe ran on the reused session (found it logged out) and was
    # skipped on the fresh post-reset session; both close at run end.
    assert ports[0].probe_calls == ["#logout"]
    assert ports[1].probe_calls == []
    assert all(p.closed for p in ports)
    assert out[1].extra["session_probe"] == "expired: re-login engaged"


# ---------------------------------------------------------------- FIX 2: login-stall handling


def test_login_stall_records_blocked_with_mfa_reason(tmp_path):
    """A stalled login is blocked (not retried, not hung); next policy runs."""
    from robie_job_engine.phase1_adapters.base import (
        LOGIN_STALL_REASON,
        LoginStalledError,
    )

    class StallThenOk:
        ADAPTER = _portal_spec()
        calls = 0

        @staticmethod
        def download(policy, dest_dir, browser, accessor=None):
            StallThenOk.calls += 1
            browser.note("goto", "https://example.invalid/login")
            if StallThenOk.calls == 1:
                raise LoginStalledError(LOGIN_STALL_REASON)
            dest = Path(dest_dir) / "doc.pdf"
            dest.write_bytes(_make_pdf_with_text(
                f"Policy Number: {policy.policy_number}"))
            return DownloadResult(ok=True, file_path=dest, detail="fake ok")

    def factory(runtime):
        return FakeSessionPort()

    rows = [_row(policy_number="P1"), _row(policy_number="P2")]
    out = run_pilot(
        rows,
        run_dir=tmp_path,
        registry={"amtrust": StallThenOk()},
        browser_factory=factory,
        sleep=lambda s: None,
    )
    assert StallThenOk.calls == 2  # stalled once (no retry), then next policy ran
    blocked, second = out
    assert blocked.status == "blocked"
    assert blocked.failure_reason == LOGIN_STALL_REASON  # exact, distinct string
    assert "possible MFA" in blocked.failure_reason
    assert blocked.screenshot == "screenshots/P1.png"
    assert (tmp_path / blocked.screenshot).is_file()
    assert [a["action"] for a in blocked.extra["action_log"]] == ["goto"]
    assert second.status == "downloaded"  # run moved to the next policy


def test_login_stall_reason_distinct_from_selector_failure(tmp_path):
    """The stall reason must not look like an ordinary selector failure."""
    from robie_job_engine.phase1_adapters.base import LOGIN_STALL_REASON

    assert "possible MFA" in LOGIN_STALL_REASON
    assert "timed out" not in LOGIN_STALL_REASON.lower()
    assert "selector" not in LOGIN_STALL_REASON.lower()


def test_adapter_login_converts_timeout_to_stall(tmp_path, monkeypatch):
    """Real adapter login(): a login-page timeout becomes LoginStalledError."""
    from robie_job_engine.phase1_adapters import amtrust
    from robie_job_engine.phase1_adapters.base import (
        LOGIN_STALL_REASON,
        LoginStalledError,
    )

    monkeypatch.setenv("PHASE1_AMTRUST_USERNAME_SECRET",
                       "projects/p/secrets/u/versions/1")
    monkeypatch.setenv("PHASE1_AMTRUST_PASSWORD_SECRET",
                       "projects/p/secrets/p/versions/1")

    class FakeAccessor:
        def access(self, ref):
            # values chosen to not collide with any selector substring
            return "zz-user-007" if ref.endswith("/u/versions/1") else "zz-pass-008"

    page = FakePage()
    page.failures["wait"] = TimeoutError("waiting for #policy-search timed out")
    port = PlaywrightBrowserPort(runtime="box", page=page)
    with pytest.raises(LoginStalledError) as exc_info:
        amtrust.login(port, accessor=FakeAccessor())
    assert str(exc_info.value) == LOGIN_STALL_REASON
    # no credential values in the action log even on the stall path
    blob = json.dumps(port.action_log)
    assert "zz-user-007" not in blob and "zz-pass-008" not in blob


def test_adapter_login_succeeds_when_indicator_present(tmp_path, monkeypatch):
    """Real adapter login(): clean login passes, including the indicator."""
    from robie_job_engine.phase1_adapters import amtrust

    monkeypatch.setenv("PHASE1_AMTRUST_USERNAME_SECRET",
                       "projects/p/secrets/u/versions/1")
    monkeypatch.setenv("PHASE1_AMTRUST_PASSWORD_SECRET",
                       "projects/p/secrets/p/versions/1")

    class FakeAccessor:
        def access(self, ref):
            return "user" if ref.endswith("/u/versions/1") else "pass"

    page = FakePage()
    port = PlaywrightBrowserPort(runtime="box", page=page)
    amtrust.login(port, accessor=FakeAccessor())  # must not raise
    waited = [c[1] for c in page.calls if c[0] == "wait"]
    assert "#policy-search" in waited
    assert "a[href*='logout']" in waited  # indicator asserted after login
