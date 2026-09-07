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
