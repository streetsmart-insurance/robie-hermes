from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from robie_job_engine.ascend_api import (
    ACTION_TYPE,
    PRODUCTION_API_ORIGIN,
    SANDBOX_API_ORIGIN,
    AscendApiClient,
    AscendApiConfig,
    AscendApiError,
    AscendConfigurationError,
    AscendCreateProgramVerifier,
    AscendCreateProgramWorker,
    AscendPayloadError,
    validate_create_payload,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.engine import JobEngine
from robie_job_engine.store import JobStore
from durable_temp import durable_temporary_directory


INSURED_ID = str(uuid4())
CONTACT_ID = str(uuid4())
PRODUCER_ID = str(uuid4())
MANAGER_ID = str(uuid4())
PROGRAM_ID = str(uuid4())
BILLABLE_ID = str(uuid4())


def payload() -> dict:
    return {
        "program": {
            "insured_id": INSURED_ID,
            "producer_id": PRODUCER_ID,
            "account_manager_id": MANAGER_ID,
        },
        "billables": [
            {
                "billable_identifier": "TEST-QUOTE-1",
                "carrier_identifier": "test-carrier",
                "coverage_identifier": "commercial_auto",
                "effective_date": "2026-09-01",
                "expiration_date": "2027-09-01",
                "premium_cents": 125000,
                "agency_fees_cents": 50000,
            }
        ],
    }


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, path, *, query=None, json_body=None):
        self.calls.append((method, path, query, json_body))
        if method == "POST" and path == "/programs":
            return {
                "id": PROGRAM_ID,
                "insured_id": INSURED_ID,
                "producer_id": PRODUCER_ID,
                "account_manager_id": MANAGER_ID,
            }
        if method == "POST" and path == "/billables":
            return {"id": BILLABLE_ID, "program_id": PROGRAM_ID}
        if method == "GET" and path == f"/programs/{PROGRAM_ID}":
            return {
                "id": PROGRAM_ID,
                "insured_id": INSURED_ID,
                "producer_id": PRODUCER_ID,
                "account_manager_id": MANAGER_ID,
            }
        if method == "GET" and path == f"/billables/{BILLABLE_ID}":
            return {
                "id": BILLABLE_ID,
                "program_id": PROGRAM_ID,
                **payload()["billables"][0],
            }
        raise AssertionError((method, path))


class Accessor:
    def access(self, resource_name):
        self.resource_name = resource_name
        return "REDACTED_TEST_SECRET"


class AscendApiTests(unittest.TestCase):
    def test_plan_is_network_free_and_normalized(self):
        plan = validate_create_payload(payload())
        self.assertFalse(plan["network_performed"])
        self.assertEqual(plan["requests"][0], {"method": "POST", "path": "/programs"})
        self.assertEqual(plan["requests"][1], {"method": "POST", "path": "/billables"})

    def test_forbidden_account_is_refused_at_any_depth(self):
        request_payload = payload()
        request_payload["program"]["metadata"] = {"customer": "PAWIVA"}
        with self.assertRaisesRegex(AscendPayloadError, "forbidden account"):
            validate_create_payload(request_payload)
        request_payload = payload()
        request_payload["program"]["metadata"] = {"account": "221398001"}
        with self.assertRaisesRegex(AscendPayloadError, "forbidden account"):
            validate_create_payload(request_payload)

    def test_unknown_fields_and_invalid_money_fail_closed(self):
        request_payload = payload()
        request_payload["program"]["surprise"] = True
        with self.assertRaisesRegex(AscendPayloadError, "unsupported program"):
            validate_create_payload(request_payload)
        request_payload = payload()
        request_payload["billables"][0]["premium_cents"] = 12.50
        with self.assertRaisesRegex(AscendPayloadError, "non-negative integer"):
            validate_create_payload(request_payload)

    def test_configuration_requires_enable_secret_and_matching_host(self):
        base = {
            "ROBIE_ENV": "TEST",
            "ROBIE_ASCEND_API_ENABLED": "1",
            "ROBIE_ASCEND_API_KEY_SECRET": "projects/p/secrets/ascend-sandbox/versions/1",
        }
        with patch.dict(os.environ, base, clear=True):
            config = AscendApiConfig.from_environment(Accessor())
        self.assertEqual(config.origin, SANDBOX_API_ORIGIN)
        with patch.dict(
            os.environ,
            {**base, "ROBIE_ASCEND_API_BASE_URL": PRODUCTION_API_ORIGIN},
            clear=True,
        ):
            with self.assertRaisesRegex(AscendConfigurationError, "TEST may use only"):
                AscendApiConfig.from_environment(Accessor())

    def test_production_requires_a_second_explicit_enable(self):
        env = {
            "ROBIE_ENV": "PRODUCTION",
            "ROBIE_ASCEND_API_ENABLED": "1",
            "ROBIE_ASCEND_API_BASE_URL": PRODUCTION_API_ORIGIN,
            "ROBIE_ASCEND_API_KEY_SECRET": "projects/p/secrets/ascend-production/versions/1",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(AscendConfigurationError, "Production API execution"):
                AscendApiConfig.from_environment(Accessor())

    def test_worker_creates_program_then_billable_with_idempotency_metadata(self):
        transport = FakeTransport()
        client = AscendApiClient(transport)
        job = {"id": str(uuid4()), "action_type": ACTION_TYPE, "payload": {**payload(), "execute": True}}
        result = AscendCreateProgramWorker(lambda: client).perform(job, idempotency_key="key-1")
        self.assertTrue(result.succeeded)
        self.assertEqual(result.destination["program_id"], PROGRAM_ID)
        self.assertEqual(result.detail["billable_ids"], [BILLABLE_ID])
        self.assertEqual(transport.calls[0][0:2], ("POST", "/programs"))
        self.assertEqual(transport.calls[1][0:2], ("POST", "/billables"))
        self.assertEqual(transport.calls[1][3]["program_id"], PROGRAM_ID)
        self.assertEqual(transport.calls[0][3]["metadata"]["robie_idempotency_key"], "key-1")

    def test_worker_fails_closed_after_partial_creation(self):
        # A program whose billables fail must NEVER be reported as success:
        # the old code returned succeeded=True and the workflow composed an
        # "Agreement Ready" email for a program with no billables.
        class Partial(FakeTransport):
            def request(self, method, path, *, query=None, json_body=None):
                if method == "POST" and path == "/billables":
                    raise AscendApiError(422, "invalid quote")
                return super().request(method, path, query=query, json_body=json_body)

        client = AscendApiClient(Partial())
        job = {"id": str(uuid4()), "action_type": ACTION_TYPE, "payload": {**payload(), "execute": True}}
        result = AscendCreateProgramWorker(lambda: client).perform(job, idempotency_key="key-2")
        self.assertFalse(result.succeeded)
        self.assertFalse(result.retryable)
        self.assertEqual(result.destination["program_id"], PROGRAM_ID)
        self.assertIn("partial_failure", result.detail)
        self.assertIn("NOT ready", result.error or "")

    def test_find_or_create_insured_sends_mailing_address_and_primary_contact(self):
        # Ascend rejects new-insured creation without mailing_address_* and
        # insured_contacts. The workflow resolves both from the EZLynx record;
        # this asserts they actually reach the create payload.
        created = {}
        contact_created = {}

        class InsuredTransport(FakeTransport):
            def request(self, method, path, *, query=None, json_body=None):
                if method == "GET" and path == "/insureds":
                    return {"data": []}
                if method == "GET" and path == "/contacts":
                    return {"data": []}
                if method == "POST" and path == "/contacts":
                    contact_created.update(json_body or {})
                    return {"id": CONTACT_ID, **(json_body or {})}
                if method == "POST" and path == "/insureds":
                    created.update(json_body or {})
                    return {"id": INSURED_ID, **(json_body or {})}
                return super().request(method, path, query=query, json_body=json_body)

        client = AscendApiClient(InsuredTransport())
        insured_id, _ = client.find_or_create_insured(
            business_name="Acme Landscaping LLC",
            address={
                "mailing_address_street_one": "42 Riva Ave",
                "mailing_address_city": "North Brunswick",
                "mailing_address_state": "NJ",
                "mailing_address_zip_code": "08902",
            },
            contact={
                "first_name": "Mike",
                "last_name": "Fingerhut",
                "email": "mjfingerhut@gmail.com",
                "phone": "7322668111",
            },
        )
        self.assertEqual(insured_id, INSURED_ID)
        self.assertEqual(created["mailing_address_street_one"], "42 Riva Ave")
        self.assertEqual(created["mailing_address_city"], "North Brunswick")
        self.assertEqual(created["mailing_address_state"], "NJ")
        self.assertEqual(created["mailing_address_zip_code"], "08902")
        self.assertEqual(len(created["insured_contacts"]), 1)
        self.assertEqual(created["insured_contacts"][0]["id"], CONTACT_ID)
        # The contact itself was created with the EZLynx-resolved details.
        self.assertEqual(contact_created["first_name"], "Mike")
        self.assertEqual(contact_created["last_name"], "Fingerhut")
        self.assertEqual(contact_created["email"], "mjfingerhut@gmail.com")

    def test_verifier_freshly_reads_program_and_billable(self):
        transport = FakeTransport()
        client = AscendApiClient(transport)
        job = {"payload": payload()}
        action = {
            "destination": {"resource_id": PROGRAM_ID, "program_id": PROGRAM_ID},
            "detail": {"billable_ids": [BILLABLE_ID]},
        }
        result = AscendCreateProgramVerifier(lambda: client).verify(job, action)
        self.assertTrue(result.verified)
        self.assertTrue(result.evidence.authoritative)
        self.assertEqual(result.evidence.locator, PROGRAM_ID)
        self.assertEqual(
            result.evidence.observed["billables"][0]["agency_fees_cents"],
            50000,
        )

    def test_verifier_rejects_wrong_billable_business_values(self):
        class WrongPremium(FakeTransport):
            def request(self, method, path, *, query=None, json_body=None):
                result = super().request(
                    method, path, query=query, json_body=json_body
                )
                if method == "GET" and path == f"/billables/{BILLABLE_ID}":
                    result["premium_cents"] = 1
                return result

        client = AscendApiClient(WrongPremium())
        result = AscendCreateProgramVerifier(lambda: client).verify(
            {"payload": payload()},
            {
                "destination": {
                    "resource_id": PROGRAM_ID,
                    "program_id": PROGRAM_ID,
                },
                "detail": {"billable_ids": [BILLABLE_ID]},
            },
        )
        self.assertFalse(result.verified)
        self.assertIn("did not match", result.error or "")

    def test_execute_false_holds_without_loading_client(self):
        calls = []
        result = AscendCreateProgramWorker(lambda: calls.append(True)).perform(
            {"payload": payload()}, idempotency_key="key-3"
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.hold_status, JobStatus.NEEDS_CLARIFICATION)
        self.assertEqual(calls, [])

    def test_job_engine_completes_only_after_fresh_api_readback(self):
        transport = FakeTransport()
        client = AscendApiClient(transport)
        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            request_payload = {**payload(), "execute": True, "worker": "ascend-api"}
            job = store.create_job(ACTION_TYPE, request_payload, max_attempts=1)
            engine = JobEngine(
                store,
                {"ascend-api": AscendCreateProgramWorker(lambda: client)},
                {ACTION_TYPE: AscendCreateProgramVerifier(lambda: client)},
                enforce_recording_policy=True,
            )
            with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
                final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE.value)
        self.assertEqual([call[0] for call in transport.calls], ["POST", "POST", "GET", "GET"])


if __name__ == "__main__":
    unittest.main()
