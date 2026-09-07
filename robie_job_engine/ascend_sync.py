"""Ascend to EZLynx Lifecycle & Cancellation Event Synchronizer.

Polls Ascend API for account events:
- Cancellation Notices & Return Premiums (/v1/cancelation_returns)
- Past Due / Payment Failures (/v1/invoices)
- Financing & Loan Updates (/v1/loans)

Correlates events with EZLynx accounts and policies, applying:
1. Cancellation Notices:
   - Plain text Due Date / Cancellation Effective Date
   - Plain text Amount Due / Return Premium and tax breakdown
   - Embedded document link to cancellation notice PDF
   - Account label: 'Cancellation Notice'
   - High-priority EZLynx follow-up task assigned to CSR or Producer
   - Permanent EZLynx discussion card note ending in 'Robie was here'
2. Past Due / Payment Failures:
   - Plain text Amount Due and Due Date
   - Embedded invoice link
   - Permanent EZLynx discussion card note (no label applied)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request

from .ezlynx_note_poster import (
    EZLynxAgreementPoster,
    format_accounting_issue_task,
    format_agreement_signed_note,
    format_cancellation_notice_note,
    format_past_due_notice_note,
    format_reinstatement_carrier_email,
    format_reinstatement_paid_note,
)
from .quickbooks_api import QuickBooksApiClient
from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, current_robie_env
from .secret_manager import GoogleSecretManagerAccessor, SecretAccessor
from .secrets import redact_text

logger = logging.getLogger("robie.ascend_sync")

PRODUCTION_API_ORIGIN = "https://api.useascend.com"
SANDBOX_API_ORIGIN = "https://sandbox.api.useascend.com"
DEFAULT_DB_PATH = Path("/opt/streetsmart-hermes/robie-job-engine/data/ascend_sync.db")


def send_google_chat_alert(message: str, webhook_url: Optional[str] = None) -> bool:
    """Dispatches real-time message notification to Google Chat space."""
    target_url = webhook_url or os.environ.get("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    logger.info("GOOGLE CHAT ALERT: %s", message.replace("\n", " "))
    if not target_url:
        return False
    try:
        req = request.Request(
            target_url,
            data=json.dumps({"text": message}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=UTF-8"},
            method="POST",
        )
        with request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception as exc:
        logger.warning("Failed to send Google Chat webhook alert: %s", exc)
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AscendCancellationEvent:
    id: str
    policy_number: str
    carrier_name: str
    insured_name: str
    cancellation_effective_date: str
    due_date_text: str
    unearned_premium_cents: int = 0
    unearned_commission_cents: int = 0
    unearned_surplus_lines_tax_cents: int = 0
    wholesaler_name: Optional[str] = None
    coverage_title: Optional[str] = None
    document_url: Optional[str] = None
    document_title: Optional[str] = None
    producer_name: Optional[str] = None
    producer_email: Optional[str] = None
    account_manager_name: Optional[str] = None
    account_manager_email: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_return_cents(self) -> int:
        return self.unearned_premium_cents + self.unearned_surplus_lines_tax_cents

    @property
    def total_return_text(self) -> str:
        return f"${self.total_return_cents / 100:,.2f}"

    @property
    def unearned_premium_text(self) -> str:
        return f"${self.unearned_premium_cents / 100:,.2f}"

    @property
    def unearned_commission_text(self) -> str:
        return f"${self.unearned_commission_cents / 100:,.2f}"

    @property
    def unearned_tax_text(self) -> str:
        return f"${self.unearned_surplus_lines_tax_cents / 100:,.2f}"


@dataclass
class AscendPastDueEvent:
    id: str
    invoice_number: str
    insured_name: str
    amount_due_cents: int
    due_date_text: str
    policy_number: Optional[str] = None
    memo: Optional[str] = None
    invoice_url: Optional[str] = None
    payment_status: str = "past_due"
    producer_name: Optional[str] = None
    account_manager_name: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def amount_due_text(self) -> str:
        return f"${self.amount_due_cents / 100:,.2f}"


class AscendSyncStore:
    """Durable SQLite storage for tracking processed Ascend events and sync checkpoints."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ascend_synced_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    policy_number TEXT,
                    applicant_id TEXT,
                    amount_cents INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    synced_at TEXT NOT NULL,
                    raw_data_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_ascend_sync_policy ON ascend_synced_events(policy_number);
                CREATE INDEX IF NOT EXISTS idx_ascend_sync_applicant ON ascend_synced_events(applicant_id);

                CREATE TABLE IF NOT EXISTS ascend_sync_checkpoints (
                    feed_name TEXT PRIMARY KEY,
                    last_poll_started_at TEXT,
                    last_poll_completed_at TEXT,
                    last_error TEXT,
                    total_synced INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def is_event_processed(self, event_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM ascend_synced_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return row is not None

    def record_synced_event(
        self,
        event_id: str,
        event_type: str,
        *,
        policy_number: Optional[str] = None,
        applicant_id: Optional[str] = None,
        amount_cents: int = 0,
        status: str = "SUCCESS",
        error_message: Optional[str] = None,
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ascend_synced_events
                (event_id, event_type, policy_number, applicant_id, amount_cents, status, error_message, synced_at, raw_data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    status=excluded.status,
                    error_message=excluded.error_message,
                    synced_at=excluded.synced_at
                """,
                (
                    event_id,
                    event_type,
                    policy_number,
                    applicant_id,
                    amount_cents,
                    status,
                    error_message,
                    now,
                    json.dumps(raw_data or {}),
                ),
            )

    def update_checkpoint(
        self,
        feed_name: str,
        *,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        error: Optional[str] = None,
        synced_count: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ascend_sync_checkpoints
                (feed_name, last_poll_started_at, last_poll_completed_at, last_error, total_synced)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(feed_name) DO UPDATE SET
                    last_poll_started_at=COALESCE(excluded.last_poll_started_at, ascend_sync_checkpoints.last_poll_started_at),
                    last_poll_completed_at=COALESCE(excluded.last_poll_completed_at, ascend_sync_checkpoints.last_poll_completed_at),
                    last_error=excluded.last_error,
                    total_synced=ascend_sync_checkpoints.total_synced + excluded.total_synced
                """,
                (feed_name, started_at, completed_at, error, synced_count),
            )


class AscendApiClient:
    """Lightweight direct REST reader for Ascend API account event endpoints."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        origin: Optional[str] = None,
        secret_accessor: Optional[SecretAccessor] = None,
    ) -> None:
        self.secret_accessor = secret_accessor or GoogleSecretManagerAccessor()
        self.api_key = api_key or self._resolve_api_key()
        self.origin = origin or self._resolve_origin()
        self._billable_cache: Dict[str, Any] = {}
        self._program_cache: Dict[str, Any] = {}

    def _resolve_origin(self) -> str:
        env_name = current_robie_env()
        if env_name in PRODUCTION_ENV_NAMES:
            return PRODUCTION_API_ORIGIN
        return SANDBOX_API_ORIGIN

    def _resolve_api_key(self) -> str:
        env_key = os.environ.get("ASCEND_API_KEY", "").strip()
        if env_key:
            return env_key
        secret_ref = str(os.environ.get("ROBIE_ASCEND_API_KEY_SECRET") or "ascend-api-key").strip()
        if not secret_ref.startswith("projects/"):
            project = os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "streetsmart-hermes-poc"
            secret_ref = f"projects/{project}/secrets/{secret_ref}/versions/latest"
        try:
            return self.secret_accessor.access(secret_ref).strip()
        except Exception as e:
            logger.warning("Could not access Ascend secret from Secret Manager: %s", e)
            return ""

    def get(self, path: str, query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Perform authenticated GET request to Ascend API."""
        if not self.api_key:
            raise RuntimeError("Ascend API key not configured")
        
        url = f"{self.origin}{path}"
        if query:
            url += f"?{parse.urlencode(query)}"

        req = request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "Robie-Ascend-Sync/1.0",
            },
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=25) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ascend API GET {path} returned HTTP {exc.code}: {redact_text(body)}") from exc
        except Exception as exc:
            raise RuntimeError(f"Ascend API GET {path} failed: {redact_text(str(exc))}") from exc

    def fetch_cancelation_returns(self, page_size: int = 50) -> List[Dict[str, Any]]:
        """Fetch cancellation returns containing return premiums and cancellation docs."""
        data = self.get("/v1/cancelation_returns", {"page_size": page_size})
        return data.get("data", [])

    def fetch_billable(self, billable_id: str) -> Dict[str, Any]:
        """Fetch billable details (carrier, policy number, coverage, program ID)."""
        if billable_id not in self._billable_cache:
            self._billable_cache[billable_id] = self.get(f"/v1/billables/{billable_id}")
        return self._billable_cache[billable_id]

    def fetch_program(self, program_id: str) -> Dict[str, Any]:
        """Fetch program details (insured name, producer, account manager)."""
        if program_id not in self._program_cache:
            self._program_cache[program_id] = self.get(f"/v1/programs/{program_id}")
        return self._program_cache[program_id]

    def fetch_invoices(self, page_size: int = 50) -> List[Dict[str, Any]]:
        """Fetch invoices."""
        data = self.get("/v1/invoices", {"page_size": page_size})
        return data.get("data", [])

    def fetch_programs(self, page_size: int = 50) -> List[Dict[str, Any]]:
        """Fetch programs."""
        data = self.get("/v1/programs", {"page_size": page_size})
        return data.get("data", [])

    def fetch_loans(self, page_size: int = 50) -> List[Dict[str, Any]]:
        """Fetch loans."""
        data = self.get("/v1/loans", {"page_size": page_size})
        return data.get("data", [])

    def fetch_payouts(self, page_size: int = 50) -> List[Dict[str, Any]]:
        """Fetch payouts (supplier remittances and agency commissions)."""
        data = self.get("/v1/payouts", {"page_size": page_size})
        return data.get("data", [])


class EZLynxAccountMatcher:
    """Matches Ascend events to EZLynx applicant IDs and staff assignments."""

    def __init__(self) -> None:
        self._cached_client: Any = None

    def _get_ezlynx_client(self) -> Any:
        if self._cached_client is None:
            for extra_path in (
                "/opt/renewal-automation-system",
                "/opt/renewal-automation-system/venv/lib/python3.12/site-packages",
                "/opt/renewal-automation-system/venv/lib/python3.11/site-packages",
            ):
                if os.path.exists(extra_path) and extra_path not in sys.path:
                    sys.path.append(extra_path)
            try:
                from src.ezlynx.api_client import EZLynxApiClient
                self._cached_client = EZLynxApiClient()
            except ImportError:
                self._cached_client = None
        return self._cached_client

    def match_account(
        self,
        policy_number: Optional[str] = None,
        insured_name: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Finds (applicant_id, assigned_rep) in EZLynx.

        Matches by policy number first, then by insured name.
        """
        client = self._get_ezlynx_client()
        if not client:
            return None, None

        # 1. Match by policy number
        if policy_number:
            try:
                res = client.search_policy_by_number(policy_number)
                if res.get("status") == "success" and res.get("data"):
                    items = res["data"]
                    if isinstance(items, list) and items:
                        app_id = str(items[0].get("ApplicantId") or items[0].get("applicant_id") or "")
                        rep = items[0].get("AssignedUser") or items[0].get("ProducerName")
                        if app_id:
                            return app_id, rep
                    elif isinstance(items, dict):
                        app_id = str(items.get("ApplicantId") or items.get("applicant_id") or "")
                        rep = items.get("AssignedUser") or items.get("ProducerName")
                        if app_id:
                            return app_id, rep
            except Exception as e:
                logger.warning("EZLynx search_policy_by_number error: %s", e)

        # 2. Fall back to matching by insured name
        if insured_name:
            try:
                res = client.search_applicant(insured_name)
                if isinstance(res, dict) and res.get("id"):
                    app_id = str(res["id"])
                    rep = res.get("assigned_user") or res.get("producer")
                    return app_id, rep
            except Exception as e:
                logger.warning("EZLynx search_applicant error: %s", e)

        return None, None


class AscendEZLynxSyncManager:
    """Coordinates polling Ascend account events and synchronizing them to EZLynx."""

    def __init__(
        self,
        api_client: Optional[AscendApiClient] = None,
        store: Optional[AscendSyncStore] = None,
        matcher: Optional[EZLynxAccountMatcher] = None,
        poster: Optional[EZLynxAgreementPoster] = None,
        quickbooks_client: Optional[QuickBooksApiClient] = None,
    ) -> None:
        self.api = api_client or AscendApiClient()
        self.store = store or AscendSyncStore(DEFAULT_DB_PATH)
        self.matcher = matcher or EZLynxAccountMatcher()
        self.poster = poster or EZLynxAgreementPoster()
        self.qb = quickbooks_client or QuickBooksApiClient()

    def sync_once(self) -> Dict[str, Any]:
        """Perform one full synchronization run of all Ascend event feeds."""
        started_at = _now_iso()
        stats = {
            "started_at": started_at,
            "cancellations_found": 0,
            "cancellations_synced": 0,
            "past_due_found": 0,
            "past_due_synced": 0,
            "signed_agreements_found": 0,
            "signed_agreements_synced": 0,
            "reinstatements_found": 0,
            "reinstatements_synced": 0,
            "payouts_found": 0,
            "payouts_synced": 0,
            "errors": [],
        }

        # 1. Sync Cancellation Returns
        try:
            cancels_found, cancels_synced = self._sync_cancellations()
            stats["cancellations_found"] = cancels_found
            stats["cancellations_synced"] = cancels_synced
        except Exception as exc:
            logger.error("Cancellation sync error: %s", exc, exc_info=True)
            stats["errors"].append(f"Cancellations: {exc}")

        # 2. Sync Past Due Invoices
        try:
            pd_found, pd_synced = self._sync_past_due_invoices()
            stats["past_due_found"] = pd_found
            stats["past_due_synced"] = pd_synced
        except Exception as exc:
            logger.error("Past due sync error: %s", exc, exc_info=True)
            stats["errors"].append(f"Past Due: {exc}")

        # 3. Sync Signed Agreements & Ready-to-Bind Triggers
        try:
            signed_found, signed_synced = self._sync_signed_agreements()
            stats["signed_agreements_found"] = signed_found
            stats["signed_agreements_synced"] = signed_synced
        except Exception as exc:
            logger.error("Signed agreements sync error: %s", exc, exc_info=True)
            stats["errors"].append(f"Signed Agreements: {exc}")

        # 4. Sync Reinstatement Payments
        try:
            reinst_found, reinst_synced = self._sync_reinstatements()
            stats["reinstatements_found"] = reinst_found
            stats["reinstatements_synced"] = reinst_synced
        except Exception as exc:
            logger.error("Reinstatement sync error: %s", exc, exc_info=True)
            stats["errors"].append(f"Reinstatements: {exc}")

        # 5. Sync Accounting Payouts & Discrepancies
        try:
            payouts_found, payouts_synced = self._sync_accounting_payouts()
            stats["payouts_found"] = payouts_found
            stats["payouts_synced"] = payouts_synced
        except Exception as exc:
            logger.error("Accounting payouts sync error: %s", exc, exc_info=True)
            stats["errors"].append(f"Accounting Payouts: {exc}")

        completed_at = _now_iso()
        stats["completed_at"] = completed_at
        total_synced = (
            stats["cancellations_synced"]
            + stats["past_due_synced"]
            + stats["signed_agreements_synced"]
            + stats["reinstatements_synced"]
            + stats["payouts_synced"]
        )
        error_msg = "; ".join(stats["errors"]) if stats["errors"] else None

        self.store.update_checkpoint(
            "ascend_ezlynx_sync",
            started_at=started_at,
            completed_at=completed_at,
            error=error_msg,
            synced_count=total_synced,
        )

        logger.info(
            "Ascend sync completed. Cancels: %d/%d, Past Due: %d/%d, Signed: %d/%d, Reinst: %d/%d, Payouts: %d/%d, Errors: %s",
            stats["cancellations_synced"],
            stats["cancellations_found"],
            stats["past_due_synced"],
            stats["past_due_found"],
            stats["signed_agreements_synced"],
            stats["signed_agreements_found"],
            stats["reinstatements_synced"],
            stats["reinstatements_found"],
            stats["payouts_synced"],
            stats["payouts_found"],
            error_msg or "None",
        )
        return stats


    def _sync_cancellations(self) -> Tuple[int, int]:
        """Fetch and process new cancellation returns."""
        raw_items = self.api.fetch_cancelation_returns()
        found = len(raw_items)
        synced = 0

        for item in raw_items:
            event_id = item.get("id")
            if not event_id or self.store.is_event_processed(event_id):
                continue

            event = self._hydrate_cancellation_event(item)
            if not event:
                continue

            # Correlate with EZLynx
            applicant_id, rep_from_ezlynx = self.matcher.match_account(
                policy_number=event.policy_number,
                insured_name=event.insured_name,
            )

            assigned_rep = (
                rep_from_ezlynx
                or event.producer_name
                or event.account_manager_name
                or "Carlo Ferrara"
            )

            # Format Note Text with Plain Text Due Date & Return Amount
            note_text = format_cancellation_notice_note(
                insured_name=event.insured_name,
                policy_number=event.policy_number,
                carrier_name=event.carrier_name,
                wholesaler_name=event.wholesaler_name,
                coverage_title=event.coverage_title,
                cancellation_effective_date=event.cancellation_effective_date,
                due_date_text=event.due_date_text,
                amount_due_or_return_text=event.total_return_text,
                document_url=event.document_url,
                assigned_rep=assigned_rep,
                unearned_premium_text=event.unearned_premium_text,
                unearned_commission_text=event.unearned_commission_text,
                unearned_tax_text=event.unearned_tax_text,
            )

            if applicant_id:
                # 1. Post discussion note
                self.poster.post_custom_note(
                    applicant_id=applicant_id,
                    title=f"🚨 Cancellation Notice - {event.carrier_name} - Policy #{event.policy_number}",
                    note_text=note_text,
                    policy_number=event.policy_number,
                    line_of_business=event.coverage_title,
                    carrier_name=event.carrier_name,
                )

                # 2. Apply label 'Cancellation Notice'
                self.poster.apply_account_label(
                    applicant_id=applicant_id,
                    label="Cancellation Notice",
                    policy_number=event.policy_number,
                )

                # 3. Create high-priority task assigned to CSR or Producer
                task_title = f"🚨 CSR ACTION REQUIRED: Cancellation Notice - {event.policy_number} - {event.carrier_name} - Due: {event.due_date_text}"
                task_desc = (
                    f"Ascend cancellation notice received for {event.insured_name} (Policy #{event.policy_number}).\n"
                    f"Assigned CSR / Producer: {assigned_rep}\n"
                    f"Cancellation Effective Date: {event.cancellation_effective_date}\n"
                    f"Plain Text Due Date / Effective Date: {event.due_date_text}\n"
                    f"Return Pure Premium: {event.unearned_premium_text}\n"
                    f"Embedded Document: {event.document_url or 'N/A'}\n"
                    f"Label 'Cancellation Notice' applied. Please review and verify policy status."
                )
                self.poster.create_task(
                    applicant_id=applicant_id,
                    title=task_title,
                    description=task_desc,
                    assigned_user=assigned_rep,
                    due_days_out=0,
                )
            else:
                logger.info(
                    "Policy %s (%s) not found in EZLynx. Event recorded as UNMATCHED.",
                    event.policy_number,
                    event.insured_name,
                )

            # 4. Record event checkpoint
            self.store.record_synced_event(
                event_id=event_id,
                event_type="cancellation",
                policy_number=event.policy_number,
                applicant_id=applicant_id,
                amount_cents=event.total_return_cents,
                status="SUCCESS" if applicant_id else "UNMATCHED",
                raw_data=item,
            )
            synced += 1

        return found, synced

    def _sync_past_due_invoices(self) -> Tuple[int, int]:
        """Fetch and process past due or failed invoices."""
        raw_items = self.api.fetch_invoices()
        found = 0
        synced = 0

        for item in raw_items:
            status = str(item.get("status", "")).lower()
            if status not in ("past_due", "failed"):
                continue

            found += 1
            event_id = item.get("id")
            if not event_id or self.store.is_event_processed(event_id):
                continue

            event = self._hydrate_past_due_event(item)
            if not event:
                continue

            # Correlate with EZLynx
            applicant_id, _ = self.matcher.match_account(
                policy_number=event.policy_number,
                insured_name=event.insured_name,
            )

            # Format Note Text with Plain Text Amount Due and Due Date (No Label Needed)
            note_text = format_past_due_notice_note(
                insured_name=event.insured_name,
                policy_number=event.policy_number,
                invoice_number=event.invoice_number,
                memo=event.memo,
                amount_due_text=event.amount_due_text,
                due_date_text=event.due_date_text,
                payment_status=event.payment_status,
                invoice_url=event.invoice_url,
            )

            if applicant_id:
                # Post note to EZLynx discussions
                self.poster.post_custom_note(
                    applicant_id=applicant_id,
                    title=f"⚠️ Past Due Payment Alert - Ascend Invoice #{event.invoice_number}",
                    note_text=note_text,
                    policy_number=event.policy_number,
                )
            else:
                logger.info(
                    "Past due invoice %s for %s (Policy %s) not found in EZLynx. Event recorded as UNMATCHED.",
                    event.invoice_number,
                    event.insured_name,
                    event.policy_number,
                )

            # Record event checkpoint
            self.store.record_synced_event(
                event_id=event_id,
                event_type="past_due",
                policy_number=event.policy_number,
                applicant_id=applicant_id,
                amount_cents=event.amount_due_cents,
                status="SUCCESS" if applicant_id else "UNMATCHED",
                raw_data=item,
            )
            synced += 1

        return found, synced

    def _is_policy_cancelled(self, policy_number: str) -> bool:
        if not policy_number:
            return False
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM ascend_synced_events WHERE event_type = 'cancellation' AND policy_number = ?",
                (policy_number,)
            ).fetchone()
            return row is not None

    def _sync_signed_agreements(self) -> Tuple[int, int]:
        """Fetch and process signed financing and checkout agreements."""
        programs = self.api.fetch_programs(page_size=50)
        found = 0
        synced = 0

        for prog in programs:
            status = str(prog.get("status", "")).lower()
            if status not in ("checked_out", "purchased"):
                continue

            found += 1
            prog_id = prog.get("id")
            event_id = f"signed_{prog_id}"
            if not prog_id or self.store.is_event_processed(event_id):
                continue

            billables = prog.get("billables") or []
            if not billables:
                try:
                    b_resp = self.api.get(f"/v1/programs/{prog_id}/billables")
                    billables = b_resp.get("data") or []
                except Exception:
                    billables = []

            first_b = billables[0] if billables else {}
            carrier_info = first_b.get("carrier") or {}
            wholesaler_info = first_b.get("wholesaler") or {}
            coverage_info = first_b.get("coverage_type") or {}
            policy_num = first_b.get("policy_number") or first_b.get("billable_identifier") or "Pending"
            carrier_name = carrier_info.get("title") or carrier_info.get("identifier") or "Carrier"
            wholesaler_name = wholesaler_info.get("title") or None
            coverage_title = coverage_info.get("title") or coverage_info.get("identifier") or "Commercial"

            insured = prog.get("insured") or {}
            insured_name = (
                insured.get("business_name")
                or f"{insured.get('first_name', '')} {insured.get('last_name', '')}".strip()
                or "Valued Client"
            )

            payment_option = prog.get("selected_payment_option_type") or "pay_in_full"
            checkedout_at = prog.get("checkedout_at") or _now_iso()
            downpayment_cents = int(prog.get("downpayment_amount_cents") or prog.get("total_payable_amount_cents") or 0)
            total_cents = int(prog.get("total_payable_amount_cents") or downpayment_cents)

            downpayment_text = f"${downpayment_cents / 100:,.2f}"
            total_text = f"${total_cents / 100:,.2f}"

            prod = prog.get("producer") or {}
            producer_name = f"{prod.get('first_name', '')} {prod.get('last_name', '')}".strip() or None

            prog_url = prog.get("program_url") or f"https://checkout.useascend.com/streetsmart_insurance_agency/overview?program_id={prog_id}"

            applicant_id, rep_from_ezlynx = self.matcher.match_account(
                policy_number=policy_num,
                insured_name=insured_name,
            )
            assigned_rep = rep_from_ezlynx or producer_name or "Account Manager"

            note_text = format_agreement_signed_note(
                insured_name=insured_name,
                policy_number=policy_num,
                carrier_name=carrier_name,
                wholesaler_name=wholesaler_name,
                coverage_title=coverage_title,
                payment_option=payment_option,
                downpayment_text=downpayment_text,
                total_text=total_text,
                checkedout_at=checkedout_at,
                program_url=prog_url,
                producer_name=assigned_rep,
            )

            if applicant_id:
                self.poster.post_custom_note(
                    applicant_id=applicant_id,
                    title=f"🎉 Agreement Signed & Checked Out - {carrier_name} - {policy_num}",
                    note_text=note_text,
                    policy_number=policy_num,
                    line_of_business=coverage_title,
                    carrier_name=carrier_name,
                )

                task_title = f"🚨 READY TO BIND: {policy_num} - {carrier_name} - {insured_name} (Agreement Signed)"
                task_desc = (
                    f"Insured {insured_name} completed Ascend checkout for Policy #{policy_num}.\n"
                    f"Plan: {payment_option.replace('_', ' ').title()}\n"
                    f"Initial Payment / Down Payment: {downpayment_text}\n"
                    f"Total: {total_text}\n"
                    f"Overview: {prog_url}\n\n"
                    f"Coverage is ready to bind with carrier {carrier_name}!"
                )
                self.poster.create_task(
                    applicant_id=applicant_id,
                    title=task_title,
                    description=task_desc,
                    assigned_user=assigned_rep,
                    due_days_out=0,
                )

            chat_msg = (
                f"🎉 *Agreement Signed & Checked Out! Ready to Bind:*\n"
                f"• Insured: *{insured_name}*\n"
                f"• Policy: `{policy_num}` | Carrier: *{carrier_name}*{f' (via {wholesaler_name})' if wholesaler_name else ''}\n"
                f"• Plan: {payment_option.replace('_', ' ').title()} (Down Payment: {downpayment_text}, Total: {total_text})\n"
                f"• Ascend Link: {prog_url}"
            )
            send_google_chat_alert(chat_msg)

            self.store.record_synced_event(
                event_id=event_id,
                event_type="agreement_signed",
                policy_number=policy_num,
                applicant_id=applicant_id,
                amount_cents=total_cents,
                status="SUCCESS" if applicant_id else "UNMATCHED",
                raw_data=prog,
            )
            synced += 1

        return found, synced

    def _sync_reinstatements(self) -> Tuple[int, int]:
        """Fetch and process reinstatement payments against cancelled or past-due policies."""
        invoices = self.api.fetch_invoices(page_size=50)
        found = 0
        synced = 0

        for inv in invoices:
            status = str(inv.get("status", "")).lower()
            is_reinstatement = bool(inv.get("is_reinstatement", False))

            memo = inv.get("memo") or ""
            policy_num = None
            if memo:
                parts = memo.split()
                if parts and parts[0].isalnum():
                    policy_num = parts[0]

            if not is_reinstatement and status in ("paid", "processing_payment") and policy_num:
                if self._is_policy_cancelled(policy_num):
                    is_reinstatement = True

            if not (is_reinstatement and status in ("paid", "processing_payment")):
                continue

            found += 1
            inv_id = inv.get("id")
            event_id = f"reinstatement_{inv_id}"
            if not inv_id or self.store.is_event_processed(event_id):
                continue

            insured_name = inv.get("payer_name") or inv.get("payee") or "Insured"
            amount_cents = int(inv.get("total_amount_cents") or 0)
            amount_paid_text = f"${amount_cents / 100:,.2f}"
            paid_at = inv.get("paid_at") or inv.get("updated_at") or _now_iso()
            invoice_num = inv.get("invoice_number") or inv_id[:8]
            receipt_url = inv.get("invoice_url") or inv.get("receipt_url")
            carrier_name = inv.get("carrier_name") or "Carrier"

            applicant_id, rep_from_ezlynx = self.matcher.match_account(
                policy_number=policy_num,
                insured_name=insured_name,
            )
            assigned_rep = rep_from_ezlynx or "Account Manager"

            note_text = format_reinstatement_paid_note(
                insured_name=insured_name,
                policy_number=policy_num or "Pending",
                carrier_name=carrier_name,
                amount_paid_text=amount_paid_text,
                paid_at=paid_at,
                invoice_number=invoice_num,
                receipt_url=receipt_url,
            )

            email_draft = format_reinstatement_carrier_email(
                carrier_or_wholesaler_name=carrier_name,
                policy_number=policy_num or "Pending",
                insured_name=insured_name,
                amount_paid_text=amount_paid_text,
                paid_at=paid_at,
                receipt_url=receipt_url,
            )

            if applicant_id:
                self.poster.post_custom_note(
                    applicant_id=applicant_id,
                    title=f"✅ Reinstatement Paid - {policy_num} - {amount_paid_text}",
                    note_text=note_text,
                    policy_number=policy_num,
                )

                task_title = f"🚨 REINSTATEMENT PAID: Request Carrier Reinstatement - {policy_num} - {insured_name}"
                task_desc = (
                    f"Insured {insured_name} paid outstanding balance of {amount_paid_text} on {paid_at}.\n"
                    f"Receipt: {receipt_url or 'N/A'}\n\n"
                    f"DRAFT CARRIER EMAIL:\n"
                    f"Subject: {email_draft['subject']}\n\n"
                    f"{email_draft['body']}"
                )
                self.poster.create_task(
                    applicant_id=applicant_id,
                    title=task_title,
                    description=task_desc,
                    assigned_user=assigned_rep,
                    due_days_out=0,
                )

            chat_msg = (
                f"🚨 *REINSTATEMENT PAYMENT RECEIVED!*\n"
                f"• Insured: *{insured_name}*\n"
                f"• Policy: `{policy_num or 'N/A'}` | Amount Paid: *{amount_paid_text}*\n"
                f"• Paid At: {paid_at}\n"
                f"• Action: Please submit reinstatement request to *{carrier_name}* with payment receipt attached.\n"
                f"• Receipt: {receipt_url or 'N/A'}"
            )
            send_google_chat_alert(chat_msg)

            self.store.record_synced_event(
                event_id=event_id,
                event_type="reinstatement_paid",
                policy_number=policy_num,
                applicant_id=applicant_id,
                amount_cents=amount_cents,
                status="SUCCESS" if applicant_id else "UNMATCHED",
                raw_data=inv,
            )
            synced += 1

        return found, synced

    def _sync_accounting_payouts(self) -> Tuple[int, int]:
        """Audit Ascend payouts, sync to QuickBooks, and assign tasks to Accounting on issues."""
        payouts = self.api.fetch_payouts(page_size=50)
        found = len(payouts)
        synced = 0
        accounting_assignee = os.environ.get("ROBIE_ACCOUNTING_ASSIGNEE", "Markley1")

        for p in payouts:
            payout_id = p.get("id")
            if not payout_id:
                continue

            ptype = p.get("payout_type")
            status = str(p.get("status", "")).lower()
            net_cents = int(p.get("net_payout_amount_cents") or 0)
            prog_id = p.get("program_id") or ""
            payable_acc = p.get("payable_account") or {}
            owner_name = payable_acc.get("owner_name") or "Recipient"

            if ptype == "commission" and status == "paid":
                event_id = f"comm_payout_{payout_id}"
                if not self.store.is_event_processed(event_id):
                    policy_num = "COMMISSION"
                    insured_name = "Agency Commission"
                    if prog_id:
                        try:
                            prog = self.api.fetch_program(prog_id)
                            insured = prog.get("insured") or {}
                            insured_name = insured.get("business_name") or insured.get("first_name") or insured_name
                            b_list = prog.get("billables") or []
                            if b_list:
                                policy_num = b_list[0].get("policy_number") or policy_num
                        except Exception:
                            pass

                    qb_res = self.qb.record_commission_deposit(
                        program_id=prog_id,
                        policy_number=policy_num,
                        insured_name=insured_name,
                        amount_cents=net_cents,
                        deposit_date=p.get("paid_at")[:10] if p.get("paid_at") else None,
                        payout_id=payout_id,
                    )

                    self.store.record_synced_event(
                        event_id=event_id,
                        event_type="commission_payout",
                        policy_number=policy_num,
                        amount_cents=net_cents,
                        status="SUCCESS",
                        raw_data={"payout": p, "quickbooks": qb_res},
                    )
                    synced += 1

            elif ptype == "supplier" and status == "paid":
                event_id = f"supp_payout_{payout_id}"
                if not self.store.is_event_processed(event_id):
                    qb_res = self.qb.record_supplier_payout_bill(
                        program_id=prog_id,
                        policy_number="WHOLESALER",
                        wholesaler_name=owner_name,
                        net_amount_cents=net_cents,
                        payment_date=p.get("paid_at")[:10] if p.get("paid_at") else None,
                        payout_id=payout_id,
                    )
                    self.store.record_synced_event(
                        event_id=event_id,
                        event_type="supplier_payout",
                        amount_cents=net_cents,
                        status="SUCCESS",
                        raw_data={"payout": p, "quickbooks": qb_res},
                    )
                    synced += 1

            elif ptype == "supplier" and (status == "failed" or status == "unpaid"):
                event_id = f"acct_issue_{payout_id}_{status}"
                if not self.store.is_event_processed(event_id):
                    amount_text = f"${net_cents / 100:,.2f}"
                    paying_at = p.get("paying_at") or "Unscheduled"
                    
                    details = (
                        f"Ascend supplier remittance of {amount_text} to wholesaler '{owner_name}' is currently {status.upper()}.\n"
                        f"Payout ID: {payout_id}\n"
                        f"Scheduled / Paying At: {paying_at}\n"
                        f"Action: Verify bank balance and Ascend supplier account settings to prevent carrier cancellation."
                    )
                    
                    task_text = format_accounting_issue_task(
                        issue_type=f"Unpaid Wholesaler Remittance ({status.upper()})",
                        wholesaler_name=owner_name,
                        expected_amount_text=amount_text,
                        actual_amount_text="$0.00",
                        discrepancy_details=details,
                        ascend_reference_url=f"https://app.useascend.com/payouts/{payout_id}",
                    )
                    # Discrepancy escalation:
                    # High priority EZLynx Task assigned to Accounting (no Google Chat alert per user directive)
                    self.poster.create_task(
                        applicant_id="0",
                        title=f"⚠️ ACCOUNTING AUDIT: {status.upper()} Supplier Payout to {owner_name} ({amount_text})",
                        description=task_text,
                        assigned_user=accounting_assignee,
                        due_days_out=1,
                    )
                    logger.info(
                        "Dispatched Accounting audit task in EZLynx for %s supplier payout %s to %s",
                        status.upper(),
                        payout_id,
                        owner_name,
                    )

                    self.store.record_synced_event(
                        event_id=event_id,
                        event_type="accounting_issue",
                        amount_cents=net_cents,
                        status="FLAGGED",
                        error_message=f"Supplier payout {payout_id} is {status}",
                        raw_data=p,
                    )
                    synced += 1

        return found, synced


    def _hydrate_cancellation_event(self, item: Dict[str, Any]) -> Optional[AscendCancellationEvent]:
        billable_summary = item.get("billable") or {}
        billable_id = billable_summary.get("id")
        if not billable_id:
            return None

        # Fetch billable to get policy number, carrier, and program ID
        try:
            billable = self.api.fetch_billable(billable_id)
        except Exception as e:
            logger.warning("Could not fetch billable %s: %s", billable_id, e)
            return None

        carrier_info = billable.get("carrier") or {}
        wholesaler_info = billable.get("wholesaler") or {}
        coverage_info = billable.get("coverage_type") or {}
        policy_num = billable.get("policy_number") or billable.get("billable_identifier") or "Pending"
        carrier_name = carrier_info.get("title") or carrier_info.get("identifier") or "Unknown Carrier"
        wholesaler_name = wholesaler_info.get("title") or None
        coverage_title = coverage_info.get("title") or coverage_info.get("identifier") or "Commercial"

        # Fetch program to get insured name, producer, and account manager
        program_id = billable.get("program_id")
        insured_name = "Unknown Insured"
        producer_name = None
        producer_email = None
        am_name = None
        am_email = None

        if program_id:
            try:
                prog = self.api.fetch_program(program_id)
                insured = prog.get("insured") or {}
                insured_name = (
                    insured.get("business_name")
                    or f"{insured.get('first_name', '')} {insured.get('last_name', '')}".strip()
                    or "Unknown Insured"
                )
                prod = prog.get("producer") or {}
                producer_name = f"{prod.get('first_name', '')} {prod.get('last_name', '')}".strip() or None
                producer_email = prod.get("email")
                am = prog.get("account_manager") or {}
                am_name = f"{am.get('first_name', '')} {am.get('last_name', '')}".strip() or None
                am_email = am.get("email")
            except Exception as e:
                logger.warning("Could not fetch program %s: %s", program_id, e)

        eff_date = billable_summary.get("cancelation_effective_date") or billable.get("effective_date") or _now_iso()[:10]
        
        # Document URL and title
        docs = item.get("cancelation_docs") or []
        doc_url = docs[0].get("url") if docs else None
        doc_title = docs[0].get("title") if docs else None

        return AscendCancellationEvent(
            id=item["id"],
            policy_number=policy_num,
            carrier_name=carrier_name,
            wholesaler_name=wholesaler_name,
            coverage_title=coverage_title,
            insured_name=insured_name,
            cancellation_effective_date=eff_date,
            due_date_text=eff_date,
            unearned_premium_cents=int(item.get("unearned_premium_cents") or 0),
            unearned_commission_cents=int(item.get("unearned_commission_cents") or 0),
            unearned_surplus_lines_tax_cents=int(item.get("unearned_surplus_lines_tax_cents") or 0),
            document_url=doc_url,
            document_title=doc_title,
            producer_name=producer_name,
            producer_email=producer_email,
            account_manager_name=am_name,
            account_manager_email=am_email,
            raw_data=item,
        )

    def _hydrate_past_due_event(self, item: Dict[str, Any]) -> Optional[AscendPastDueEvent]:
        insured_name = item.get("payer_name") or item.get("payee") or "Unknown Insured"
        invoice_num = item.get("invoice_number") or item.get("id") or "INV"
        due_date = item.get("due_date") or _now_iso()[:10]
        memo = item.get("memo") or ""
        amount = int(item.get("total_amount_cents") or 0)
        invoice_url = item.get("invoice_url")
        status = item.get("status", "past_due")

        # Extract policy number from memo if present (e.g. '33470152 (Excess Umbrella)')
        policy_num = None
        if memo:
            parts = memo.split()
            if parts and parts[0].isalnum():
                policy_num = parts[0]

        return AscendPastDueEvent(
            id=item["id"],
            invoice_number=invoice_num,
            insured_name=insured_name,
            amount_due_cents=amount,
            due_date_text=due_date,
            policy_number=policy_num,
            memo=memo,
            invoice_url=invoice_url,
            payment_status=status,
            raw_data=item,
        )


def run_daemon(interval_seconds: int = 3600, db_path: Optional[str] = None) -> None:
    """Run synchronization daemon on a recurring interval (default 1 hour)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logger.info("Starting Ascend to EZLynx Sync Daemon (Interval: %ds)", interval_seconds)

    store = AscendSyncStore(db_path or DEFAULT_DB_PATH)
    manager = AscendEZLynxSyncManager(store=store)

    while True:
        try:
            logger.info("Executing scheduled Ascend-EZLynx sync cycle...")
            manager.sync_once()
        except Exception as exc:
            logger.error("Daemon cycle error: %s", exc, exc_info=True)

        logger.info("Sleeping for %d seconds...", interval_seconds)
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ascend to EZLynx Account Event Synchronizer")
    parser.add_argument("--once", action="store_true", help="Run sync once and exit")
    parser.add_argument("--daemon", action="store_true", help="Run in continuous background loop")
    parser.add_argument("--interval", type=int, default=3600, help="Sync interval in seconds (default: 3600 / 1 hour)")
    parser.add_argument("--db-path", type=str, default=str(DEFAULT_DB_PATH), help="Path to sqlite sync database")
    args = parser.parse_args()

    if args.daemon:
        run_daemon(interval_seconds=args.interval, db_path=args.db_path)
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        store = AscendSyncStore(args.db_path)
        manager = AscendEZLynxSyncManager(store=store)
        res = manager.sync_once()
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
