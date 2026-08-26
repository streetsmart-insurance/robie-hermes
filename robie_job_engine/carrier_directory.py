from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


UTC = timezone.utc


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(val: Any) -> str:
    return json.dumps(val, separators=(",", ":"), sort_keys=True)


@dataclass
class Carrier:
    id: str
    name: str
    slug: str
    naic_code: str | None = None
    am_best_rating: str | None = None
    website_url: str | None = None
    status: str = "ACTIVE"  # ACTIVE, INACTIVE, MAINTENANCE, DEPRECATED
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CarrierEndpoint:
    id: str
    carrier_id: str
    environment: str  # production, staging, uat, test
    endpoint_type: str  # login, quote, submission, document_upload, status_check, api_base
    url: str
    http_method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 60
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CarrierCredential:
    id: str
    carrier_id: str
    environment: str
    username: str
    secret_ref: str  # Reference to vault / secret manager or encrypted payload
    agency_code: str | None = None
    producer_code: str | None = None
    account_number: str | None = None
    auth_type: str = "credentials"  # credentials, oauth2, api_key, session_cookie, sso
    extra_fields: dict[str, Any] = field(default_factory=dict)
    status: str = "ACTIVE"  # ACTIVE, EXPIRED, LOCKED, REVOKED
    last_rotated_at: str | None = None
    expires_at: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CarrierLobRule:
    id: str
    carrier_id: str
    line_of_business: str  # commercial_auto, general_liability, workers_comp, bop, property, etc.
    state_eligibility: list[str] = field(default_factory=list)  # 2-letter state codes e.g. ["CA", "TX"]
    appetite_rules: dict[str, Any] = field(default_factory=dict)
    required_fields: list[str] = field(default_factory=list)
    submission_mode: str = "portal_automation"  # portal_automation, api, email, ezlynx_integration
    quote_auto_approval: bool = False
    max_limit_amount: float | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CarrierAuthRequirement:
    id: str
    carrier_id: str
    mfa_type: str = "none"  # none, totp, sms, email, push_notification, duo, okta
    mfa_secret_ref: str | None = None
    session_timeout_minutes: int = 30
    password_rotation_days: int = 90
    captcha_type: str = "none"  # none, recaptcha_v2, recaptcha_v3, turnstile, hcaptcha
    ip_allowlist: list[str] = field(default_factory=list)
    headers_template: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CarrierDirectoryStore:
    """Manages carrier directory schema, endpoints, portal credentials, LOB rules, and auth requirements."""

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
                CREATE TABLE IF NOT EXISTS carriers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    naic_code TEXT,
                    am_best_rating TEXT,
                    website_url TEXT,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_carriers_slug ON carriers(slug);
                CREATE INDEX IF NOT EXISTS idx_carriers_status ON carriers(status);

                CREATE TABLE IF NOT EXISTS carrier_endpoints (
                    id TEXT PRIMARY KEY,
                    carrier_id TEXT NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
                    environment TEXT NOT NULL DEFAULT 'production',
                    endpoint_type TEXT NOT NULL,
                    url TEXT NOT NULL,
                    http_method TEXT NOT NULL DEFAULT 'GET',
                    headers_json TEXT NOT NULL DEFAULT '{}',
                    timeout_seconds INTEGER NOT NULL DEFAULT 60,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(carrier_id, environment, endpoint_type)
                );
                CREATE INDEX IF NOT EXISTS idx_carrier_endpoints_lookup
                    ON carrier_endpoints(carrier_id, environment, endpoint_type);

                CREATE TABLE IF NOT EXISTS carrier_credentials (
                    id TEXT PRIMARY KEY,
                    carrier_id TEXT NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
                    environment TEXT NOT NULL DEFAULT 'production',
                    username TEXT NOT NULL,
                    secret_ref TEXT NOT NULL,
                    agency_code TEXT,
                    producer_code TEXT,
                    account_number TEXT,
                    auth_type TEXT NOT NULL DEFAULT 'credentials',
                    extra_fields_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    last_rotated_at TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(carrier_id, environment, username)
                );
                CREATE INDEX IF NOT EXISTS idx_carrier_credentials_carrier
                    ON carrier_credentials(carrier_id, environment);

                CREATE TABLE IF NOT EXISTS carrier_lob_rules (
                    id TEXT PRIMARY KEY,
                    carrier_id TEXT NOT NULL REFERENCES carriers(id) ON DELETE CASCADE,
                    line_of_business TEXT NOT NULL,
                    state_eligibility_json TEXT NOT NULL DEFAULT '[]',
                    appetite_rules_json TEXT NOT NULL DEFAULT '{}',
                    required_fields_json TEXT NOT NULL DEFAULT '[]',
                    submission_mode TEXT NOT NULL DEFAULT 'portal_automation',
                    quote_auto_approval INTEGER NOT NULL DEFAULT 0,
                    max_limit_amount REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(carrier_id, line_of_business)
                );
                CREATE INDEX IF NOT EXISTS idx_carrier_lob_rules_lookup
                    ON carrier_lob_rules(carrier_id, line_of_business);

                CREATE TABLE IF NOT EXISTS carrier_auth_requirements (
                    id TEXT PRIMARY KEY,
                    carrier_id TEXT NOT NULL REFERENCES carriers(id) ON DELETE CASCADE UNIQUE,
                    mfa_type TEXT NOT NULL DEFAULT 'none',
                    mfa_secret_ref TEXT,
                    session_timeout_minutes INTEGER NOT NULL DEFAULT 30,
                    password_rotation_days INTEGER NOT NULL DEFAULT 90,
                    captcha_type TEXT NOT NULL DEFAULT 'none',
                    ip_allowlist_json TEXT NOT NULL DEFAULT '[]',
                    headers_template_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    # ---------------- Carrier CRUD ----------------

    def add_carrier(
        self,
        name: str,
        slug: str | None = None,
        *,
        naic_code: str | None = None,
        am_best_rating: str | None = None,
        website_url: str | None = None,
        status: str = "ACTIVE",
        carrier_id: str | None = None,
    ) -> Carrier:
        carrier_id = carrier_id or str(uuid.uuid4())
        clean_slug = (slug or name.lower().replace(" ", "_")).strip()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO carriers
                (id, name, slug, naic_code, am_best_rating, website_url, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (carrier_id, name.strip(), clean_slug, naic_code, am_best_rating, website_url, status, now, now),
            )
        return self.get_carrier(carrier_id)  # type: ignore[return-value]

    def get_carrier(self, identifier: str) -> Carrier | None:
        """Find carrier by ID or slug."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM carriers WHERE id=? OR slug=?", (identifier, identifier)
            ).fetchone()
        if not row:
            return None
        return Carrier(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            naic_code=row["naic_code"],
            am_best_rating=row["am_best_rating"],
            website_url=row["website_url"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_carriers(self, status: str | None = None) -> list[Carrier]:
        query = "SELECT * FROM carriers"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY name ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            Carrier(
                id=r["id"],
                name=r["name"],
                slug=r["slug"],
                naic_code=r["naic_code"],
                am_best_rating=r["am_best_rating"],
                website_url=r["website_url"],
                status=r["status"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def update_carrier(self, carrier_id: str, **fields: Any) -> Carrier:
        allowed = {"name", "slug", "naic_code", "am_best_rating", "website_url", "status"}
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Invalid carrier fields: {invalid}")
        if not fields:
            c = self.get_carrier(carrier_id)
            if not c:
                raise KeyError(f"Carrier not found: {carrier_id}")
            return c
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [carrier_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE carriers SET {assignments} WHERE id=?", params)
        c = self.get_carrier(carrier_id)
        if not c:
            raise KeyError(f"Carrier not found: {carrier_id}")
        return c

    # ---------------- Endpoints ----------------

    def set_endpoint(
        self,
        carrier_id: str,
        endpoint_type: str,
        url: str,
        *,
        environment: str = "production",
        http_method: str = "GET",
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> CarrierEndpoint:
        carrier = self.get_carrier(carrier_id)
        if not carrier:
            raise KeyError(f"Carrier not found: {carrier_id}")
        headers_dict = headers or {}
        now = _now()
        endpoint_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO carrier_endpoints
                (id, carrier_id, environment, endpoint_type, url, http_method, headers_json, timeout_seconds, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(carrier_id, environment, endpoint_type) DO UPDATE SET
                    url=excluded.url,
                    http_method=excluded.http_method,
                    headers_json=excluded.headers_json,
                    timeout_seconds=excluded.timeout_seconds,
                    updated_at=excluded.updated_at""",
                (endpoint_id, carrier.id, environment, endpoint_type, url, http_method, _json(headers_dict), timeout_seconds, now, now),
            )
        endpoint = self.get_endpoint(carrier.id, endpoint_type, environment=environment)
        assert endpoint is not None
        return endpoint

    def get_endpoint(
        self, carrier_id: str, endpoint_type: str, environment: str = "production"
    ) -> CarrierEndpoint | None:
        carrier = self.get_carrier(carrier_id)
        cid = carrier.id if carrier else carrier_id
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM carrier_endpoints WHERE carrier_id=? AND endpoint_type=? AND environment=?",
                (cid, endpoint_type, environment),
            ).fetchone()
        if not row:
            return None
        return CarrierEndpoint(
            id=row["id"],
            carrier_id=row["carrier_id"],
            environment=row["environment"],
            endpoint_type=row["endpoint_type"],
            url=row["url"],
            http_method=row["http_method"],
            headers=json.loads(row["headers_json"]),
            timeout_seconds=row["timeout_seconds"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_endpoints(self, carrier_id: str, environment: str | None = None) -> list[CarrierEndpoint]:
        carrier = self.get_carrier(carrier_id)
        cid = carrier.id if carrier else carrier_id
        query = "SELECT * FROM carrier_endpoints WHERE carrier_id=?"
        params: list[Any] = [cid]
        if environment:
            query += " AND environment=?"
            params.append(environment)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            CarrierEndpoint(
                id=r["id"],
                carrier_id=r["carrier_id"],
                environment=r["environment"],
                endpoint_type=r["endpoint_type"],
                url=r["url"],
                http_method=r["http_method"],
                headers=json.loads(r["headers_json"]),
                timeout_seconds=r["timeout_seconds"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # ---------------- Credentials ----------------

    def set_credential(
        self,
        carrier_id: str,
        username: str,
        secret_ref: str,
        *,
        environment: str = "production",
        agency_code: str | None = None,
        producer_code: str | None = None,
        account_number: str | None = None,
        auth_type: str = "credentials",
        extra_fields: dict[str, Any] | None = None,
        status: str = "ACTIVE",
        expires_at: str | None = None,
    ) -> CarrierCredential:
        carrier = self.get_carrier(carrier_id)
        if not carrier:
            raise KeyError(f"Carrier not found: {carrier_id}")
        now = _now()
        cred_id = str(uuid.uuid4())
        extra = extra_fields or {}
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO carrier_credentials
                (id, carrier_id, environment, username, secret_ref, agency_code, producer_code,
                 account_number, auth_type, extra_fields_json, status, last_rotated_at, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(carrier_id, environment, username) DO UPDATE SET
                    secret_ref=excluded.secret_ref,
                    agency_code=excluded.agency_code,
                    producer_code=excluded.producer_code,
                    account_number=excluded.account_number,
                    auth_type=excluded.auth_type,
                    extra_fields_json=excluded.extra_fields_json,
                    status=excluded.status,
                    last_rotated_at=excluded.last_rotated_at,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at""",
                (
                    cred_id, carrier.id, environment, username, secret_ref, agency_code,
                    producer_code, account_number, auth_type, _json(extra), status,
                    now, expires_at, now, now,
                ),
            )
        cred = self.get_credential(carrier.id, username, environment=environment)
        assert cred is not None
        return cred

    def get_credential(
        self, carrier_id: str, username: str, environment: str = "production"
    ) -> CarrierCredential | None:
        carrier = self.get_carrier(carrier_id)
        cid = carrier.id if carrier else carrier_id
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM carrier_credentials WHERE carrier_id=? AND username=? AND environment=?",
                (cid, username, environment),
            ).fetchone()
        if not row:
            return None
        return self._decode_credential(row)

    def get_active_credential(
        self, carrier_id: str, environment: str = "production"
    ) -> CarrierCredential | None:
        carrier = self.get_carrier(carrier_id)
        cid = carrier.id if carrier else carrier_id
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM carrier_credentials WHERE carrier_id=? AND environment=? AND status='ACTIVE' ORDER BY updated_at DESC LIMIT 1",
                (cid, environment),
            ).fetchone()
        if not row:
            return None
        return self._decode_credential(row)

    def rotate_credential(
        self, carrier_id: str, username: str, new_secret_ref: str, *, environment: str = "production"
    ) -> CarrierCredential:
        now = _now()
        carrier = self.get_carrier(carrier_id)
        cid = carrier.id if carrier else carrier_id
        with self._connect() as conn:
            conn.execute(
                """UPDATE carrier_credentials SET secret_ref=?, last_rotated_at=?, updated_at=?
                WHERE carrier_id=? AND username=? AND environment=?""",
                (new_secret_ref, now, now, cid, username, environment),
            )
        res = self.get_credential(cid, username, environment=environment)
        if not res:
            raise KeyError(f"Credential not found for carrier={carrier_id}, user={username}")
        return res

    # ---------------- LOB Rules ----------------

    def set_lob_rule(
        self,
        carrier_id: str,
        line_of_business: str,
        *,
        state_eligibility: list[str] | None = None,
        appetite_rules: dict[str, Any] | None = None,
        required_fields: list[str] | None = None,
        submission_mode: str = "portal_automation",
        quote_auto_approval: bool = False,
        max_limit_amount: float | None = None,
    ) -> CarrierLobRule:
        carrier = self.get_carrier(carrier_id)
        if not carrier:
            raise KeyError(f"Carrier not found: {carrier_id}")
        states = [s.upper().strip() for s in (state_eligibility or [])]
        appetite = appetite_rules or {}
        req_fields = required_fields or []
        now = _now()
        rule_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO carrier_lob_rules
                (id, carrier_id, line_of_business, state_eligibility_json, appetite_rules_json,
                 required_fields_json, submission_mode, quote_auto_approval, max_limit_amount, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(carrier_id, line_of_business) DO UPDATE SET
                    state_eligibility_json=excluded.state_eligibility_json,
                    appetite_rules_json=excluded.appetite_rules_json,
                    required_fields_json=excluded.required_fields_json,
                    submission_mode=excluded.submission_mode,
                    quote_auto_approval=excluded.quote_auto_approval,
                    max_limit_amount=excluded.max_limit_amount,
                    updated_at=excluded.updated_at""",
                (
                    rule_id, carrier.id, line_of_business.lower().strip(),
                    _json(states), _json(appetite), _json(req_fields),
                    submission_mode, int(quote_auto_approval), max_limit_amount, now, now,
                ),
            )
        rule = self.get_lob_rule(carrier.id, line_of_business)
        assert rule is not None
        return rule

    def get_lob_rule(self, carrier_id: str, line_of_business: str) -> CarrierLobRule | None:
        carrier = self.get_carrier(carrier_id)
        cid = carrier.id if carrier else carrier_id
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM carrier_lob_rules WHERE carrier_id=? AND line_of_business=?",
                (cid, line_of_business.lower().strip()),
            ).fetchone()
        if not row:
            return None
        return self._decode_lob_rule(row)

    def list_lob_rules(self, carrier_id: str) -> list[CarrierLobRule]:
        carrier = self.get_carrier(carrier_id)
        cid = carrier.id if carrier else carrier_id
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM carrier_lob_rules WHERE carrier_id=?", (cid,)
            ).fetchall()
        return [self._decode_lob_rule(r) for r in rows]

    def validate_submission_appetite(
        self,
        carrier_id: str,
        line_of_business: str,
        *,
        state: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate whether a submission payload meets the carrier's state eligibility, required fields, and appetite rules."""
        carrier = self.get_carrier(carrier_id)
        if not carrier:
            return {"eligible": False, "reasons": [f"Unknown carrier: {carrier_id}"]}
        rule = self.get_lob_rule(carrier.id, line_of_business)
        if not rule:
            return {
                "eligible": False,
                "reasons": [f"Carrier '{carrier.name}' does not support line of business '{line_of_business}'"],
            }

        reasons: list[str] = []
        # State validation
        st = state.upper().strip()
        if rule.state_eligibility and st not in rule.state_eligibility:
            reasons.append(f"State '{st}' not eligible for {carrier.name} ({line_of_business})")

        # Required fields validation
        for field_name in rule.required_fields:
            if field_name not in payload or payload[field_name] is None or payload[field_name] == "":
                reasons.append(f"Missing required field: '{field_name}'")

        # Appetite limits check
        appetite = rule.appetite_rules
        if "max_limit" in appetite and "requested_limit" in payload:
            if float(payload["requested_limit"]) > float(appetite["max_limit"]):
                reasons.append(f"Requested limit ${payload['requested_limit']:,} exceeds carrier max ${appetite['max_limit']:,}")

        if "min_years_in_business" in appetite and "years_in_business" in payload:
            if float(payload["years_in_business"]) < float(appetite["min_years_in_business"]):
                reasons.append(f"Years in business ({payload['years_in_business']}) below minimum requirement ({appetite['min_years_in_business']})")

        if "prohibited_class_codes" in appetite and "class_code" in payload:
            if str(payload["class_code"]) in [str(c) for c in appetite["prohibited_class_codes"]]:
                reasons.append(f"Class code '{payload['class_code']}' is in carrier prohibited appetite list")

        return {
            "eligible": len(reasons) == 0,
            "carrier_id": carrier.id,
            "carrier_name": carrier.name,
            "line_of_business": line_of_business,
            "submission_mode": rule.submission_mode,
            "reasons": reasons,
        }

    # ---------------- Auth Requirements ----------------

    def set_auth_requirement(
        self,
        carrier_id: str,
        *,
        mfa_type: str = "none",
        mfa_secret_ref: str | None = None,
        session_timeout_minutes: int = 30,
        password_rotation_days: int = 90,
        captcha_type: str = "none",
        ip_allowlist: list[str] | None = None,
        headers_template: dict[str, str] | None = None,
    ) -> CarrierAuthRequirement:
        carrier = self.get_carrier(carrier_id)
        if not carrier:
            raise KeyError(f"Carrier not found: {carrier_id}")
        now = _now()
        req_id = str(uuid.uuid4())
        allowlist = ip_allowlist or []
        headers = headers_template or {}
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO carrier_auth_requirements
                (id, carrier_id, mfa_type, mfa_secret_ref, session_timeout_minutes,
                 password_rotation_days, captcha_type, ip_allowlist_json, headers_template_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(carrier_id) DO UPDATE SET
                    mfa_type=excluded.mfa_type,
                    mfa_secret_ref=excluded.mfa_secret_ref,
                    session_timeout_minutes=excluded.session_timeout_minutes,
                    password_rotation_days=excluded.password_rotation_days,
                    captcha_type=excluded.captcha_type,
                    ip_allowlist_json=excluded.ip_allowlist_json,
                    headers_template_json=excluded.headers_template_json,
                    updated_at=excluded.updated_at""",
                (
                    req_id, carrier.id, mfa_type, mfa_secret_ref, session_timeout_minutes,
                    password_rotation_days, captcha_type, _json(allowlist), _json(headers), now, now,
                ),
            )
        auth_req = self.get_auth_requirement(carrier.id)
        assert auth_req is not None
        return auth_req

    def get_auth_requirement(self, carrier_id: str) -> CarrierAuthRequirement | None:
        carrier = self.get_carrier(carrier_id)
        cid = carrier.id if carrier else carrier_id
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM carrier_auth_requirements WHERE carrier_id=?", (cid,)
            ).fetchone()
        if not row:
            return None
        return CarrierAuthRequirement(
            id=row["id"],
            carrier_id=row["carrier_id"],
            mfa_type=row["mfa_type"],
            mfa_secret_ref=row["mfa_secret_ref"],
            session_timeout_minutes=row["session_timeout_minutes"],
            password_rotation_days=row["password_rotation_days"],
            captcha_type=row["captcha_type"],
            ip_allowlist=json.loads(row["ip_allowlist_json"]),
            headers_template=json.loads(row["headers_template_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ---------------- Helpers ----------------

    @staticmethod
    def _decode_credential(row: sqlite3.Row) -> CarrierCredential:
        return CarrierCredential(
            id=row["id"],
            carrier_id=row["carrier_id"],
            environment=row["environment"],
            username=row["username"],
            secret_ref=row["secret_ref"],
            agency_code=row["agency_code"],
            producer_code=row["producer_code"],
            account_number=row["account_number"],
            auth_type=row["auth_type"],
            extra_fields=json.loads(row["extra_fields_json"]),
            status=row["status"],
            last_rotated_at=row["last_rotated_at"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _decode_lob_rule(row: sqlite3.Row) -> CarrierLobRule:
        return CarrierLobRule(
            id=row["id"],
            carrier_id=row["carrier_id"],
            line_of_business=row["line_of_business"],
            state_eligibility=json.loads(row["state_eligibility_json"]),
            appetite_rules=json.loads(row["appetite_rules_json"]),
            required_fields=json.loads(row["required_fields_json"]),
            submission_mode=row["submission_mode"],
            quote_auto_approval=bool(row["quote_auto_approval"]),
            max_limit_amount=row["max_limit_amount"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
