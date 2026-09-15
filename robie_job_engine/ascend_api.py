"""Bounded Ascend API program creation and independent read-back.

This is the only operational path for creating an Ascend program and its
billables. It does not use Playwright, CDP, browser selectors, or the Ascend UI.

No API call is made by validation or planning.  Execution is opt-in and the
configured API host must match ROBIE_ENV.  Credentials are loaded only from a
Secret Manager version reference; they are never accepted in a Job payload.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib import error, parse, request
from uuid import UUID

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, current_robie_env
from .secret_manager import GoogleSecretManagerAccessor, SecretAccessor
from .secrets import redact_text
from .write_markers import record_write_marker


ACTION_TYPE = "ascend.create_program"
WORKER_NAME = "ascend-api"
SANDBOX_API_ORIGIN = "https://sandbox.api.useascend.com"
PRODUCTION_API_ORIGIN = "https://api.useascend.com"
API_VERSION_PATH = "/v1"
FORBIDDEN_ACCOUNTS = frozenset({"pawiva", "221398001"})

PROGRAM_FIELDS = frozenset(
    {
        "account_manager_id",
        "secondary_account_manager_id",
        "allowed_payment_methods",
        "billing_type",
        "due_date",
        "failure_callback_url",
        "hidden_payment_options",
        "insured_id",
        "is_checkout_overview_page_visible",
        "is_checkout_receipt_page_visible",
        "organization_account",
        "producer_id",
        "return_url",
        "success_callback_url",
        "metadata",
    }
)

BILLABLE_FIELDS = frozenset(
    {
        "agency_fees_cents",
        "attachments",
        "billable_identifier",
        "billable_type",
        "broker_fee_cents",
        "carrier_identifier",
        "coverage_identifier",
        "description",
        "effective_date",
        "expiration_date",
        "metadata",
        "min_earned_rate",
        "organization_account_commission_cents",
        "organization_account_commission_rate",
        "organization_commission_cents",
        "organization_commission_rate",
        "other_fees_cents",
        "parent_billable_id",
        "policy_fee_cents",
        "policy_number",
        "premium_cents",
        "program_id",
        "seller_commission_amount_cents",
        "seller_commission_rate",
        "surplus_lines_tax_cents",
        "taxes_and_fees_cents",
        "wholesaler_identifier",
    }
)

REQUIRED_PROGRAM_FIELDS = ("insured_id", "producer_id", "account_manager_id")
REQUIRED_BILLABLE_FIELDS = (
    "billable_identifier",
    "carrier_identifier",
    "coverage_identifier",
    "effective_date",
    "expiration_date",
    "premium_cents",
)


class AscendConfigurationError(RuntimeError):
    """The integration is disabled, unsafe, or missing its secret reference."""


class AscendPayloadError(ValueError):
    """The requested program is incomplete or outside the bounded contract."""


class AscendApiError(RuntimeError):
    """A sanitized Ascend API failure."""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        super().__init__(redact_text(message))

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or bool(self.status and self.status >= 500)


class AscendTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def _truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().casefold() in {"1", "true", "yes"}


def _uuid(value: Any, field: str) -> str:
    raw = str(value or "").strip()
    try:
        return str(UUID(raw))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AscendPayloadError(f"{field} must be a UUID") from exc


def _date(value: Any, field: str) -> str:
    raw = str(value or "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise AscendPayloadError(f"{field} must use YYYY-MM-DD") from exc


def _contains_forbidden_account(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_forbidden_account(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_account(item) for item in value)
    normalized = " ".join(str(value or "").casefold().split())
    return any(marker in normalized for marker in FORBIDDEN_ACCOUNTS)


def _allowlisted(source: dict[str, Any], allowed: frozenset[str], label: str) -> dict[str, Any]:
    unknown = sorted(set(source) - set(allowed))
    if unknown:
        raise AscendPayloadError(f"unsupported {label} fields: {', '.join(unknown)}")
    return dict(source)


def validate_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized API plan without credentials or network access."""
    if not isinstance(payload, dict):
        raise AscendPayloadError("payload must be an object")
    if _contains_forbidden_account(payload):
        raise AscendPayloadError("forbidden account: PAWIVA / 221398001")

    raw_program = payload.get("program")
    if not isinstance(raw_program, dict):
        raise AscendPayloadError("program must be an object")
    program = _allowlisted(raw_program, PROGRAM_FIELDS, "program")
    for field in REQUIRED_PROGRAM_FIELDS:
        program[field] = _uuid(program.get(field), f"program.{field}")

    raw_billables = payload.get("billables")
    if not isinstance(raw_billables, list) or not raw_billables:
        raise AscendPayloadError("billables must contain at least one quote")
    billables: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_billables):
        if not isinstance(raw, dict):
            raise AscendPayloadError(f"billables[{index}] must be an object")
        billable = _allowlisted(raw, BILLABLE_FIELDS, f"billables[{index}]")
        for field in REQUIRED_BILLABLE_FIELDS:
            if billable.get(field) in (None, ""):
                raise AscendPayloadError(f"billables[{index}].{field} is required")
        billable["effective_date"] = _date(
            billable["effective_date"], f"billables[{index}].effective_date"
        )
        billable["expiration_date"] = _date(
            billable["expiration_date"], f"billables[{index}].expiration_date"
        )
        premium = billable.get("premium_cents")
        if isinstance(premium, bool) or not isinstance(premium, int) or premium < 0:
            raise AscendPayloadError(
                f"billables[{index}].premium_cents must be a non-negative integer"
            )
        for field in (
            "agency_fees_cents",
            "broker_fee_cents",
            "other_fees_cents",
            "policy_fee_cents",
            "surplus_lines_tax_cents",
            "taxes_and_fees_cents",
        ):
            value = billable.get(field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise AscendPayloadError(
                    f"billables[{index}].{field} must be a non-negative integer"
                )
        billables.append(billable)

    return {
        "action_type": ACTION_TYPE,
        "program": program,
        "billables": billables,
        "requests": [
            {"method": "POST", "path": "/programs"},
            *({"method": "POST", "path": "/billables"} for _ in billables),
        ],
        "network_performed": False,
    }


@dataclass(frozen=True)
class AscendApiConfig:
    origin: str
    token: str

    @classmethod
    def from_environment(
        cls, accessor: SecretAccessor | None = None
    ) -> "AscendApiConfig":
        if not _truthy("ROBIE_ASCEND_API_ENABLED"):
            raise AscendConfigurationError("Ascend API execution is not enabled")
        env = current_robie_env()
        origin = str(os.environ.get("ROBIE_ASCEND_API_BASE_URL") or SANDBOX_API_ORIGIN).rstrip("/")
        if origin.endswith("/v1"):
            origin = origin[:-3]
        if env == TEST_ENV_NAME and origin != SANDBOX_API_ORIGIN:
            raise AscendConfigurationError("TEST may use only the Ascend sandbox API")
        if env in PRODUCTION_ENV_NAMES:
            if origin != PRODUCTION_API_ORIGIN:
                raise AscendConfigurationError("Production may use only the Ascend Production API")
            if not _truthy("ROBIE_ASCEND_API_PRODUCTION_ENABLED"):
                raise AscendConfigurationError("Ascend Production API execution is not enabled")
        elif env != TEST_ENV_NAME:
            raise AscendConfigurationError("ROBIE_ENV must be TEST or PRODUCTION")

        token = str(os.environ.get("ROBIE_ASCEND_API_KEY") or "").strip()
        secret_ref = str(os.environ.get("ROBIE_ASCEND_API_KEY_SECRET") or "").strip()
        if not token:
            if not secret_ref:
                raise AscendConfigurationError("ROBIE_ASCEND_API_KEY or ROBIE_ASCEND_API_KEY_SECRET is required")
            if not secret_ref.startswith("projects/"):
                project = os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "streetsmart-hermes-poc"
                secret_ref = f"projects/{project}/secrets/{secret_ref}/versions/latest"
            secret_accessor = accessor or GoogleSecretManagerAccessor()
            token = secret_accessor.access(secret_ref).strip()
        if not token:
            raise AscendConfigurationError("Ascend API credential is empty")
        return cls(origin=origin, token=token)


class UrllibAscendTransport:
    def __init__(self, config: AscendApiConfig, *, timeout_seconds: float = 30) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or ".." in path:
            raise ValueError("Ascend API path must be an absolute bounded path")
        url = f"{self.config.origin}{API_VERSION_PATH}{path}"
        if query:
            clean_query = {key: value for key, value in query.items() if value is not None}
            url = f"{url}?{parse.urlencode(clean_query)}"
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.token}",
            "User-Agent": "streetsmart-robie-hermes/ascend-api",
        }
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except error.HTTPError as exc:
            try:
                detail = exc.read(2_000).decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            raise AscendApiError(exc.code, f"Ascend API returned HTTP {exc.code}: {detail}") from exc
        except (error.URLError, TimeoutError, socket.timeout) as exc:
            raise AscendApiError(None, f"Ascend API is unavailable: {exc}") from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AscendApiError(None, "Ascend API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise AscendApiError(None, "Ascend API returned a non-object response")
        return decoded


class AscendApiClient:
    def __init__(self, transport: AscendTransport) -> None:
        self.transport = transport

    @staticmethod
    def _record(response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        if isinstance(data, dict):
            return data
        return response

    @staticmethod
    def _record_id(response: dict[str, Any], label: str) -> str:
        record = AscendApiClient._record(response)
        identifier = record.get("id")
        try:
            return str(UUID(str(identifier)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AscendApiError(None, f"Ascend {label} response did not contain a UUID id") from exc

    def create_program(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        response = self.transport.request("POST", "/programs", json_body=body)
        # Slice 4: every successful Ascend create records a worker-unforgeable
        # write marker. record_write_marker never raises.
        record_write_marker(method="ascend.create_program", url="/programs")
        return self._record_id(response, "program"), self._record(response)

    def get_program(self, program_id: str) -> dict[str, Any]:
        return self._record(self.transport.request("GET", f"/programs/{_uuid(program_id, 'program_id')}"))

    def create_billable(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        response = self.transport.request("POST", "/billables", json_body=body)
        record_write_marker(method="ascend.create_billable", url="/billables")
        return self._record_id(response, "billable"), self._record(response)

    def get_billable(self, billable_id: str) -> dict[str, Any]:
        return self._record(
            self.transport.request("GET", f"/billables/{_uuid(billable_id, 'billable_id')}")
        )

    def search_carriers(self, query: str = "") -> list[dict[str, Any]]:
        query_params = {"search_query": query} if query else {}
        response = self.transport.request("GET", "/carriers", query=query_params)
        data = response.get("data")
        return data if isinstance(data, list) else []

    def search_wholesalers(self, query: str = "") -> list[dict[str, Any]]:
        query_params = {"search_query": query} if query else {}
        response = self.transport.request("GET", "/wholesalers", query=query_params)
        data = response.get("data")
        return data if isinstance(data, list) else []

    def list_insureds(
        self, business_name: str | None = None, email: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if business_name:
            params["business_name"] = business_name
        if email:
            params["email"] = email
        response = self.transport.request("GET", "/insureds", query=params)
        data = response.get("data")
        return data if isinstance(data, list) else []

    def create_insured(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        response = self.transport.request("POST", "/insureds", json_body=body)
        record_write_marker(method="ascend.create_insured", url="/insureds")
        return self._record_id(response, "insured"), self._record(response)

    def find_or_create_insured(
        self,
        business_name: str,
        email: str | None = None,
        phone: str | None = None,
        address: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if business_name:
            existing = self.list_insureds(business_name=business_name)
            if existing:
                first = existing[0]
                return str(first.get("id")), first
        if email:
            existing_email = self.list_insureds(email=email)
            if existing_email:
                first = existing_email[0]
                return str(first.get("id")), first

        payload: dict[str, Any] = {
            "business_name": business_name,
            "is_business": True,
        }
        if email:
            payload["email"] = email
        if phone:
            payload["phone"] = phone
        if address:
            for k in (
                "mailing_address_street_one",
                "mailing_address_street_two",
                "mailing_address_city",
                "mailing_address_state",
                "mailing_address_zip_code",
            ):
                if k in address:
                    payload[k] = address[k]
        return self.create_insured(payload)

    def list_users(self) -> list[dict[str, Any]]:
        response = self.transport.request("GET", "/users")
        data = response.get("data")
        return data if isinstance(data, list) else []

    def resolve_user(self, name_or_email: str) -> str:
        users = self.list_users()
        target = name_or_email.strip().lower()
        for u in users:
            if u.get("email", "").lower() == target:
                return str(u.get("id"))
        for u in users:
            full_name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip().lower()
            if target in full_name or full_name in target:
                return str(u.get("id"))
        return "3b5cc5b8-3636-4342-bd3d-9da12d2f690e"

    def find_program_by_policy(self, policy_number: str) -> dict[str, Any] | None:
        """Find active or purchased program containing the given policy number.

        Returns None only when the policy genuinely has no matching program.
        Transport, authentication, and API failures raise AscendApiError so a
        failed lookup is never mistaken for "no program found".
        """
        clean_policy = policy_number.strip().upper()
        # 1. Search billables directly if possible
        resp = self.transport.request("GET", "/billables", query={"policy_number": clean_policy})
        items = resp.get("data", [])
        if items:
            first_billable = items[0]
            prog_id = first_billable.get("program_id")
            if prog_id:
                prog = self.get_program(prog_id)
                return {
                    "program": prog,
                    "billable": first_billable,
                    "program_id": prog_id,
                    "parent_billable_id": first_billable.get("id"),
                }

        # 2. Fallback: inspect recent programs
        progs = self.transport.request("GET", "/programs", query={"page_size": 50}).get("data", [])
        for p in progs:
            pid = p.get("id")
            b_resp = self.transport.request("GET", f"/programs/{pid}/billables")
            for b in b_resp.get("data", []):
                if str(b.get("policy_number", "")).strip().upper() == clean_policy:
                    return {
                        "program": p,
                        "billable": b,
                        "program_id": pid,
                        "parent_billable_id": b.get("id"),
                    }
        return None

    @staticmethod
    def extract_program_uuid(text: str) -> str | None:
        """Pull the Ascend program UUID from a dashboard link in notice emails.

        Every Ascend notice email (late payment, cancellation, return premium)
        carries a link shaped like
        https://dashboard.useascend.com/programs/<uuid>[...].  Looking the
        program up by this UUID is more reliable than searching by policy
        number, so triage should prefer it.
        """
        match = re.search(
            r"dashboard\.useascend\.com/programs/([0-9a-fA-F-]{36})", text or ""
        )
        if not match:
            return None
        try:
            return str(UUID(match.group(1)))
        except (TypeError, ValueError, AttributeError):
            return None

    def create_endorsement_billable(
        self,
        *,
        program_id: str,
        parent_billable_id: str,
        description: str,
        premium_cents: int,
        effective_date: str,
        taxes_and_fees_cents: int = 0,
        seller_commission_rate: float | None = None,
        billable_identifier: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Create an endorsement billable attached to an existing policy program."""
        parent = self.get_billable(parent_billable_id)
        carrier = parent.get("carrier") or {}
        wholesaler = parent.get("wholesaler") or {}
        coverage = parent.get("coverage_type") or {}

        carrier_id = carrier.get("identifier") or parent.get("carrier_identifier")
        wholesaler_id = wholesaler.get("identifier") or parent.get("wholesaler_identifier")
        coverage_id = coverage.get("identifier") or parent.get("coverage_identifier") or "gl"
        exp_date = parent.get("expiration_date") or effective_date

        payload: dict[str, Any] = {
            "program_id": program_id,
            "parent_billable_id": parent_billable_id,
            "billable_type": "endorsement",
            "billable_identifier": billable_identifier or "1",
            "policy_number": parent.get("policy_number", ""),
            "description": description,
            "effective_date": effective_date,
            "expiration_date": exp_date,
            "premium_cents": int(premium_cents),
            "taxes_and_fees_cents": int(taxes_and_fees_cents),
            "carrier_identifier": carrier_id,
            "coverage_identifier": coverage_id,
        }
        if wholesaler_id:
            payload["wholesaler_identifier"] = wholesaler_id
        if seller_commission_rate is not None:
            payload["seller_commission_rate"] = float(seller_commission_rate)

        return self.create_billable(payload)


def configured_client() -> AscendApiClient:
    return AscendApiClient(UrllibAscendTransport(AscendApiConfig.from_environment()))


def _job_metadata(job: dict[str, Any], idempotency_key: str) -> dict[str, str]:
    return {
        "robie_job_id": str(job.get("id") or "")[:128],
        "robie_idempotency_key": str(idempotency_key or "")[:128],
    }


def _merge_metadata(body: dict[str, Any], metadata: dict[str, str]) -> dict[str, Any]:
    merged = dict(body)
    current = dict(merged.get("metadata") or {})
    current.update(metadata)
    merged["metadata"] = current
    return merged


class AscendCreateProgramWorker:
    """Create one program followed by one or more quote billables."""

    def __init__(self, client_factory: Callable[[], AscendApiClient] = configured_client) -> None:
        self.client_factory = client_factory

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = dict(job.get("payload") or {})
        if payload.get("execute") is not True:
            return WorkerResult(
                False,
                ACTION_TYPE,
                {},
                retryable=False,
                error="Ascend API creation requires execute=true",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        try:
            plan = validate_create_payload(payload)
            client = self.client_factory()
        except (AscendPayloadError, AscendConfigurationError) as exc:
            return WorkerResult(
                False,
                ACTION_TYPE,
                {},
                retryable=False,
                error=str(exc),
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )

        metadata = _job_metadata(job, idempotency_key)
        try:
            program_id, program_record = client.create_program(_merge_metadata(plan["program"], metadata))
            program_url = str(program_record.get("program_url") or "")
        except AscendApiError as exc:
            return WorkerResult(
                False,
                ACTION_TYPE,
                {},
                retryable=exc.retryable,
                error=str(exc),
                hold_status=JobStatus.NEEDS_AUTH if exc.status in {401, 403} else None,
            )

        billable_ids: list[str] = []
        try:
            for billable in plan["billables"]:
                body = _merge_metadata(billable, metadata)
                body["program_id"] = program_id
                billable_id, _ = client.create_billable(body)
                billable_ids.append(billable_id)
        except AscendApiError as exc:
            # The program already exists.  Persist its identity and proceed to
            # verification instead of retrying and creating a duplicate.
            return WorkerResult(
                True,
                ACTION_TYPE,
                {
                    "resource_id": program_id,
                    "program_id": program_id,
                    "program_url": program_url,
                },
                detail={
                    "program_created": True,
                    "program_url": program_url,
                    "billable_ids": billable_ids,
                    "expected_billable_count": len(plan["billables"]),
                    "partial_failure": str(exc),
                },
                retryable=False,
            )

        return WorkerResult(
            True,
            ACTION_TYPE,
            {
                "resource_id": program_id,
                "program_id": program_id,
                "program_url": program_url,
            },
            detail={
                "program_created": True,
                "program_url": program_url,
                "billable_ids": billable_ids,
                "expected_billable_count": len(plan["billables"]),
            },
            retryable=False,
        )


def _normalized_program(record: dict[str, Any], program_id: str) -> dict[str, Any]:
    def related_id(field: str) -> str:
        direct = record.get(f"{field}_id")
        if direct:
            return str(direct)
        related = record.get(field)
        if isinstance(related, dict):
            return str(related.get("id") or "")
        return ""

    return {
        "resource_id": program_id,
        "insured_id": related_id("insured"),
        "producer_id": related_id("producer"),
        "account_manager_id": related_id("account_manager"),
    }


def _billable_program_id(record: dict[str, Any]) -> str:
    direct = record.get("program_id")
    if direct:
        return str(direct)
    program = record.get("program")
    if isinstance(program, dict):
        return str(program.get("id") or "")
    return ""


def _expected_billable(record: dict[str, Any], program_id: str) -> dict[str, Any]:
    """Business postcondition for one requested billable.

    Attachments and metadata have separate storage/shape contracts. Every
    scalar billable value supplied to the create API is consequential and is
    therefore included in fresh readback comparison.
    """
    return {
        "program_id": program_id,
        **{
            field: record[field]
            for field in sorted(BILLABLE_FIELDS - {"attachments", "metadata"})
            if field in record
        },
    }


def _observed_billable(
    record: dict[str, Any], expected: dict[str, Any], program_id: str
) -> dict[str, Any]:
    return {
        "program_id": _billable_program_id(record),
        **{
            field: record.get(field)
            for field in expected
            if field != "program_id"
        },
    }


class AscendCreateProgramVerifier:
    """Fresh authoritative GET of the program and every created billable."""

    def __init__(self, client_factory: Callable[[], AscendApiClient] = configured_client) -> None:
        self.client_factory = client_factory

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        destination = dict(action.get("destination") or {})
        detail = dict(action.get("detail") or {})
        program_id = str(destination.get("program_id") or destination.get("resource_id") or "")
        payload = dict(job.get("payload") or {})
        try:
            plan = validate_create_payload(payload)
        except AscendPayloadError as exc:
            plan = {"program": {}, "billables": []}
            validation_error: str | None = str(exc)
        else:
            validation_error = None
        expected_program = dict(plan["program"])
        expected_billable_count = len(plan["billables"])
        billable_ids = [str(item) for item in detail.get("billable_ids") or []]
        expected = {
            "resource_id": program_id,
            "insured_id": str(expected_program.get("insured_id") or ""),
            "producer_id": str(expected_program.get("producer_id") or ""),
            "account_manager_id": str(expected_program.get("account_manager_id") or ""),
            "billable_count": expected_billable_count,
            "billables": sorted(
                (
                    _expected_billable(item, program_id)
                    for item in plan["billables"]
                ),
                key=lambda item: str(item.get("billable_identifier") or ""),
            ),
        }
        observed: dict[str, Any] = {
            "resource_id": program_id,
            "billable_count": 0,
            "billables": [],
        }
        try:
            if validation_error:
                raise AscendPayloadError(validation_error)
            client = self.client_factory()
            observed.update(_normalized_program(client.get_program(program_id), program_id))
            expected_by_identifier = {
                str(item.get("billable_identifier") or ""): item
                for item in expected["billables"]
            }
            observed_billables: list[dict[str, Any]] = []
            for billable_id in billable_ids:
                record = client.get_billable(billable_id)
                if _billable_program_id(record) != program_id:
                    raise AscendApiError(None, "Ascend billable is not bound to the created program")
                identifier = str(record.get("billable_identifier") or "")
                expected_item = expected_by_identifier.get(identifier)
                if expected_item is None:
                    raise AscendApiError(
                        None,
                        "Ascend returned an unexpected billable identifier",
                    )
                observed_billables.append(
                    _observed_billable(record, expected_item, program_id)
                )
            observed["billable_count"] = len(billable_ids)
            observed["billables"] = sorted(
                observed_billables,
                key=lambda item: str(item.get("billable_identifier") or ""),
            )
        except (AscendApiError, AscendConfigurationError, AscendPayloadError) as exc:
            observed["readback_error"] = str(exc)
            evidence = VerificationEvidence(
                method="ASCEND_API_FRESH_GET",
                source="ascend-api",
                expected=expected,
                observed=observed,
                authoritative=True,
                captured_at=datetime.now(timezone.utc).isoformat(),
                locator=program_id or None,
            )
            return VerificationResult(False, evidence, retryable=False, error=str(exc))

        verified = observed == expected
        evidence = VerificationEvidence(
            method="ASCEND_API_FRESH_GET",
            source="ascend-api",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=program_id,
        )
        return VerificationResult(
            verified,
            evidence,
            retryable=False,
            error=None if verified else "Ascend API read-back did not match the requested program",
        )
