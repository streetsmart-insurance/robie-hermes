"""Tests for carrier_pull_health_check.

Covers Carlo's both-ways rule:
- healthy path: all carriers have today's output -> quiet, no alert, exit 0
- broken path: one carrier missing -> alert names the carrier in plain English, exit 1
- held path: carrier held with reason -> reported as held, not a failure

All offline, tmp dirs only. Never touches the real QA root, never posts.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine import carrier_pull_health_check as health

DAY = "2026-10-05"
PDF_BYTES = b"%PDF-1.4\nfake\n%%EOF\n"


@pytest.fixture()
def qa_root(tmp_path):
    root = tmp_path / "carrier-pull-qa"
    root.mkdir()
    return root


def _make_ok(qa_root, carrier, day=DAY, pdfs=1, receipt_held=0):
    folder = qa_root / carrier / day
    folder.mkdir(parents=True)
    for i in range(pdfs):
        (folder / f"doc-{i}.pdf").write_bytes(PDF_BYTES)
    if receipt_held:
        held = [{"reason": f"test hold {i}"} for i in range(receipt_held)]
        (folder / "manifest.json").write_text(json.dumps({"held": held}))
    return folder


def _make_held(qa_root, carrier, day=DAY, reason="portal login expired"):
    folder = qa_root / carrier / day
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(
        json.dumps({"held": [{"reason": reason}], "downloaded": []})
    )
    return folder


def _make_zero_results(qa_root, carrier, day=DAY):
    folder = qa_root / carrier / day
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "zero-results.json").write_text(json.dumps({"count": 0}))
    return folder


# --- healthy path -----------------------------------------------------------

def test_all_ok_stays_quiet(qa_root):
    for carrier in health.CARRIERS:
        _make_ok(qa_root, health.CARRIERS[carrier])
    report = health.probe(root=qa_root, as_of=date.fromisoformat(DAY))
    assert report["green"] is True
    assert report["summary"] == {"ok": 7, "held": 0, "missing": 0, "total": 7}
    decision = health.maybe_alert(report)
    assert decision["alerted"] is False
    assert decision["state"] == "green-quiet"
    assert "message" not in decision


def test_zero_results_counts_as_ok(qa_root):
    for carrier in health.CARRIERS:
        _make_ok(qa_root, health.CARRIERS[carrier])
    _make_zero_results(qa_root, health.CARRIERS["geico"])
    # remove geico's pdf so only the zero-results marker remains
    for p in (qa_root / "geico" / DAY).glob("*.pdf"):
        p.unlink()
    result = health.check_carrier("geico", root=qa_root, as_of=date.fromisoformat(DAY))
    assert result["status"] == "OK"
    assert "zero actionable" in result["reason"]


def test_nested_subfolder_tolerated(qa_root):
    # Some modules nest: {root}/{CarrierName}/{YYYY-MM-DD}/
    folder = qa_root / "guard" / "Guard" / DAY
    folder.mkdir(parents=True)
    (folder / "notice.pdf").write_bytes(PDF_BYTES)
    result = health.check_carrier("guard", root=qa_root, as_of=date.fromisoformat(DAY))
    assert result["status"] == "OK"


# --- broken path ------------------------------------------------------------

def test_one_missing_alerts_in_plain_english(qa_root):
    for carrier in health.CARRIERS:
        if carrier == "natgen":
            continue
        _make_ok(qa_root, health.CARRIERS[carrier])
    report = health.probe(root=qa_root, as_of=date.fromisoformat(DAY))
    assert report["green"] is False
    assert report["summary"]["missing"] == 1
    decision = health.maybe_alert(report)
    assert decision["state"] == "red"
    assert decision["alerted"] is False  # no poster wired
    message = decision["message"]
    assert "NatGen" in message
    assert "no QA pack folder" in message
    # plain English: no jargon dumps
    assert "carrier_pull_health_check" not in message.lower()
    for missing in [c for c in report["carriers"] if c["status"] == "MISSING"]:
        assert missing["carrier"] == "natgen"


def test_all_missing_alert_lists_every_carrier(qa_root):
    report = health.probe(root=qa_root, as_of=date.fromisoformat(DAY))
    assert report["green"] is False
    assert report["summary"]["missing"] == 7
    message = health.render_alert(report)
    for name in ("Guard", "Progressive", "GEICO", "Travelers", "NatGen",
                 "Utica First", "Farmers of Salem"):
        assert name in message


def test_empty_folder_is_missing_not_ok(qa_root):
    (qa_root / "guard" / DAY).mkdir(parents=True)
    result = health.check_carrier("guard", root=qa_root, as_of=date.fromisoformat(DAY))
    assert result["status"] == "MISSING"
    assert "no documents" in result["reason"]


# --- held path --------------------------------------------------------------

def test_held_is_not_a_failure(qa_root):
    for carrier in health.CARRIERS:
        _make_ok(qa_root, health.CARRIERS[carrier])
    _make_held(qa_root, health.CARRIERS["uticafirst"], reason="portal session expired")
    for p in (qa_root / "uticafirst" / DAY).glob("*.pdf"):
        p.unlink()
    report = health.probe(root=qa_root, as_of=date.fromisoformat(DAY))
    assert report["green"] is True  # held does not break green
    assert report["summary"]["held"] == 1
    assert report["summary"]["missing"] == 0
    decision = health.maybe_alert(report)
    assert decision["state"] == "green-quiet"


def test_held_reason_reported(qa_root):
    _make_held(qa_root, "guard", reason="portal session expired")
    result = health.check_carrier("guard", root=qa_root, as_of=date.fromisoformat(DAY))
    assert result["status"] == "HELD"
    assert "portal session expired" in result["reason"]


def test_held_with_downloads_is_ok(qa_root):
    # A run that downloaded some and held others is OK, not HELD.
    folder = _make_ok(qa_root, "guard", pdfs=2)
    (folder / "manifest.json").write_text(
        json.dumps({"held": [{"reason": "one ambiguous"}], "downloaded": ["a", "b"]})
    )
    result = health.check_carrier("guard", root=qa_root, as_of=date.fromisoformat(DAY))
    assert result["status"] == "OK"


# --- probe robustness -------------------------------------------------------

def test_missing_qa_root_is_all_missing(tmp_path):
    report = health.probe(root=tmp_path / "does-not-exist",
                          as_of=date.fromisoformat(DAY))
    assert report["green"] is False
    assert report["summary"]["missing"] == 7


def test_carrier_subset(qa_root):
    _make_ok(qa_root, "guard")
    report = health.probe(root=qa_root, as_of=date.fromisoformat(DAY),
                          carriers=["guard"])
    assert report["green"] is True
    assert report["summary"] == {"ok": 1, "held": 0, "missing": 0, "total": 1}


def test_main_exit_codes(qa_root, capsys):
    for carrier in health.CARRIERS:
        _make_ok(qa_root, health.CARRIERS[carrier])
    code = health.main(["--qa-root", str(qa_root), "--as-of", DAY])
    assert code == 0
    code = health.main(["--qa-root", str(qa_root), "--as-of", "2026-10-06"])
    assert code == 1


def test_poster_called_on_red(qa_root):
    report = health.probe(root=qa_root, as_of=date.fromisoformat(DAY))
    assert not report["green"]
    posted = []
    decision = health.maybe_alert(report, poster=posted.append)
    assert decision["alerted"] is True
    assert len(posted) == 1
    assert "NatGen" in posted[0] or "Guard" in posted[0]


def test_poster_failure_does_not_raise(qa_root):
    report = health.probe(root=qa_root, as_of=date.fromisoformat(DAY))

    def bad_poster(text):
        raise RuntimeError("chat down")

    decision = health.maybe_alert(report, poster=bad_poster)
    assert decision["alerted"] is False
    assert "RuntimeError" in decision["error"]


def test_recovery_message(qa_root):
    for carrier in health.CARRIERS:
        _make_ok(qa_root, health.CARRIERS[carrier])
    report = health.probe(root=qa_root, as_of=date.fromisoformat(DAY))
    text = health.render_recovery(report)
    assert DAY in text
    assert "7 carrier(s) OK" in text
