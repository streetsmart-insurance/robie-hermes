from datetime import date
from pathlib import Path

from robie_job_engine.magellan_collection import _mask_phone, _seconds, _verify_newest_first


def test_magellan_duration_parsing():
    assert _seconds("0:02:05") == 125
    assert _seconds("1:26:58") == 5218
    assert _seconds("02:03") == 123


def test_magellan_phone_masking_never_labels_a_raw_number_as_masked():
    assert _mask_phone("+1 (980) 721-5193") == "***-***-5193"
    assert _mask_phone("12") == "unavailable"


def test_magellan_order_check_accepts_newest_first_and_rejects_unsorted():
    from datetime import datetime
    import pytest

    newest = datetime(2026, 9, 4, 12, 0)
    older = datetime(2026, 9, 4, 11, 0)
    assert _verify_newest_first([newest, older], previous_page_oldest=None) == older
    with pytest.raises(RuntimeError, match="newest to oldest"):
        _verify_newest_first([older, newest], previous_page_oldest=None)
    with pytest.raises(RuntimeError, match="newest to oldest"):
        _verify_newest_first([newest], previous_page_oldest=older)


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
            assert arg == [collector.ROW_SELECTOR, 'call-success-row-0']
            self.waits += 1

    page = Page()
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url: browser))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=lambda: nullcontext(playwright)))
    monkeypatch.setattr(collector, '_authenticate', lambda *args, **kwargs: None)
    monkeypatch.setattr(collector, '_ensure_sad_filter', lambda page: None)
    monkeypatch.setattr(collector, '_wait_for_sad_results', lambda page: True)
    monkeypatch.setattr(
        collector,
        '_visible_row_datetimes',
        lambda page: [__import__('datetime').datetime(2026, 9, 4, 12 - page.current, 0)],
    )
    monkeypatch.setattr(collector, '_next_enabled', lambda page: page.current == 0)
    monkeypatch.setattr(
        collector,
        'parse_visible_rows',
        lambda page, target: ([{
            'call_id': str(page.current),
            'occurred_at': f'2026-09-04T{12 - page.current:02d}:00:00',
            'from_number': '+15555550101',
            'to_number': '+15555550199',
        }], False),
    )
    output = collector.collect_magellan_snapshot(target_date=date(2026, 9, 4), output_path=tmp_path / 'snapshot.json')
    snapshot = json.loads(output.read_text())
    assert [row['call_id'] for row in snapshot['records']] == ['0', '1']
    assert snapshot['pages_read'] == 2
    assert page.waits == 1
    assert snapshot['sad_calls'][0]['caller_phone_masked'] == '***-***-0101'
    assert 'from_number' not in snapshot['sad_calls'][0]
    assert 'to_number' not in snapshot['sad_calls'][0]


def test_collector_accepts_verified_empty_sad_results(tmp_path, monkeypatch):
    import json
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace
    from robie_job_engine import magellan_collection as collector

    class Page:
        def set_default_timeout(self, value): pass
        def goto(self, *args, **kwargs): pass

    page = Page()
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url: browser))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=lambda: nullcontext(playwright)))
    monkeypatch.setattr(collector, '_authenticate', lambda *args, **kwargs: None)
    monkeypatch.setattr(collector, '_ensure_sad_filter', lambda page: None)
    monkeypatch.setattr(collector, '_wait_for_sad_results', lambda page: False)

    output = collector.collect_magellan_snapshot(
        target_date=date(2026, 9, 4), output_path=tmp_path / 'empty.json'
    )
    snapshot = json.loads(output.read_text())
    assert snapshot['source_status'] == 'available'
    assert snapshot['result_status'] == 'empty'
    assert snapshot['at_risk_calls'] == 0
    assert snapshot['sad_calls'] == []
    assert snapshot['pages_read'] == 1


def test_collector_fails_closed_when_page_limit_is_exhausted(tmp_path, monkeypatch):
    import sys
    from contextlib import nullcontext
    from datetime import datetime
    from types import SimpleNamespace
    import pytest
    from robie_job_engine import magellan_collection as collector

    class Page:
        current = 0

        def set_default_timeout(self, value): pass
        def goto(self, *args, **kwargs): pass

        def locator(self, selector):
            if selector == collector.ROW_SELECTOR:
                return SimpleNamespace(first=SimpleNamespace(get_attribute=lambda key: f'call-success-row-{self.current}'))
            return SimpleNamespace(click=lambda: setattr(self, 'current', self.current + 1))

        def wait_for_function(self, expression, *, arg): pass

    page = Page()
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url: browser))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(sync_playwright=lambda: nullcontext(playwright)))
    monkeypatch.setattr(collector, '_authenticate', lambda *args, **kwargs: None)
    monkeypatch.setattr(collector, '_ensure_sad_filter', lambda page: None)
    monkeypatch.setattr(collector, '_wait_for_sad_results', lambda page: True)
    monkeypatch.setattr(collector, '_visible_row_datetimes', lambda page: [datetime(2026, 9, 4, 12 - page.current, 0)])
    monkeypatch.setattr(collector, '_next_enabled', lambda page: True)
    monkeypatch.setattr(
        collector,
        'parse_visible_rows',
        lambda page, target: ([{
            'call_id': str(page.current),
            'occurred_at': f'2026-09-04T{12 - page.current:02d}:00:00',
            'from_number': '+15555550101',
            'to_number': '+15555550199',
        }], False),
    )

    output = tmp_path / 'limited.json'
    with pytest.raises(RuntimeError, match='pagination limit reached'):
        collector.collect_magellan_snapshot(
            target_date=date(2026, 9, 4), output_path=output, max_pages=2
        )
    assert not output.exists()
