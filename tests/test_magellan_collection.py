from datetime import date
from pathlib import Path

from robie_job_engine.magellan_collection import _seconds


def test_magellan_duration_parsing():
    assert _seconds("0:02:05") == 125
    assert _seconds("1:26:58") == 5218
    assert _seconds("02:03") == 123


def test_magellan_service_uses_separate_profile_and_port():
    unit = (Path(__file__).resolve().parents[1] / "deploy/systemd/robie-magellan-browser.service").read_text()
    assert "--remote-debugging-port=9223" in unit
    assert "browser-profiles/magellan" in unit
    assert "browser-profiles/ezlynx" not in unit
    assert "https://app.magellan.insure/dashboard" in unit


def test_magellan_manifest_is_read_only_and_no_transcripts():
    import json

    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "deploy/accountability/connection-manifest.example.json").read_text()
    )
    config = manifest["collection"]["magellan"]
    assert config["read_only"] is True
    assert config["transcripts_enabled"] is False
    assert config["cdp_url"].endswith(":9223")


def test_magellan_collector_selects_sad_filter_and_never_opens_details():
    source = (
        Path(__file__).resolve().parents[1] / "robie_job_engine/magellan_collection.py"
    ).read_text()
    assert 'get_by_role("radio", name=re.compile(r"\\bSad\\b", re.I))' in source
    assert "View details" not in source
    assert 'get_by_role("button", name="Mark as Handled", exact=True).click' not in source


def test_collector_reads_second_page_with_playwright_keyword_only_argument(tmp_path, monkeypatch):
    import json
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace
    from robie_job_engine import magellan_collection as collector

    class Page:
        current = 0
        waits = 0

        def set_default_timeout(self, value): pass
        def goto(self, *args, **kwargs): pass
        def wait_for_selector(self, selector): pass

        def locator(self, selector):
            if selector == collector.ROW_SELECTOR:
                return SimpleNamespace(first=SimpleNamespace(get_attribute=lambda key: f'call-success-row-{self.current}'))
            assert selector == 'li.ant-pagination-next button, li.ant-pagination-next a'
            return SimpleNamespace(click=lambda: setattr(self, 'current', self.current + 1))

        def wait_for_function(self, expression, *, arg):
            # Match Playwright's keyword-only signature. Old code raises TypeError.
            assert arg == [collector.ROW_SELECTOR, 'call-success-row-0']
            self.waits += 1

    page = Page()
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url: browser))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=lambda: nullcontext(playwright)))
    monkeypatch.setattr(collector, '_authenticate', lambda *args, **kwargs: None)
    monkeypatch.setattr(collector, '_ensure_sad_filter', lambda page: None)
    monkeypatch.setattr(collector, '_next_enabled', lambda page: page.current == 0)
    monkeypatch.setattr(collector, 'parse_visible_rows', lambda page, target: ([{'call_id': str(page.current), 'from_number': '+15555550101'}], False))
    output = collector.collect_magellan_snapshot(target_date=date(2026, 9, 4), output_path=tmp_path / 'snapshot.json')
    snapshot = json.loads(output.read_text())
    assert [row['call_id'] for row in snapshot['records']] == ['0', '1']
    assert snapshot['pages_read'] == 2
    assert page.waits == 1
