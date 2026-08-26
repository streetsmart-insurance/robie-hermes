from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

from .store import JobStore


UTC = timezone.utc
logger = logging.getLogger("robie.ezlynx_poller")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(val: Any) -> str:
    return json.dumps(val, separators=(",", ":"), sort_keys=True)


@dataclass
class EzlynxQuote:
    id: str
    applicant_id: str
    carrier_name: str
    line_of_business: str
    quote_number: str | None = None
    carrier_id: str | None = None
    premium: float = 0.0
    effective_date: str | None = None
    expiration_date: str | None = None
    status: str = "QUOTED"  # QUOTED, BOUND, DECLINED, EXPIRED, IN_REVIEW
    down_payment: float | None = None
    terms: dict[str, Any] = field(default_factory=dict)
    raw_data: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None
    synced_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EzlynxCustomerRecord:
    id: str
    applicant_id: str
    first_name: str | None = None
    last_name: str | None = None
    company_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    customer_type: str = "COMMERCIAL"  # COMMERCIAL, PERSONAL
    custom_fields: dict[str, Any] = field(default_factory=dict)
    sync_status: str = "SYNCED"
    last_polled_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EzlynxApiAdapter(Protocol):
    """Protocol for fetching quotes and applicant records from EZLynx."""

    def fetch_quotes(
        self, *, applicant_id: str | None = None, since_cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    def fetch_customers(
        self, *, since_cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]: ...


class MockEzlynxAdapter:
    """Mock adapter for development, testing, and fallback simulation."""

    def __init__(self, quotes: list[dict[str, Any]] | None = None, customers: list[dict[str, Any]] | None = None):
        self.quotes = quotes or []
        self.customers = customers or []

    def fetch_quotes(
        self, *, applicant_id: str | None = None, since_cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        results = []
        for q in self.quotes:
            if applicant_id and q.get("applicant_id") != applicant_id:
                continue
            results.append(q)
        return results, f"cursor_quotes_{int(time.time())}"

    def fetch_customers(
        self, *, since_cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        return list(self.customers), f"cursor_cust_{int(time.time())}"


class EzlynxSyncStore:
    """Database store for EZLynx extracted quotes, synced customer records, and poller checkpoints."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ezlynx_quotes (
                    id TEXT PRIMARY KEY,
                    applicant_id TEXT NOT NULL,
                    carrier_id TEXT,
                    carrier_name TEXT NOT NULL,
                    line_of_business TEXT NOT NULL,
                    quote_number TEXT,
                    premium REAL NOT NULL DEFAULT 0.0,
                    effective_date TEXT,
                    expiration_date TEXT,
                    status TEXT NOT NULL DEFAULT 'QUOTED',
                    down_payment REAL,
                    terms_json TEXT NOT NULL DEFAULT '{}',
                    raw_data_json TEXT NOT NULL DEFAULT '{}',
                    job_id TEXT,
                    synced_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(applicant_id, carrier_name, line_of_business, quote_number)
                );
                CREATE INDEX IF NOT EXISTS idx_ezlynx_quotes_applicant ON ezlynx_quotes(applicant_id);
                CREATE INDEX IF NOT EXISTS idx_ezlynx_quotes_carrier ON ezlynx_quotes(carrier_name, line_of_business);

                CREATE TABLE IF NOT EXISTS ezlynx_customer_syncs (
                    id TEXT PRIMARY KEY,
                    applicant_id TEXT NOT NULL UNIQUE,
                    first_name TEXT,
                    last_name TEXT,
                    company_name TEXT,
                    email TEXT,
                    phone TEXT,
                    address_line1 TEXT,
                    city TEXT,
                    state TEXT,
                    zip_code TEXT,
                    customer_type TEXT NOT NULL DEFAULT 'COMMERCIAL',
                    custom_fields_json TEXT NOT NULL DEFAULT '{}',
                    sync_status TEXT NOT NULL DEFAULT 'SYNCED',
                    last_polled_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ezlynx_customer_applicant ON ezlynx_customer_syncs(applicant_id);

                CREATE TABLE IF NOT EXISTS ezlynx_poller_checkpoints (
                    feed_name TEXT PRIMARY KEY,
                    last_cursor TEXT,
                    last_poll_started_at TEXT,
                    last_poll_completed_at TEXT,
                    last_error TEXT,
                    total_synced INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    # ---------------- Quote Persistence ----------------

    def upsert_quote(self, quote: EzlynxQuote) -> EzlynxQuote:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO ezlynx_quotes
                (id, applicant_id, carrier_id, carrier_name, line_of_business, quote_number,
                 premium, effective_date, expiration_date, status, down_payment,
                 terms_json, raw_data_json, job_id, synced_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(applicant_id, carrier_name, line_of_business, quote_number) DO UPDATE SET
                    carrier_id=excluded.carrier_id,
                    premium=excluded.premium,
                    effective_date=excluded.effective_date,
                    expiration_date=excluded.expiration_date,
                    status=excluded.status,
                    down_payment=excluded.down_payment,
                    terms_json=excluded.terms_json,
                    raw_data_json=excluded.raw_data_json,
                    job_id=COALESCE(excluded.job_id, ezlynx_quotes.job_id),
                    synced_at=excluded.synced_at,
                    updated_at=excluded.updated_at""",
                (
                    quote.id, quote.applicant_id, quote.carrier_id, quote.carrier_name,
                    quote.line_of_business, quote.quote_number, quote.premium,
                    quote.effective_date, quote.expiration_date, quote.status,
                    quote.down_payment, _json(quote.terms), _json(quote.raw_data),
                    quote.job_id, quote.synced_at or now, now,
                ),
            )
        return self.get_quote(quote.applicant_id, quote.carrier_name, quote.line_of_business, quote.quote_number)  # type: ignore[return-value]

    def get_quote(
        self, applicant_id: str, carrier_name: str, line_of_business: str, quote_number: str | None
    ) -> EzlynxQuote | None:
        with self._connect() as conn:
            if quote_number is None:
                row = conn.execute(
                    """SELECT * FROM ezlynx_quotes WHERE applicant_id=? AND carrier_name=?
                       AND line_of_business=? AND quote_number IS NULL""",
                    (applicant_id, carrier_name, line_of_business),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM ezlynx_quotes WHERE applicant_id=? AND carrier_name=?
                       AND line_of_business=? AND quote_number=?""",
                    (applicant_id, carrier_name, line_of_business, quote_number),
                ).fetchone()
        if not row:
            return None
        return self._decode_quote(row)

    def list_quotes(
        self, applicant_id: str | None = None, line_of_business: str | None = None
    ) -> list[EzlynxQuote]:
        query = "SELECT * FROM ezlynx_quotes"
        clauses = []
        params: list[Any] = []
        if applicant_id:
            clauses.append("applicant_id=?")
            params.append(applicant_id)
        if line_of_business:
            clauses.append("line_of_business=?")
            params.append(line_of_business)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode_quote(r) for r in rows]

    # ---------------- Customer Persistence ----------------

    def upsert_customer(self, customer: EzlynxCustomerRecord) -> EzlynxCustomerRecord:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO ezlynx_customer_syncs
                (id, applicant_id, first_name, last_name, company_name, email, phone,
                 address_line1, city, state, zip_code, customer_type, custom_fields_json,
                 sync_status, last_polled_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(applicant_id) DO UPDATE SET
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    company_name=excluded.company_name,
                    email=excluded.email,
                    phone=excluded.phone,
                    address_line1=excluded.address_line1,
                    city=excluded.city,
                    state=excluded.state,
                    zip_code=excluded.zip_code,
                    customer_type=excluded.customer_type,
                    custom_fields_json=excluded.custom_fields_json,
                    sync_status=excluded.sync_status,
                    last_polled_at=excluded.last_polled_at,
                    updated_at=excluded.updated_at""",
                (
                    customer.id, customer.applicant_id, customer.first_name, customer.last_name,
                    customer.company_name, customer.email, customer.phone, customer.address_line1,
                    customer.city, customer.state, customer.zip_code, customer.customer_type,
                    _json(customer.custom_fields), customer.sync_status, customer.last_polled_at or now, now,
                ),
            )
        res = self.get_customer(customer.applicant_id)
        assert res is not None
        return res

    def get_customer(self, applicant_id: str) -> EzlynxCustomerRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ezlynx_customer_syncs WHERE applicant_id=?", (applicant_id,)
            ).fetchone()
        if not row:
            return None
        return self._decode_customer(row)

    def list_customers(self, state: str | None = None) -> list[EzlynxCustomerRecord]:
        query = "SELECT * FROM ezlynx_customer_syncs"
        params: list[Any] = []
        if state:
            query += " WHERE state=?"
            params.append(state.upper())
        query += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode_customer(r) for r in rows]

    # ---------------- Checkpoints ----------------

    def get_checkpoint(self, feed_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ezlynx_poller_checkpoints WHERE feed_name=?", (feed_name,)
            ).fetchone()
        return dict(row) if row else None

    def update_checkpoint(
        self,
        feed_name: str,
        *,
        cursor: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        error: str | None = None,
        synced_count: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO ezlynx_poller_checkpoints
                (feed_name, last_cursor, last_poll_started_at, last_poll_completed_at, last_error, total_synced)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(feed_name) DO UPDATE SET
                    last_cursor=COALESCE(excluded.last_cursor, ezlynx_poller_checkpoints.last_cursor),
                    last_poll_started_at=COALESCE(excluded.last_poll_started_at, ezlynx_poller_checkpoints.last_poll_started_at),
                    last_poll_completed_at=COALESCE(excluded.last_poll_completed_at, ezlynx_poller_checkpoints.last_poll_completed_at),
                    last_error=excluded.last_error,
                    total_synced=ezlynx_poller_checkpoints.total_synced + excluded.total_synced""",
                (feed_name, cursor, started_at, completed_at, error, synced_count),
            )

    @staticmethod
    def _decode_quote(row: sqlite3.Row) -> EzlynxQuote:
        return EzlynxQuote(
            id=row["id"],
            applicant_id=row["applicant_id"],
            carrier_id=row["carrier_id"],
            carrier_name=row["carrier_name"],
            line_of_business=row["line_of_business"],
            quote_number=row["quote_number"],
            premium=float(row["premium"]),
            effective_date=row["effective_date"],
            expiration_date=row["expiration_date"],
            status=row["status"],
            down_payment=float(row["down_payment"]) if row["down_payment"] is not None else None,
            terms=json.loads(row["terms_json"]),
            raw_data=json.loads(row["raw_data_json"]),
            job_id=row["job_id"],
            synced_at=row["synced_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _decode_customer(row: sqlite3.Row) -> EzlynxCustomerRecord:
        return EzlynxCustomerRecord(
            id=row["id"],
            applicant_id=row["applicant_id"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            company_name=row["company_name"],
            email=row["email"],
            phone=row["phone"],
            address_line1=row["address_line1"],
            city=row["city"],
            state=row["state"],
            zip_code=row["zip_code"],
            customer_type=row["customer_type"],
            custom_fields=json.loads(row["custom_fields_json"]),
            sync_status=row["sync_status"],
            last_polled_at=row["last_polled_at"],
            updated_at=row["updated_at"],
        )


class EzlynxPollerDaemon:
    """Background poller daemon for automated quote extraction and customer record syncs."""

    def __init__(
        self,
        db_path: str | Path,
        adapter: EzlynxApiAdapter | None = None,
        *,
        poll_interval_seconds: int = 60,
    ) -> None:
        self.db_path = str(db_path)
        self.store = EzlynxSyncStore(db_path)
        self.adapter = adapter or MockEzlynxAdapter()
        self.poll_interval = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_tick(self) -> dict[str, Any]:
        """Execute one poll cycle for quotes and customer records."""
        now = _now()
        result: dict[str, Any] = {
            "timestamp": now,
            "quotes_extracted": 0,
            "customers_synced": 0,
            "errors": [],
        }

        # 1. Sync Quotes
        quotes_cp = self.store.get_checkpoint("quotes")
        quote_cursor = quotes_cp.get("last_cursor") if quotes_cp else None
        try:
            self.store.update_checkpoint("quotes", started_at=now)
            raw_quotes, new_quote_cursor = self.adapter.fetch_quotes(since_cursor=quote_cursor)
            extracted_count = 0
            for q_data in raw_quotes:
                quote = EzlynxQuote(
                    id=q_data.get("id") or str(uuid.uuid4()),
                    applicant_id=str(q_data["applicant_id"]),
                    carrier_name=str(q_data["carrier_name"]),
                    line_of_business=str(q_data.get("line_of_business") or "commercial_auto"),
                    quote_number=q_data.get("quote_number"),
                    carrier_id=q_data.get("carrier_id"),
                    premium=float(q_data.get("premium", 0.0)),
                    effective_date=q_data.get("effective_date"),
                    expiration_date=q_data.get("expiration_date"),
                    status=q_data.get("status", "QUOTED"),
                    down_payment=float(q_data["down_payment"]) if q_data.get("down_payment") is not None else None,
                    terms=q_data.get("terms") or {},
                    raw_data=q_data,
                    job_id=q_data.get("job_id"),
                )
                self.store.upsert_quote(quote)
                extracted_count += 1

            self.store.update_checkpoint(
                "quotes",
                cursor=new_quote_cursor,
                completed_at=_now(),
                synced_count=extracted_count,
            )
            result["quotes_extracted"] = extracted_count
        except Exception as exc:
            err = f"Quote poll error: {type(exc).__name__}: {exc}"
            logger.error(err)
            self.store.update_checkpoint("quotes", error=err)
            result["errors"].append(err)

        # 2. Sync Customers
        cust_cp = self.store.get_checkpoint("customers")
        cust_cursor = cust_cp.get("last_cursor") if cust_cp else None
        try:
            self.store.update_checkpoint("customers", started_at=now)
            raw_customers, new_cust_cursor = self.adapter.fetch_customers(since_cursor=cust_cursor)
            synced_count = 0
            for c_data in raw_customers:
                cust = EzlynxCustomerRecord(
                    id=c_data.get("id") or str(uuid.uuid4()),
                    applicant_id=str(c_data["applicant_id"]),
                    first_name=c_data.get("first_name"),
                    last_name=c_data.get("last_name"),
                    company_name=c_data.get("company_name"),
                    email=c_data.get("email"),
                    phone=c_data.get("phone"),
                    address_line1=c_data.get("address_line1"),
                    city=c_data.get("city"),
                    state=c_data.get("state"),
                    zip_code=c_data.get("zip_code"),
                    customer_type=c_data.get("customer_type", "COMMERCIAL"),
                    custom_fields=c_data.get("custom_fields") or {},
                    sync_status="SYNCED",
                )
                self.store.upsert_customer(cust)
                synced_count += 1

            self.store.update_checkpoint(
                "customers",
                cursor=new_cust_cursor,
                completed_at=_now(),
                synced_count=synced_count,
            )
            result["customers_synced"] = synced_count
        except Exception as exc:
            err = f"Customer poll error: {type(exc).__name__}: {exc}"
            logger.error(err)
            self.store.update_checkpoint("customers", error=err)
            result["errors"].append(err)

        return result

    def start(self) -> None:
        """Start daemon background loop."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop daemon background loop gracefully."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_tick()
            except Exception as exc:
                logger.exception("Unexpected exception in daemon poll tick: %s", exc)
            self._stop_event.wait(self.poll_interval)
