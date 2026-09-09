"""Test-only reads grounded in the EZLynx Postman collection supplied by Jake.

Collection: https://documenter.getpostman.com/view/56716523/2sBYApyCjN
Task create/search/read and general applicant search are not documented there.
Those capabilities deliberately remain unavailable rather than guessing URLs.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .intake_core import Identifiers, IntakeHold, ReadResult, require_test


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EzlynxReadTransport:
    """Credentials come from the existing server integration, never a Job payload.

    No authentication endpoint is invoked here. The credential provider must
    supply a current token and the approved API host must be explicitly provided.
    Redirects are refused so credentials cannot follow a redirected request.
    """
    def __init__(self, base_url: str, headers_provider: Callable[[], Mapping[str, str]]):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise IntakeHold("An explicit HTTPS EZLynx API base URL without credentials is required")
        self.base_url = base_url.rstrip("/") + "/"
        self.headers_provider = headers_provider

    def get(self, path: str) -> Any:
        require_test()
        if path.startswith(("/", ".")) or "://" in path:
            raise IntakeHold("Only relative documented API paths are accepted")
        supplied = self.headers_provider()
        required = ("EZAppSecret", "EZToken", "AccountUsername")
        if not all(isinstance(supplied.get(k), str) and supplied[k].strip() for k in required):
            raise IntakeHold("The existing API credential provider is not configured")
        request = Request(self.base_url + path, headers={k: supplied[k] for k in required}, method="GET")
        request.add_header("Accept", "application/json")
        try:
            with build_opener(_NoRedirect()).open(request, timeout=20) as response:
                data = response.read(10 * 1024 * 1024 + 1)
                if len(data) > 10 * 1024 * 1024:
                    raise IntakeHold("API response exceeds the bounded read limit")
                return json.loads(data)
        except HTTPError as exc:
            # HTTP response bodies may contain PII or credential details.
            raise IntakeHold(f"EZLynx read unavailable (HTTP {exc.code})") from None
        except (URLError, TimeoutError, ValueError):
            raise IntakeHold("EZLynx read failed or did not return valid JSON") from None


def _id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntakeHold("An explicit EZLynx identifier is required")
    return quote(value.strip(), safe="")


class DocumentedEzlynxReader:
    """Partial live adapter: exact-ID reads only; task operations refuse."""
    def __init__(self, transport: EzlynxReadTransport):
        self.transport = transport

    def lookup_candidates(self, identifiers: Identifiers) -> ReadResult:
        require_test()
        if not identifiers.applicant_id:
            raise IntakeHold("General applicant search is absent from this collection; supply a verified applicant ID")
        applicant = self.transport.get("Applicant/v2/" + _id(identifiers.applicant_id))
        if not isinstance(applicant, dict) or str(applicant.get("Id") or "") != identifiers.applicant_id:
            raise IntakeHold("Applicant API response does not confirm the requested ID")
        candidate = {
            "applicant_id": str(applicant["Id"]),
            "insured_email": str(applicant.get("BusinessEmail") or applicant.get("Email") or ""),
            "insured_name": str(applicant.get("BusinessName") or " ".join(
                str(applicant.get(k) or "") for k in ("FirstName", "LastName"))).strip(),
        }
        if any((identifiers.policy_id, identifiers.policy_number, identifiers.policy_effective_date)):
            if not identifiers.policy_id:
                raise IntakeHold("Policy-number search is not documented; supply a verified policy ID")
            policy = self.transport.get("Policy/" + _id(identifiers.policy_id) + "?encryptPolicyIdAndApplicantId=false")
            if not isinstance(policy, dict) or str(policy.get("Id") or "") != identifiers.policy_id or str(policy.get("ApplicantId") or "") != identifiers.applicant_id:
                raise IntakeHold("Policy API response does not confirm the policy/account relationship")
            candidate.update(policy_id=str(policy["Id"]), policy_number=str(policy.get("PolicyNumber") or ""),
                             policy_effective_date=str(policy.get("EffectiveDate") or "").split("T")[0])
        return ReadResult((candidate,), authoritative=True, complete=True)

    def lookup_assignee(self, user_id: str) -> ReadResult:
        require_test()
        user = self.transport.get("User/" + _id(user_id))
        if not isinstance(user, dict) or str(user.get("Id") or "") != user_id or not isinstance(user.get("IsActive"), bool):
            raise IntakeHold("User API response is incomplete or identifies a different user")
        return ReadResult(({"user_id": str(user["Id"]), "active": user["IsActive"]},), True, True)

    def find_users_by_login(self, org_id: str, username: str) -> ReadResult:
        require_test()
        if not username.strip():
            raise IntakeHold("A specific EZLynx username is required")
        users = self.transport.get("User/Users/" + _id(org_id))
        if not isinstance(users, list):
            raise IntakeHold("Organization user response is not the documented list")
        selected = []
        for user in users:
            if not isinstance(user, dict):
                raise IntakeHold("Organization user list is incomplete")
            if str(user.get("UserName") or "").casefold() == username.casefold():
                if not user.get("Id") or not isinstance(user.get("IsActive"), bool):
                    raise IntakeHold("Matching user record is incomplete")
                selected.append({"user_id": str(user["Id"]), "active": user["IsActive"], "username": user["UserName"]})
        return ReadResult(tuple(selected), True, True)

    def find_source_tasks(self, source_key: str) -> ReadResult:
        raise IntakeHold("Task search/read API is absent from the supplied collection; existing server adapter required")

    def find_related_work(self, applicant_id, policy_id, source) -> ReadResult:
        raise IntakeHold("Related-work/task API has not been identified")

    def create_task_once(self, task) -> str:
        raise IntakeHold("Task creation and idempotency API have not been identified")

    def read_task(self, task_id: str) -> ReadResult:
        raise IntakeHold("Task read-back API has not been identified")
