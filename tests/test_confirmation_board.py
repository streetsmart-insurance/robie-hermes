"""Tests for the Confirmations board tab (confirmation_board.py).

A fake in-memory Sheets values API stands in for Google: no credentials,
no network. Every test runs against a throwaway sqlite db.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robie_job_engine import confirmation_board
from robie_job_engine import confirmations
from robie_job_engine.store import JobStore


SHEET_ID = "test-sheet"
TEST_KEY = "test-decision-signing-key-0123456789abcdef"
PRINCIPAL = "carlo@streetsmart.insurance"


def _token(cid, decision, principal=PRINCIPAL, **kw):
    kw.setdefault("key", TEST_KEY)
    return confirmations.mint_decision_token(cid, decision, principal, **kw)


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeValuesApi:
    """Minimal in-memory stand-in for the Sheets values API."""

    def __init__(self):
        self.cells: dict[tuple[str, str], list[list[str]]] = {}
        self.titles: set[str] = set()

    # -- values API --
    def get(self, spreadsheetId, range):  # noqa: N803
        tab = range.split("!", 1)[0]
        return _FakeExecutable({"values": [list(r) for r in self.cells.get((spreadsheetId, tab), [])]})

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: N803
        tab = range.split("!", 1)[0]
        self.titles.add(tab)
        self.cells[(spreadsheetId, tab)] = [list(r) for r in body["values"]]
        return _FakeExecutable({"updatedRange": range})

    def clear(self, spreadsheetId, range):  # noqa: N803
        tab = range.split("!", 1)[0]
        self.cells[(spreadsheetId, tab)] = []
        return _FakeExecutable({})


class FakeSheetsApi:
    """Stand-in for spreadsheets.get / spreadsheets.batchUpdate (tab mgmt)."""

    def __init__(self, values: FakeValuesApi):
        self.values = values

    def get(self, spreadsheetId, fields):  # noqa: N803
        return _FakeExecutable({
            "sheets": [{"properties": {"title": t}} for t in sorted(self.values.titles)]
        })

    def batchUpdate(self, spreadsheetId, body):  # noqa: N803
        for req in body.get("requests", []):
            title = req.get("addSheet", {}).get("properties", {}).get("title")
            if title:
                self.values.titles.add(title)
        return _FakeExecutable({})


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "jobs.db")


@pytest.fixture()
def apis():
    values = FakeValuesApi()
    return values, FakeSheetsApi(values)


def _request(store, **kw):
    args = dict(
        loop_job_id="loop-1",
        job_type="policy_change",
        draft_summary="Raise written premium",
        changes_json={"policy_number": "HO-1", "changes": {"writtenPremium": {"old": "2000", "new": "2450"}}},
        requested_by="robie",
    )
    args.update(kw)
    return confirmations.request_confirmation(store=store, **args)


def test_sync_creates_tab_and_writes_headers(db, apis):
    values, sheets = apis
    result = confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    assert result["tab"] == "Confirmations"
    assert confirmation_board.TAB_TITLE in values.titles
    rows = values.cells[(SHEET_ID, "Confirmations")]
    assert rows[0] == confirmation_board.HEADERS


def test_pending_row_is_plain_english(db, apis):
    values, sheets = apis
    store = JobStore(db)
    _request(store)
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    assert len(rows) == 2  # headers + one pending row
    row = rows[1]
    assert row[confirmation_board.STATUS] == "PENDING"
    assert "waiting for approval" in row[confirmation_board.SUMMARY]
    assert "HO-1" in row[confirmation_board.DETAILS]
    assert "2450" in row[confirmation_board.DETAILS]
    # The human's decision column starts empty.
    assert row[confirmation_board.DECISION] == ""


def test_approve_from_sheet_is_ingested(db, apis):
    values, sheets = apis
    store = JobStore(db)
    cid = _request(store)
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    # Carlo pastes his signed APPROVE token in the decision column.
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows[1][confirmation_board.DECISION] = f"APPROVE {_token(cid, 'APPROVE')}"
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    assert result["decisions_applied"] == 1
    assert result["applied"][0]["confirmation_id"] == cid
    assert result["applied"][0]["decided_by"] == PRINCIPAL
    record = confirmations.get(cid, store=store)
    assert record["status"] == "APPROVED"
    assert record["decided_by"] == PRINCIPAL
    # Write-back shows the decision on the tab.
    rows = values.cells[(SHEET_ID, "Confirmations")]
    assert rows[1][confirmation_board.STATUS] == "APPROVED"
    assert rows[1][confirmation_board.DECIDED_BY] == PRINCIPAL


def test_reject_from_sheet_with_reason(db, apis):
    values, sheets = apis
    store = JobStore(db)
    cid = _request(store)
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows[1][confirmation_board.DECISION] = _token(cid, "REJECT")
    rows[1][confirmation_board.REASON] = "wrong premium"
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    assert result["decisions_applied"] == 1
    record = confirmations.get(cid, store=store)
    assert record["status"] == "REJECTED"
    assert record["decision_reason"] == "wrong premium"


def test_double_decision_is_ignored(db, apis):
    values, sheets = apis
    store = JobStore(db)
    cid = _request(store)
    confirmations.approve(cid, "Carlo Ferrara", store=store)
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    # Someone pastes a valid REJECT token on an already-approved row:
    # must not flip it.
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows[1][confirmation_board.DECISION] = _token(cid, "REJECT")
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    assert result["decisions_applied"] == 0
    assert confirmations.get(cid, store=store)["status"] == "APPROVED"


def test_unknown_id_on_sheet_is_ignored(db, apis):
    values, sheets = apis
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows.append(["no-such-id", "", "", "", "", "", "", _token("no-such-id", "APPROVE"), "", "", ""])
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    assert result["decisions_applied"] == 0


def test_gibberish_decision_is_ignored(db, apis):
    values, sheets = apis
    store = JobStore(db)
    cid = _request(store)
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows[1][confirmation_board.DECISION] = "maybe"
    result = confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    assert result["decisions_applied"] == 0
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_pending_decision_text_survives_rewrite(db, apis):
    values, sheets = apis
    store = JobStore(db)
    cid = _request(store)
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    # Simulate decision typed after ingest but before rewrite: put the text
    # directly on the tab, then run only the rewrite portion via a second
    # sync whose ingest finds nothing new... instead emulate by writing the
    # decision into the fake tab and checking carry-over on rewrite.
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows[1][confirmation_board.DECISION] = _token(cid, "APPROVE")
    # First ingest consumes it (normal path)...
    confirmation_board.apply_decisions_from_sheet(
        db, SHEET_ID, values_api=values, decision_key=TEST_KEY
    )
    assert confirmations.get(cid, store=store)["status"] == "APPROVED"
    # ...and a fresh PENDING row keeps its typed text across a rewrite.
    cid2 = _request(store, loop_job_id="loop-2")
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    pending_rows = [r for r in rows[1:] if r[confirmation_board.STATUS] == "PENDING"]
    assert len(pending_rows) == 1
    pending_rows[0][confirmation_board.DECISION] = _token(cid2, "REJECT")
    pending_rows[0][confirmation_board.REASON] = "typed late"
    # Rewrite path: call sync again; ingest will apply it, proving the text
    # was not wiped before ingest.
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    assert result["decisions_applied"] == 1
    assert confirmations.get(cid2, store=store)["status"] == "REJECTED"


def test_expired_sweep_runs_on_sync(db, apis):
    values, sheets = apis
    store = JobStore(db)
    cid = _request(store)
    # Backdate the record past the 72h expiry.
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE plan_confirmations SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (cid,),
        )
        conn.commit()
    finally:
        conn.close()
    result = confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    assert result["expired"] == 1
    assert confirmations.get(cid, store=store)["status"] == "EXPIRED"


def test_read_back_mismatch_raises(db, apis):
    values, sheets = apis
    store = JobStore(db)
    _request(store)

    class _LyingApi(FakeValuesApi):
        def get(self, spreadsheetId, range):  # noqa: N803
            return _FakeExecutable({"values": [["wrong headers"]]})

    lying = _LyingApi()
    with pytest.raises(RuntimeError):
        confirmation_board.sync_confirmations(db, SHEET_ID, values_api=lying, sheets_api=sheets)


# ---------------------------------------------------------------------------
# Critical 2 regressions: the sheet is not an authorization surface
# ---------------------------------------------------------------------------

def _sync_with_pending(db, apis):
    values, sheets = apis
    store = JobStore(db)
    cid = confirmations.request_confirmation(
        store=store, loop_job_id="loop-1", job_type="policy_change",
        draft_summary="Raise written premium",
        changes_json={"policy_number": "HO-1", "changes": {"writtenPremium": "2450"}},
        requested_by="robie",
    )
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    return values, sheets, store, cid


def test_bare_word_without_token_is_never_ingested(db, apis):
    """The old hole: typing APPROVE (+ any Decided by name) authorized the
    plan. It must now be skipped as unauthenticated."""
    values, sheets, store, cid = _sync_with_pending(db, apis)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows[1][confirmation_board.DECISION] = "APPROVE"
    rows[1][confirmation_board.DECIDED_BY] = "Carlo Ferrara"  # self-asserted
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    assert result["decisions_applied"] == 0
    assert result["skipped_unauthenticated"] == 1
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def _row_index(rows, cid):
    for i, row in enumerate(rows):
        if i > 0 and row[confirmation_board.ID] == cid:
            return i
    raise AssertionError(f"confirmation {cid} not on the tab")


def _request_second_pending(store):
    return confirmations.request_confirmation(
        store=store, loop_job_id="loop-2", job_type="policy_change",
        changes_json={"policy_number": "HO-2", "changes": {}},
        requested_by="robie",
    )


def test_no_signing_key_means_display_only(db, apis, monkeypatch):
    """Without a configured key even a well-formed token cannot authorize.

    Discriminates from the old code path: the reporting surface
    (``skipped_unauthenticated`` / per-row skip reasons) only exists on the
    new code, so this test fails on old code instead of passing vacuously.
    """
    monkeypatch.delenv(confirmations.DECISION_TOKEN_ENV, raising=False)
    values, sheets, store, cid = _sync_with_pending(db, apis)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows[_row_index(rows, cid)][confirmation_board.DECISION] = _token(cid, "APPROVE")
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
    )
    assert result["decisions_applied"] == 0
    assert result["skipped_unauthenticated"] == 1
    assert result["skipped"][0]["confirmation_id"] == cid
    assert "no signing key" in result["skipped"][0]["reason"]
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_forged_token_is_rejected_but_valid_token_on_same_sync_applies(db, apis):
    """Forged token refused while a valid token on a sibling row applies in
    the SAME sync -- proves the decision path is live, so the refusal is
    not vacuous (old code would apply neither row here)."""
    values, sheets, store, cid = _sync_with_pending(db, apis)
    good = _request_second_pending(store)
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    forged = confirmations.mint_decision_token(
        cid, "APPROVE", PRINCIPAL, key="attacker-key-attacker-key"
    )
    rows[_row_index(rows, cid)][confirmation_board.DECISION] = forged
    rows[_row_index(rows, good)][confirmation_board.DECISION] = _token(good, "APPROVE")
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    # The live path decided the valid row...
    assert result["decisions_applied"] == 1
    assert confirmations.get(good, store=store)["status"] == "APPROVED"
    # ...and refused the forgery with a named reason.
    assert result["skipped_unauthenticated"] == 1
    assert result["skipped"][0]["confirmation_id"] == cid
    assert "invalid token" in result["skipped"][0]["reason"]
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_token_for_a_different_confirmation_is_rejected(db, apis):
    """A valid token pasted on the wrong row is refused while the same
    token applies on its own row -- the refusal is discriminating, not a
    dead code path (old code applied neither and passed vacuously)."""
    values, sheets, store, cid = _sync_with_pending(db, apis)
    other = _request_second_pending(store)
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    # Paste the OTHER confirmation's token onto this row...
    rows[_row_index(rows, cid)][confirmation_board.DECISION] = _token(other, "APPROVE")
    # ...and the correct token onto its own row.
    rows[_row_index(rows, other)][confirmation_board.DECISION] = _token(other, "APPROVE")
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    assert result["decisions_applied"] == 1
    assert confirmations.get(other, store=store)["status"] == "APPROVED"
    assert result["skipped_unauthenticated"] == 1
    assert result["skipped"][0]["confirmation_id"] == cid
    assert "different confirmation" in result["skipped"][0]["reason"]
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_misconfigured_short_key_degrades_to_display_only(db, apis):
    """A <16-byte key must not crash the sync (it runs inside sheets_sync
    alongside unrelated tabs): log a warning and stay display-only."""
    values, sheets, store, cid = _sync_with_pending(db, apis)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows[_row_index(rows, cid)][confirmation_board.DECISION] = _token(cid, "APPROVE")
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key="short",
    )
    # Old behavior: ValueError raised out of the whole sync. Now the sync
    # completes, nothing is ingested, and the reason is reported.
    assert result["decisions_applied"] == 0
    assert result["skipped_unauthenticated"] == 1
    assert "no signing key" in result["skipped"][0]["reason"]
    assert result["rows"] == 1
    assert confirmations.get(cid, store=store)["status"] == "PENDING"


def test_typed_decided_by_name_is_never_trusted(db, apis):
    """The Decided by cell is display-only; identity comes from the token."""
    values, sheets, store, cid = _sync_with_pending(db, apis)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    rows[1][confirmation_board.DECISION] = _token(cid, "APPROVE")
    rows[1][confirmation_board.DECIDED_BY] = "Mallory Attacker"
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    assert result["decisions_applied"] == 1
    assert result["applied"][0]["decided_by"] == PRINCIPAL


def test_expired_token_is_rejected_but_fresh_token_on_same_sync_applies(db, apis):
    from datetime import datetime, timedelta, timezone

    values, sheets, store, cid = _sync_with_pending(db, apis)
    good = _request_second_pending(store)
    confirmation_board.sync_confirmations(db, SHEET_ID, values_api=values, sheets_api=sheets)
    rows = values.cells[(SHEET_ID, "Confirmations")]
    stale = _token(
        cid, "APPROVE",
        now=datetime.now(timezone.utc) - timedelta(days=30),
        ttl_seconds=60,
    )
    rows[_row_index(rows, cid)][confirmation_board.DECISION] = stale
    rows[_row_index(rows, good)][confirmation_board.DECISION] = _token(good, "APPROVE")
    result = confirmation_board.sync_confirmations(
        db, SHEET_ID, values_api=values, sheets_api=sheets,
        decision_key=TEST_KEY,
    )
    # The fresh token applies in the same sync, so the expired refusal is
    # a live discrimination, not a vacuous pass.
    assert result["decisions_applied"] == 1
    assert confirmations.get(good, store=store)["status"] == "APPROVED"
    assert result["skipped_unauthenticated"] == 1
    assert result["skipped"][0]["confirmation_id"] == cid
    assert "invalid token" in result["skipped"][0]["reason"]
    assert confirmations.get(cid, store=store)["status"] == "PENDING"
