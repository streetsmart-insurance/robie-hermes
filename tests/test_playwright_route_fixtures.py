from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from robie_job_engine.locator_registry import LocatorRegistry
from robie_job_engine.playwright_tracing import PlaywrightTraceManager
from robie_job_engine.retry_policy import FailureClass, classify_failure
from robie_job_engine.snapshot_diff import PageSnapshot, SnapshotDiffManager


ASCEND_MOCK_HTML = """
<!DOCTYPE html>
<html>
<head><title>Ascend Programs</title></head>
<body>
  <h1>Programs Dashboard</h1>
  <div>Programs at risk</div>
  <button role="button">New program</button>
  <form id="create-form">
    <button role="button">Import document</button>
    <label for="producer-input">Producer</label>
    <input id="producer-input" aria-label="Producer" value="Carlo Ferrara" />
    
    <label for="am-input">Account Manager</label>
    <input id="am-input" aria-label="Account Manager" value="Carlo Ferrara" />
    
    <label for="carrier-input">Carrier</label>
    <input id="carrier-input" aria-label="Carrier" value="Travelers" />
    
    <label for="cov-input">Coverage type</label>
    <input id="cov-input" aria-label="Coverage type" value="Commercial Auto" />
    
    <label for="state-select">State</label>
    <select id="state-select" name="state" aria-label="State">
      <option value="CA">California</option>
      <option value="NJ">New Jersey</option>
    </select>
    
    <label for="agency-fee">Agency Fee</label>
    <input id="agency-fee" name="agency_fee" aria-label="Agency Fee" value="0" />
    
    <button role="button">Save program</button>
  </form>
</body>
</html>
"""

EZLYNX_MOCK_HTML = """
<!DOCTYPE html>
<html>
<head><title>EZLynx Document Library</title></head>
<body>
  <div role="textbox" aria-label="Search Labels">Search Labels</div>
  <button role="button">Apply</button>
  <button role="button">Move</button>
</body>
</html>
"""


def test_route_interception_ascend_locators():
    pw_sync = pytest.importorskip("playwright.sync_api")
    sync_playwright = pw_sync.sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Intercept route and serve local mock HTML
        page.route("https://dashboard.useascend.com/programs", lambda route: route.fulfill(
            status=200, content_type="text/html", body=ASCEND_MOCK_HTML
        ))
        page.goto("https://dashboard.useascend.com/programs")

        registry = LocatorRegistry()
        
        # Test resolving locators via registry
        new_prog = registry.resolve_element(page, "ascend", "create_new", "new_program_button")
        assert new_prog.is_visible()

        import_doc = registry.resolve_element(page, "ascend", "create_new", "import_document_button")
        assert import_doc.is_visible()

        producer = registry.resolve_element(page, "ascend", "create_new", "producer")
        assert producer.is_visible()
        assert producer.input_value() == "Carlo Ferrara"

        state_el = registry.resolve_element(page, "ascend", "create_new", "state")
        assert state_el.is_visible()

        browser.close()


def test_route_interception_ezlynx_locators():
    pw_sync = pytest.importorskip("playwright.sync_api")
    sync_playwright = pw_sync.sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.route("https://ezlynx.com/documents", lambda route: route.fulfill(
            status=200, content_type="text/html", body=EZLYNX_MOCK_HTML
        ))
        page.goto("https://ezlynx.com/documents")

        registry = LocatorRegistry()
        apply_btn = registry.resolve_element(page, "ezlynx", "document_library", "apply_button")
        assert apply_btn.is_visible()

        move_btn = registry.resolve_element(page, "ezlynx", "document_library", "move_button")
        assert move_btn.is_visible()

        browser.close()


def test_playwright_tracing_produces_trace_zip(tmp_path: Path):
    pw_sync = pytest.importorskip("playwright.sync_api")
    sync_playwright = pw_sync.sync_playwright

    trace_mgr = PlaywrightTraceManager(artifact_root=tmp_path)
    job_id = "test-trace-job-001"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        with trace_mgr.trace_job(context, job_id) as trace_path:
            page = context.new_page()
            page.route("https://example.com/test", lambda route: route.fulfill(
                status=200, content_type="text/html", body="<h1>Test Page</h1>"
            ))
            page.goto("https://example.com/test")
            assert page.locator("h1").inner_text() == "Test Page"

        browser.close()

    assert trace_path.exists()
    assert trace_path.stat().st_size > 0
    assert trace_path.name == "playwright-trace.zip"


def test_retry_policy_classification():
    # 1. Locator ambiguity -> Non-retryable
    ambig = classify_failure("strict mode violation: locator resolved to 2 elements")
    assert not ambig.is_retryable
    assert ambig.failure_class == FailureClass.LOCATOR_AMBIGUITY

    # 2. Transient network error -> Retryable
    net = classify_failure("Connection reset by peer: ECONNRESET")
    assert net.is_retryable
    assert net.failure_class == FailureClass.TRANSIENT_NETWORK

    # 3. 2SV Auth -> Needs Auth Hold
    auth = classify_failure("Two-step verification 2SV checkpoint triggered")
    assert not auth.is_retryable
    assert auth.failure_class == FailureClass.AUTH_CHALLENGE
    assert auth.suggested_hold_status is not None


def test_snapshot_diff_detection(tmp_path: Path):
    mgr = SnapshotDiffManager(baseline_dir=tmp_path)

    baseline_snap = PageSnapshot(
        portal="ascend",
        page_name="create",
        url="https://dashboard.useascend.com/create/new",
        captured_at="2026-08-28T12:00:00Z",
        dom_tree={"tag": "form"},
        elements_summary=[
            {"role": "button", "name": "Import document"},
            {"role": "textbox", "name": "Producer"},
            {"role": "textbox", "name": "Carrier"},
        ],
    )
    mgr.save_baseline(baseline_snap)

    # Identical current snapshot -> No drift
    same_snap = PageSnapshot(
        portal="ascend",
        page_name="create",
        url="https://dashboard.useascend.com/create/new",
        captured_at="2026-08-28T13:00:00Z",
        dom_tree={"tag": "form"},
        elements_summary=[
            {"role": "button", "name": "Import document"},
            {"role": "textbox", "name": "Producer"},
            {"role": "textbox", "name": "Carrier"},
        ],
    )
    report1 = mgr.diff_snapshot(same_snap)
    assert not report1.is_drifted
    assert len(report1.added_elements) == 0

    # Modified snapshot with added & removed elements -> Drift detected
    modified_snap = PageSnapshot(
        portal="ascend",
        page_name="create",
        url="https://dashboard.useascend.com/create/new",
        captured_at="2026-08-28T14:00:00Z",
        dom_tree={"tag": "form"},
        elements_summary=[
            {"role": "button", "name": "Import document"},
            {"role": "textbox", "name": "Producer"},
            {"role": "button", "name": "New Carrier Combobox V2"},  # Added
        ],
    )
    report2 = mgr.diff_snapshot(modified_snap)
    assert report2.is_drifted
    assert len(report2.added_elements) == 1
    assert len(report2.removed_elements) == 1
