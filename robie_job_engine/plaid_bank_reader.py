"""Opt-in Plaid transaction reader candidate; no enrollment or scheduling.

Default HTTPS transport is Sandbox ONLY. Production wiring, token exchange,
durable encrypted evidence/cursor storage and release require separate review.
Raw data stays in memory and must never be printed or committed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.request import Request, HTTPRedirectHandler, build_opener


class PlaidHold(RuntimeError):
    """Sanitized failure. Caller must retain its previous durable cursor."""


class PaginationChanged(PlaidHold):
    pass


READ_PATHS = frozenset({"/item/get", "/accounts/get", "/transactions/sync"})


@dataclass(frozen=True, repr=False)
class Credentials:
    client_id: str
    secret: str
    access_token: str

    def __repr__(self):
        return "Credentials(<redacted>)"


def load_sandbox_credentials(reference, accessor=None):
    """Pinned Test secret only; no latest aliases, env payloads or key files."""
    if not isinstance(reference, str) or not re.fullmatch(
        r"projects/[a-z][a-z0-9-]+/secrets/plaid-[a-z0-9-]+-test/versions/[1-9][0-9]*",
        reference,
    ):
        raise PlaidHold("NEEDS_AUTH: pinned Plaid Test secret reference required")
    try:
        from .gcp_secret_reader import _refuse_key_files
        from .secret_manager import GoogleSecretManagerAccessor
        _refuse_key_files()
        payload = json.loads((accessor or GoogleSecretManagerAccessor()).access(reference))
        if payload.get("environment") != "sandbox":
            raise ValueError()
        values = [payload.get(k) for k in ("client_id", "secret", "access_token")]
        if not all(isinstance(v, str) and v.strip() and "\x00" not in v for v in values):
            raise ValueError()
        if not values[2].startswith("access-sandbox-"):
            raise ValueError()
        return Credentials(*values)
    except Exception:
        raise PlaidHold("NEEDS_AUTH: Test secret unavailable or invalid") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SandboxTransport:
    """Fixed vendor HTTPS destination, no redirects, no automatic retries."""
    def __init__(self, credentials):
        self._credentials = credentials
        self._opener = build_opener(_NoRedirect())

    def __repr__(self):
        return "SandboxTransport(<redacted>)"

    def __call__(self, path, fields):
        if path not in READ_PATHS or set(fields) & {"client_id", "secret", "access_token"}:
            raise PlaidHold("UNVERIFIED: operation refused")
        c = self._credentials
        if not c.access_token.startswith("access-sandbox-"):
            raise PlaidHold("UNVERIFIED: production transport is not released")
        body = dict(fields, client_id=c.client_id, secret=c.secret, access_token=c.access_token)
        try:
            req = Request("https://sandbox.plaid.com" + path,
                          data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "Plaid-Version": "2020-09-14"},
                          method="POST")
            with self._opener.open(req, timeout=20) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except Exception as exc:
            # Only this documented code is inspected, never provider text/logs.
            try:
                error = json.loads(exc.read(65536)) if hasattr(exc, "read") else {}
            except Exception:
                error = {}
            if error.get("error_code") == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                raise PaginationChanged("UNVERIFIED: pagination changed") from None
            raise PlaidHold("UNVERIFIED: Plaid read failed") from None


@dataclass(frozen=True, repr=False)
class AccountScope:
    entity_id: str
    item_id: str
    institution_id: str
    account_ids: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.account_ids, tuple):
            raise PlaidHold("UNVERIFIED: exact reviewed account scope required")
        values = (self.entity_id, self.item_id, self.institution_id, *self.account_ids)
        if (not isinstance(self.account_ids, tuple) or not self.account_ids
                or not all(isinstance(v, str) and v.strip() for v in values)
                or len(set(self.account_ids)) != len(self.account_ids)):
            raise PlaidHold("UNVERIFIED: exact reviewed account scope required")

    def __repr__(self):
        return "AccountScope(<private>)"


@dataclass(frozen=True, repr=False)
class TransactionDelta:
    # Private payload: caller must atomically persist data + cursor in encrypted
    # evidence storage. Returning this object is not durable completion proof.
    next_cursor: str
    added: tuple[dict, ...]
    modified: tuple[dict, ...]
    removed: tuple[dict, ...]
    accounts: tuple[dict, ...]
    source_pages: tuple[dict, ...] = field(repr=False)
    source_updated_at: datetime
    bank_clearing_proven: bool = False
    durable_commit_proven: bool = False

    def __repr__(self):
        return (f"TransactionDelta(added={len(self.added)}, modified={len(self.modified)}, "
                f"removed={len(self.removed)}, bank_clearing_proven=False, durable_commit_proven=False)")


def _date(value):
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None or d.utcoffset() is None:
            raise ValueError()
        return d
    except Exception:
        raise PlaidHold("UNVERIFIED: source update timestamp missing or invalid") from None


def _item(data, scope):
    item = data.get("item", {})
    if (item.get("item_id") != scope.item_id
            or item.get("institution_id") != scope.institution_id
            or "error" not in item or item["error"] is not None):
        raise PlaidHold("UNVERIFIED: Item identity or authorization mismatch")


def _read(call, path, fields):
    try:
        return call(path, fields)
    except PaginationChanged:
        raise PaginationChanged("UNVERIFIED: pagination changed") from None
    except Exception:
        raise PlaidHold("UNVERIFIED: Plaid read failed") from None


def collect_transaction_delta(call, scope, *, cursor, now, max_pages=20,
                              max_age=timedelta(hours=24)):
    """Return one complete, scoped in-memory delta or hold. Never persist cursor.

    24h freshness is a candidate default, not an approved accounting SLA.
    Empty initial history stays unknown until HISTORICAL_UPDATE_COMPLETE.
    """
    if (not isinstance(scope, AccountScope) or not isinstance(now, datetime)
            or not isinstance(max_age, timedelta)
            or not isinstance(cursor, str) or cursor == "now" or len(cursor) > 256
            or now.tzinfo is None or now.utcoffset() is None
            or type(max_pages) is not int or not 1 <= max_pages <= 100
            or max_age <= timedelta(0)):
        raise PlaidHold("UNVERIFIED: invalid read bounds")
    try:
        return _collect(call, scope, cursor, now, max_pages, max_age)
    except PlaidHold:
        raise
    except Exception:
        raise PlaidHold("UNVERIFIED: malformed or unavailable source") from None


def _collect(call, scope, initial_cursor, now, max_pages, max_age):
    # Restart the ENTIRE delta once on mutation, retaining original cursor.
    for attempt in range(2):
        try:
            identity = _read(call, "/item/get", {})
            _item(identity, scope)
            expiration = identity["item"].get("consent_expiration_time")
            if expiration and _date(expiration) <= now:
                raise PlaidHold("UNVERIFIED: consent expired")
            updated = _date(identity.get("status", {}).get("transactions", {}).get("last_successful_update"))
            if updated > now or now - updated > max_age:
                raise PlaidHold("UNVERIFIED: source is stale or future dated")
            failed = identity.get("status", {}).get("transactions", {}).get("last_failed_update")
            if failed and _date(failed) >= updated:
                raise PlaidHold("UNVERIFIED: newer source failure")
            accounts_response = _read(call, "/accounts/get", {})
            _item(accounts_response, scope)
            accounts = accounts_response["accounts"]
            actual = [a["account_id"] for a in accounts]
            if len(set(actual)) != len(actual) or set(actual) != set(scope.account_ids):
                raise PlaidHold("UNVERIFIED: account membership changed")
            if any(a.get("type") not in {"credit", "depository"} for a in accounts):
                raise PlaidHold("UNVERIFIED: unsupported account type")
            pages, seen_cursors, seen_ids = [], {initial_cursor}, set()
            groups = {k: [] for k in ("added", "modified", "removed")}
            current = initial_cursor
            for _ in range(max_pages):
                page = _read(call, "/transactions/sync", {"cursor": current, "count": 500,
                            "options": {"include_original_description": True}})
                if page.get("transactions_update_status") != "HISTORICAL_UPDATE_COMPLETE":
                    raise PlaidHold("UNVERIFIED: historical source pull incomplete")
                if type(page.get("has_more")) is not bool:
                    raise PlaidHold("UNVERIFIED: pagination status missing")
                nxt = page.get("next_cursor")
                if not isinstance(nxt, str) or not nxt or len(nxt) > 256:
                    raise PlaidHold("UNVERIFIED: cursor invalid")
                for kind in groups:
                    if not isinstance(page.get(kind), list):
                        raise PlaidHold("UNVERIFIED: transaction group missing")
                    for row in page[kind]:
                        txid = row.get("transaction_id")
                        if (not isinstance(txid, str) or not txid or txid in seen_ids
                                or row.get("account_id") not in scope.account_ids):
                            raise PlaidHold("UNVERIFIED: duplicate or unscoped transaction")
                        if kind != "removed" and type(row.get("pending")) is not bool:
                            raise PlaidHold("UNVERIFIED: transaction pending status missing")
                        seen_ids.add(txid)
                        groups[kind].append(row)
                pages.append(page)
                if not page["has_more"]:
                    return TransactionDelta(nxt, *(tuple(groups[k]) for k in groups),
                                            tuple(accounts), tuple(pages), updated)
                if nxt in seen_cursors:
                    raise PlaidHold("UNVERIFIED: pagination loop")
                seen_cursors.add(nxt)
                current = nxt
            raise PlaidHold("UNVERIFIED: pagination limit exceeded")
        except PaginationChanged:
            if attempt:
                raise PlaidHold("UNVERIFIED: source keeps changing") from None
    raise PlaidHold("UNVERIFIED: no complete source")


def hosted_link_request(*, opaque_user_id, client_name, history_days=730):
    """Pure request preparation only. Does not contact Plaid or consume Items.

    Enrollment executor must check free-plan budget, existing/pending sessions,
    exact user/entity binding and later authorized institution/account readback.
    No SMS/email delivery, payments, or additional products requested.
    """
    if (not isinstance(opaque_user_id, str) or not opaque_user_id.strip()
            or not isinstance(client_name, str) or not client_name.strip()
            or type(history_days) is not int
            or not 30 <= history_days <= 730):
        raise PlaidHold("UNVERIFIED: Link request scope invalid")
    return {"user": {"client_user_id": opaque_user_id}, "client_name": client_name,
            "country_codes": ["US"], "language": "en", "products": ["transactions"],
            "transactions": {"days_requested": history_days}, "hosted_link": {}}
