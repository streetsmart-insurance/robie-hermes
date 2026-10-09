"""Stage-one read-only accounting checker regression cases (synthetic records)."""
from datetime import datetime, timezone

from robie_job_engine.daily_accounting_checks import (
    Evidence, SourceSnapshot, assess_clearing, changes_since, grade_item, build_daily_report,
)


def item(source, id, status, amount="100.00", date="2026-09-26"):
    return Evidence(source=source, source_id=id, observed_at="2026-09-26T10:00:00Z",
                    event_date=date, amount=amount, currency="USD", status=status,
                    account="SYN-ACCOUNT", field="status", match_ref="SYN-MATCH", url=f"https://example.invalid/{source}/{id}")


def test_first_run_is_baseline_and_not_new_events():
    snap = SourceSnapshot("Ascend", [item("Ascend", "A", "scheduled")], complete=True)
    assert changes_since(None, snap) == []
    assert len(changes_since(SourceSnapshot("Ascend", [item("Ascend", "A", "scheduled")], complete=True),
                             SourceSnapshot("Ascend", [item("Ascend", "A", "returned")], complete=True))) == 1


def test_incomplete_page_never_yields_no_changes_claim():
    old = SourceSnapshot("Ascend", [item("Ascend", "A", "scheduled")], complete=True)
    new = SourceSnapshot("Ascend", [], complete=False, error="pagination stopped")
    report = build_daily_report({"Ascend": new}, {"Ascend": old})
    assert report["coverage"]["Ascend"]["complete"] is False
    assert "no changes" not in str(report).lower()
    assert report["findings"] == []


def test_finance_cleared_and_qbo_without_bank_are_not_clearing_proof():
    finance = item("Ascend", "P1", "cleared")
    qbo = item("QBO", "Q1", "reconciled")
    verdict = assess_clearing(finance, bank=None, qbo=qbo)
    assert verdict.status == "bank clearing not proven"
    assert verdict.evidence == (finance, qbo)


def test_matching_bank_and_fresh_qbo_are_three_source_evidence():
    finance = item("Ascend", "P1", "cleared")
    bank = item("Wells", "B1", "posted")
    qbo = item("QBO", "Q1", "reconciled")
    verdict = assess_clearing(finance, bank=bank, qbo=qbo)
    assert verdict.status == "bank posted, corroborated by QBO"
    assert len(verdict.evidence) == 3
    assert assess_clearing(finance, bank=item("Wells", "B2", "posted", amount="99.00"), qbo=qbo).status == "mismatch - human review"
    assert assess_clearing(finance, bank=bank, qbo=qbo, qbo_fresh=False).status == "bank posted, QBO stale"


def test_grade_unknown_is_not_pass_and_false_clearing_fails():
    e = item("Ascend", "P1", "cleared")
    assert grade_item(e, independent_check=None).verdict == "unknown"
    assert grade_item(e, independent_check={"matched": False, "checked_at": "2026-09-26T11:00:00Z",
                                          "source": "Wells", "source_id": "B1"}).verdict == "incorrect"
    assert grade_item(e, independent_check={"matched": True, "checked_at": "2026-09-26T11:00:00Z",
                                          "source": "Wells", "source_id": "B1"}).verdict == "correct"


def test_changed_poll_time_alone_is_not_event():
    old = item("Ascend", "A", "scheduled")
    newer = Evidence(**{**old.__dict__, "observed_at": "2026-09-27T10:00:00Z"})
    assert changes_since(SourceSnapshot("Ascend", [old], True), SourceSnapshot("Ascend", [newer], True)) == []


def test_bank_same_dollars_without_transaction_identity_cannot_prove_receipt():
    finance = item("Ascend", "P1", "cleared")
    bank = Evidence(**{**item("Wells", "B1", "posted").__dict__, "match_ref": ""})
    assert assess_clearing(finance, bank).status == "mismatch - human review"
    assert grade_item(finance, {"matched": True, "checked_at": "2026-09-26T11:00:00Z",
                                "source": "Ascend", "source_id": "P1"}).verdict == "unknown"
