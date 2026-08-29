from __future__ import annotations

import time
from pathlib import Path
try:
    import pytest
    fixture = pytest.fixture
except ImportError:
    def fixture(fn):
        return fn

from robie_job_engine.ezlynx_poller import (
    EzlynxCustomerRecord,
    EzlynxPollerDaemon,
    EzlynxQuote,
    EzlynxSyncStore,
    MockEzlynxAdapter,
)


@fixture
def sync_store(tmp_path: Path) -> EzlynxSyncStore:
    db = tmp_path / "jobs.db"
    return EzlynxSyncStore(db)


def test_quote_store_crud(sync_store: EzlynxSyncStore):
    quote = EzlynxQuote(
        id="q-100",
        applicant_id="app-12345",
        carrier_name="Travelers Insurance",
        line_of_business="commercial_auto",
        quote_number="QT-99201",
        premium=4500.50,
        effective_date="2026-09-01",
        expiration_date="2027-09-01",
        status="QUOTED",
        down_payment=900.0,
        terms={"payment_plan": "Monthly EFT", "down_payment_pct": 20},
    )
    saved = sync_store.upsert_quote(quote)
    assert saved.id == "q-100"
    assert saved.premium == 4500.50

    # Retrieve quote
    fetched = sync_store.get_quote("app-12345", "Travelers Insurance", "commercial_auto", "QT-99201")
    assert fetched is not None
    assert fetched.quote_number == "QT-99201"
    assert fetched.terms.get("payment_plan") == "Monthly EFT"

    # List quotes
    quotes = sync_store.list_quotes(applicant_id="app-12345")
    assert len(quotes) == 1
    assert quotes[0].carrier_name == "Travelers Insurance"

    # Upsert with updated premium & status
    quote.premium = 4250.00
    quote.status = "BOUND"
    updated = sync_store.upsert_quote(quote)
    assert updated.premium == 4250.00
    assert updated.status == "BOUND"


def test_customer_store_crud(sync_store: EzlynxSyncStore):
    cust = EzlynxCustomerRecord(
        id="cust-01",
        applicant_id="app-9988",
        first_name="Jane",
        last_name="Doe",
        company_name="Apex Logistics Inc",
        email="jane@apexlogistics.com",
        phone="555-0199",
        address_line1="100 Main St",
        city="Los Angeles",
        state="CA",
        zip_code="90001",
        customer_type="COMMERCIAL",
    )
    saved = sync_store.upsert_customer(cust)
    assert saved.applicant_id == "app-9988"
    assert saved.company_name == "Apex Logistics Inc"

    fetched = sync_store.get_customer("app-9988")
    assert fetched is not None
    assert fetched.first_name == "Jane"
    assert fetched.state == "CA"

    by_state = sync_store.list_customers(state="CA")
    assert len(by_state) == 1
    assert by_state[0].company_name == "Apex Logistics Inc"


def test_poller_daemon_tick_and_checkpoints(tmp_path: Path):
    db = tmp_path / "jobs.db"
    mock_quotes = [
        {
            "id": "q-1",
            "applicant_id": "app-001",
            "carrier_name": "Progressive",
            "line_of_business": "commercial_auto",
            "quote_number": "PRG-881",
            "premium": 3200.0,
            "status": "QUOTED",
        },
        {
            "id": "q-2",
            "applicant_id": "app-002",
            "carrier_name": "Hartford",
            "line_of_business": "general_liability",
            "quote_number": "HFD-112",
            "premium": 1800.0,
            "status": "QUOTED",
        },
    ]
    mock_customers = [
        {
            "id": "c-1",
            "applicant_id": "app-001",
            "company_name": "TransFleet Corp",
            "email": "ops@transfleet.com",
            "state": "TX",
        }
    ]

    adapter = MockEzlynxAdapter(quotes=mock_quotes, customers=mock_customers)
    daemon = EzlynxPollerDaemon(db, adapter=adapter, poll_interval_seconds=1)

    # Perform a single poll tick
    tick_res = daemon.poll_tick()
    assert tick_res["quotes_extracted"] == 2
    assert tick_res["customers_synced"] == 1
    assert len(tick_res["errors"]) == 0

    # Verify quotes and customer sync tables
    quotes = daemon.store.list_quotes()
    assert len(quotes) == 2
    customers = daemon.store.list_customers()
    assert len(customers) == 1

    # Verify poller checkpoints
    q_cp = daemon.store.get_checkpoint("quotes")
    assert q_cp is not None
    assert q_cp["total_synced"] == 2
    assert q_cp["last_cursor"] is not None

    c_cp = daemon.store.get_checkpoint("customers")
    assert c_cp is not None
    assert c_cp["total_synced"] == 1


def test_poller_daemon_background_lifecycle(tmp_path: Path):
    db = tmp_path / "jobs.db"
    adapter = MockEzlynxAdapter()
    daemon = EzlynxPollerDaemon(db, adapter=adapter, poll_interval_seconds=1)

    assert not daemon.is_running()
    daemon.start()
    assert daemon.is_running()
    time.sleep(0.1)
    daemon.stop(timeout=2.0)
    assert not daemon.is_running()


def test_poller_error_resilience(tmp_path: Path):
    db = tmp_path / "jobs.db"

    class FailingAdapter:
        def fetch_quotes(self, **kwargs):
            raise ConnectionError("EZLynx upstream 503 Service Unavailable")

        def fetch_customers(self, **kwargs):
            raise TimeoutError("EZLynx Gateway Timeout")

    daemon = EzlynxPollerDaemon(db, adapter=FailingAdapter(), poll_interval_seconds=1)  # type: ignore[arg-type]
    tick_res = daemon.poll_tick()
    assert tick_res["quotes_extracted"] == 0
    assert tick_res["customers_synced"] == 0
    assert len(tick_res["errors"]) == 2
    assert any("503 Service Unavailable" in e for e in tick_res["errors"])

    # Checkpoint records the error
    q_cp = daemon.store.get_checkpoint("quotes")
    assert q_cp is not None
    assert "503 Service Unavailable" in str(q_cp["last_error"])


def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite()
