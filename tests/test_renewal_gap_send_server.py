"""Regression tests for scripts/renewal_gap_send_server.py.

Covers the classification rule (Carlo 2026-10-06: every policy with no
renewal term entered is MISSING; paperwork in file -> NEEDS ENTRY,
otherwise CHASE CARRIER) and the department email body construction.
"""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "renewal_gap_send_server",
    Path(__file__).parent.parent / "scripts" / "renewal_gap_send_server.py",
)
mod = importlib.util.module_from_spec(spec)
sys.modules["renewal_gap_send_server"] = mod
spec.loader.exec_module(mod)


def rec(**kw):
    base = {"expires": "2026-10-12", "policy": "TEST123",
            "lob": "Flood", "insured": "Test Insured",
            "renewal_doc_names": [], "discussions": []}
    base.update(kw)
    return base


def test_classify_needs_entry_when_paperwork_in_file():
    r = rec(renewal_doc_names=["Renewal Offer 2026.pdf"])
    assert mod.classify(r) == mod.STATUS_NEEDS_ENTRY


def test_classify_chase_when_nothing_in_file():
    r = rec(renewal_doc_names=[])
    assert mod.classify(r) == mod.STATUS_CHASE


def test_classify_ignores_pure_non_renewal_notice():
    r = rec(renewal_doc_names=["Non-Renewal Notice.pdf"])
    assert mod.classify(r) == mod.STATUS_CHASE


def test_build_body_counts():
    records = [
        rec(policy="A1", renewal_doc_names=["offer.pdf"]),
        rec(policy="B2", renewal_doc_names=[]),
    ]
    body = mod.build_body("Personal", records)
    assert "Personal has 2 policies" in body
    assert "1 have no renewal paperwork" in body
    assert "1 have paperwork in the file" in body
    assert "A1" in body and "B2" in body
    assert mod.STATUS_NEEDS_ENTRY in body
    assert mod.STATUS_CHASE in body


def test_discussion_title_present():
    r = rec(discussions=[{"title": "Flood Renewal 2026",
                          "lastModified": "2026-10-05T10:00:00"}])
    assert "Flood Renewal 2026 (2026-10-05)" in mod.discussion_title(r)


def test_discussion_title_missing():
    assert mod.discussion_title(rec()) == "no discussion"
