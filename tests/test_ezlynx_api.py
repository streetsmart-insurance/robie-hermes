"""Unit tests for robie_job_engine.ezlynx_api.

All HTTP is mocked; no network, no secrets, no Secret Manager access.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch
from urllib import error

from robie_job_engine.ezlynx_api import (
    DOCUMENT_API_DOWNLOAD_PATH,
    DOCUMENT_API_SEARCH_PATH,
    DOCUMENT_API_UPLOAD_PATH,
    ENV_PROD_SECRET,
    ENV_UAT_SECRET,
    EzlynxApiClient,
    EzlynxApiConfig,
    EzlynxApiConfigurationError,
    EzlynxApiError,
    document_display_fields,
    extract_document_api_results,
    extract_document_records,
    is_vendor_document_api_username,
    load_ezlynx_api_config,
    parse_uploaded_document_id,
)
from robie_job_engine.ezlynx_api_read_port import EzlynxApiClientReadPort
from robie_job_engine.ezlynx_write_scope import EZLYNX_WRITE_SCOPE_REFUSED, EzlynxWriteScopeError


def _config() -> EzlynxApiConfig:
    return EzlynxApiConfig(
        token_endpoint="https://app.uatezlynx.com/auth/connect/token",
        document_base_url="https://app.uatezlynx.com/DocumentApi/",
        client_id="cid",
        client_secret="csecret",
        username="api_user",
        integration_group_id="183",
        scope="DocumentApi openid",
    )


class FakeResponse:
    def __init__(self, payload: bytes, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._payload


class FakeAccessor:
    def __init__(self, payload: dict):
        self._payload = payload

    def access(self, resource_name: str) -> str:
        assert resource_name.startswith("projects/")
        return json.dumps(self._payload)


def _payload(**overrides):
    base = {
        "client_id": "cid",
        "client_secret": "csecret",
        "username": "api_user",
        "integration_group_id": "183",
        "token_endpoint": "https://app.uatezlynx.com/auth/connect/token",
        "document_base_url": "https://app.uatezlynx.com/DocumentApi/",
        "scope": "DocumentApi openid",
    }
    base.update(overrides)
    return base


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self._old = dict(os.environ)
        os.environ.pop(ENV_UAT_SECRET, None)
        os.environ.pop(ENV_PROD_SECRET, None)
        os.environ.pop("ROBIE_ENV", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def test_test_env_selects_uat_secret(self):
        os.environ["ROBIE_ENV"] = "TEST"
        os.environ[ENV_UAT_SECRET] = "projects/p/secrets/ezlynx-api-uat/versions/latest"
        cfg = load_ezlynx_api_config(accessor=FakeAccessor(_payload()))
        self.assertIn("uatezlynx", cfg.token_endpoint)
        self.assertTrue(cfg.document_base_url.endswith("/"))

    def test_production_env_selects_prod_secret(self):
        os.environ["ROBIE_ENV"] = "PRODUCTION"
        os.environ[ENV_PROD_SECRET] = "projects/p/secrets/ezlynx-api-prod/versions/latest"
        cfg = load_ezlynx_api_config(accessor=FakeAccessor(_payload(
            token_endpoint="https://app.ezlynx.com/auth/connect/token",
            document_base_url="https://app.ezlynx.com/DocumentApi/",
        )))
        self.assertIn("app.ezlynx.com", cfg.token_endpoint)

    def test_unknown_env_rejected(self):
        os.environ["ROBIE_ENV"] = "STAGING"
        with self.assertRaises(EzlynxApiConfigurationError):
            load_ezlynx_api_config(accessor=FakeAccessor(_payload()))

    def test_missing_secret_env_rejected(self):
        os.environ["ROBIE_ENV"] = "TEST"
        with self.assertRaises(EzlynxApiConfigurationError):
            load_ezlynx_api_config(accessor=FakeAccessor(_payload()))

    def test_missing_field_rejected(self):
        os.environ["ROBIE_ENV"] = "TEST"
        os.environ[ENV_UAT_SECRET] = "projects/p/secrets/ezlynx-api-uat/versions/latest"
        bad = _payload()
        del bad["client_secret"]
        with self.assertRaises(EzlynxApiConfigurationError) as ctx:
            load_ezlynx_api_config(accessor=FakeAccessor(bad))
        self.assertIn("client_secret", str(ctx.exception))

    def test_bad_json_rejected(self):
        os.environ["ROBIE_ENV"] = "TEST"
        os.environ[ENV_UAT_SECRET] = "projects/p/secrets/ezlynx-api-uat/versions/latest"

        class BadAccessor:
            def access(self, resource_name: str) -> str:
                return "not-json{"

        with self.assertRaises(EzlynxApiConfigurationError):
            load_ezlynx_api_config(accessor=BadAccessor())

    def test_repr_redacts_everything(self):
        text = repr(_config())
        for secret in ("cid", "csecret", "api_user", "183", "uatezlynx"):
            self.assertNotIn(secret, text)


class ClientAuthTests(unittest.TestCase):
    def test_token_cached_until_expiry(self):
        calls = []

        def fake_urlopen(url, *, data, headers, timeout):
            calls.append(url)
            body = json.dumps(
                {"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}
            ).encode()
            return FakeResponse(body)

        now = [1000.0]
        client = EzlynxApiClient(_config(), urlopen=fake_urlopen, clock=lambda: now[0])
        self.assertEqual(client.get_token(), "tok-1")
        self.assertEqual(client.get_token(), "tok-1")
        self.assertEqual(len(calls), 1)
        now[0] += 4000.0  # past expiry
        self.assertEqual(client.get_token(), "tok-1")
        self.assertEqual(len(calls), 2)

    def test_token_request_posts_vendor_grant(self):
        seen = {}

        def fake_urlopen(url, *, data, headers, timeout):
            seen["url"] = url
            seen["headers"] = headers
            seen["data"] = data.decode()
            return FakeResponse(json.dumps({"access_token": "t", "expires_in": 60}).encode())

        client = EzlynxApiClient(_config(), urlopen=fake_urlopen)
        client.get_token()
        self.assertEqual(seen["url"], "https://app.uatezlynx.com/auth/connect/token")
        self.assertIn("grant_type=vendor_data_access", seen["data"])
        self.assertIn("integration_group_id=183", seen["data"])
        self.assertEqual(
            seen["headers"]["Content-Type"], "application/x-www-form-urlencoded"
        )

    def test_missing_access_token_raises(self):
        def fake_urlopen(url, *, data, headers, timeout):
            return FakeResponse(json.dumps({"token_type": "Bearer"}).encode())

        client = EzlynxApiClient(_config(), urlopen=fake_urlopen)
        with self.assertRaises(EzlynxApiError):
            client.get_token()

    def test_http_error_sanitized_and_flagged(self):
        def fake_urlopen(url, *, data, headers, timeout):
            raise error.HTTPError(
                url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad"}')
            )

        client = EzlynxApiClient(_config(), urlopen=fake_urlopen)
        with self.assertRaises(EzlynxApiError) as ctx:
            client.get_token()
        self.assertEqual(ctx.exception.status, 401)
        self.assertFalse(ctx.exception.retryable)
        # secrets must not leak into the message
        self.assertNotIn("csecret", str(ctx.exception))

    def test_server_error_is_retryable(self):
        def fake_urlopen(url, *, data, headers, timeout):
            raise error.HTTPError(url, 503, "Down", {}, io.BytesIO(b""))

        client = EzlynxApiClient(_config(), urlopen=fake_urlopen)
        with self.assertRaises(EzlynxApiError) as ctx:
            client.get_token()
        self.assertTrue(ctx.exception.retryable)

    def test_api_get_uses_bearer_and_base_url(self):
        seen = {}

        def fake_urlopen(url, *, data, headers, timeout):
            if "connect/token" in url:
                return FakeResponse(
                    json.dumps({"access_token": "tok-9", "expires_in": 3600}).encode()
                )
            seen["url"] = url
            seen["auth"] = headers.get("Authorization")
            return FakeResponse(json.dumps({"items": []}).encode())

        client = EzlynxApiClient(_config(), urlopen=fake_urlopen)
        result = client.api_get("documents", query={"policyId": "123"})
        self.assertEqual(result, {"items": []})
        self.assertTrue(
            seen["url"].startswith("https://app.uatezlynx.com/DocumentApi/documents?")
        )
        self.assertIn("policyId=123", seen["url"])
        self.assertEqual(seen["auth"], "Bearer tok-9")


def _classic_config() -> EzlynxApiConfig:
    return EzlynxApiConfig(
        token_endpoint="https://app.uatezlynx.com/auth/connect/token",
        document_base_url="https://app.uatezlynx.com/DocumentApi/",
        client_id="cid",
        client_secret="csecret",
        username="api_user",
        integration_group_id="183",
        scope="DocumentApi PolicyApi openid",
        classic_base_url="https://app.uatezlynx.com/ezlynxapi/",
        ez_token="eztok",
        ez_app_secret="ezsecret",
        account_username="classic_user",
    )


class DocumentHelperTests(unittest.TestCase):
    def test_extracts_records_and_display_name(self):
        payload = {
            "Records": [
                {"Description": "Bond - Western Surety.pdf", "PolicyId": 1},
            ]
        }
        rows = extract_document_records(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            document_display_fields(rows[0])["name"],
            "Bond - Western Surety.pdf",
        )


class DestinationReadClientTests(unittest.TestCase):
    def test_search_policy_uses_oauth_policyapi(self):
        seen = {}

        def fake_urlopen(url, *, data, headers, timeout):
            if "connect/token" in url:
                return FakeResponse(
                    json.dumps({"access_token": "tok-p", "expires_in": 3600}).encode()
                )
            seen["url"] = url
            seen["auth"] = headers.get("Authorization")
            return FakeResponse(
                json.dumps({"Policies": [{"PolicyNumber": "73834086"}]}).encode()
            )

        client = EzlynxApiClient(_classic_config(), urlopen=fake_urlopen)
        result = client.search_policy_by_number("73834086")
        self.assertEqual(result["status"], "success")
        self.assertIn("/PolicyApi/policy/v1/search", seen["url"])
        self.assertIn("PolicyNumber=73834086", seen["url"])
        self.assertEqual(seen["auth"], "Bearer tok-p")

    def test_name_and_email_search_is_a_read_only_policyapi_get(self):
        seen = {}

        def fake_urlopen(url, *, data, headers, timeout):
            if "connect/token" in url:
                return FakeResponse(
                    json.dumps({"access_token": "tok-p", "expires_in": 3600}).encode()
                )
            seen["url"] = url
            seen["data"] = data
            return FakeResponse(json.dumps({"results": [], "totalSize": 0}).encode())

        client = EzlynxApiClient(_classic_config(), urlopen=fake_urlopen)
        client.search_applicants_by_name_and_email(
            "Fixture Hauling LLC", "insured@example.test"
        )
        self.assertIn("/PolicyApi/policy/v1/search", seen["url"])
        self.assertIn("ApplicantName=Fixture", seen["url"])
        self.assertIn("Email=insured%40example.test", seen["url"])
        self.assertIsNone(seen["data"])

    def test_name_email_and_phone_searches_are_read_only_gets(self):
        seen = {}

        def fake_urlopen(url, *, data, headers, timeout):
            if "connect/token" in url:
                return FakeResponse(
                    json.dumps({"access_token": "tok-p", "expires_in": 3600}).encode()
                )
            seen["url"] = url
            seen["data"] = data
            return FakeResponse(json.dumps({"results": [], "totalSize": 0}).encode())

        client = EzlynxApiClient(_classic_config(), urlopen=fake_urlopen)
        client.search_applicants_by_name("Fixture Hauling LLC")
        self.assertIn("/PolicyApi/policy/v1/search", seen["url"])
        self.assertIn("ApplicantName=Fixture", seen["url"])
        self.assertNotIn("Email=", seen["url"])
        self.assertIsNone(seen["data"])

        client.search_applicants_by_email("insured@example.test")
        self.assertIn("Email=insured%40example.test", seen["url"])
        self.assertNotIn("ApplicantName=", seen["url"])
        self.assertIsNone(seen["data"])

        client.search_applicants_by_phone("555-010-0199")
        self.assertIn("PhoneNumber=555-010-0199", seen["url"])
        self.assertIsNone(seen["data"])

    def test_list_documents_uses_classic_eztoken_not_documentapi_host(self):
        seen = {}

        def fake_urlopen(url, *, data, headers, timeout):
            seen["url"] = url
            seen["headers"] = headers
            return FakeResponse(json.dumps({"Records": []}).encode())

        client = EzlynxApiClient(_classic_config(), urlopen=fake_urlopen)
        result = client.list_applicant_documents("194066748", policy_id=0)
        self.assertEqual(result, {"Records": []})
        self.assertIn("/ezlynxapi/api/documentlibrary/list/194066748/1/200/0", seen["url"])
        self.assertNotIn("DocumentApi", seen["url"])
        self.assertEqual(seen["headers"]["EZToken"], "eztok")
        self.assertNotIn("Authorization", seen["headers"])

    def test_list_documents_errors_clearly_when_classic_auth_missing(self):
        client = EzlynxApiClient(_config(), urlopen=lambda *a, **k: FakeResponse(b"{}"))
        with self.assertRaises(EzlynxApiConfigurationError) as ctx:
            client.list_applicant_documents("194066748")
        self.assertIn("classic EZLynx document library auth is not configured", str(ctx.exception))

    def test_discussions_soft_empty_when_oauth_forbidden(self):
        def fake_urlopen(url, *, data, headers, timeout):
            if "connect/token" in url:
                return FakeResponse(
                    json.dumps({"access_token": "tok-d", "expires_in": 3600}).encode()
                )
            raise error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))

        client = EzlynxApiClient(_classic_config(), urlopen=fake_urlopen)
        self.assertEqual(client.get_applicant_discussions("194066748"), [])


AGENCY_USER = "SSRobie"
VENDOR_USER = "ssr_userPROD"
TEST_APPLICANT = "220250093"
LIVE_APPLICANT = "194066748"
UPLOADED_ID = "818921949"


def _agency_config(**overrides) -> EzlynxApiConfig:
    values = dict(
        token_endpoint="https://app.ezlynx.com/auth/connect/token",
        document_base_url="https://app.ezlynx.com/DocumentApi/",
        client_id="cid",
        client_secret="csecret",
        username=AGENCY_USER,
        integration_group_id="183",
        scope="DocumentApi PolicyApi openid",
        vendor_username=VENDOR_USER,
    )
    values.update(overrides)
    return EzlynxApiConfig(**values)


def _token_then(handler):
    def fake_urlopen(url, *, data, headers, timeout):
        if "connect/token" in url:
            return FakeResponse(
                json.dumps({"access_token": "tok-doc", "expires_in": 3600}).encode()
            )
        return handler(url, data=data, headers=headers, timeout=timeout)

    return fake_urlopen


class DocumentApiHelperTests(unittest.TestCase):
    def test_extracts_results_id_and_ignores_document_url(self):
        payload = {
            "totalSize": 34,
            "results": [
                {
                    "id": 818921949,
                    "documentUrl": "https://old-wrong.example/Document/818921949",
                    "documentName": "Gazala-test.pdf",
                },
                {
                    "documentUrl": "https://old-wrong.example/Document/1",
                    "name": "no-id.pdf",
                },
            ],
        }
        rows = extract_document_api_results(payload)
        self.assertEqual(rows, [{"id": UPLOADED_ID, "name": "Gazala-test.pdf"}])
        self.assertTrue(all("documentUrl" not in row for row in rows))

    def test_parse_upload_body_is_numeric_id_only(self):
        self.assertEqual(parse_uploaded_document_id(b"818921949"), UPLOADED_ID)
        self.assertEqual(parse_uploaded_document_id(b'"818921949"'), UPLOADED_ID)
        with self.assertRaises(EzlynxApiError):
            parse_uploaded_document_id(
                b'{"documentUrl":"https://old-wrong.example/Document/1"}'
            )

    def test_vendor_username_is_rejected(self):
        self.assertTrue(is_vendor_document_api_username(VENDOR_USER))
        self.assertTrue(is_vendor_document_api_username("ssr_userUAT"))
        self.assertFalse(is_vendor_document_api_username(AGENCY_USER))
        self.assertFalse(is_vendor_document_api_username("Carlo1"))


class DocumentApiClientTests(unittest.TestCase):
    def test_token_uses_agency_username_never_vendor_username_or_password(self):
        seen = {}

        def fake_urlopen(url, *, data, headers, timeout):
            seen["url"] = url
            seen["data"] = data.decode()
            return FakeResponse(json.dumps({"access_token": "t", "expires_in": 60}).encode())

        client = EzlynxApiClient(_agency_config(), urlopen=fake_urlopen)
        client.get_token()
        self.assertIn(f"username={AGENCY_USER}", seen["data"])
        self.assertIn("grant_type=vendor_data_access", seen["data"])
        self.assertNotIn(VENDOR_USER, seen["data"])
        self.assertNotIn("vendor_username", seen["data"])
        self.assertNotIn("password=", seen["data"])

    def test_token_refuses_vendor_user_before_http(self):
        calls = []

        def fake_urlopen(url, *, data, headers, timeout):
            calls.append(url)
            return FakeResponse(b"{}")

        client = EzlynxApiClient(
            _agency_config(username=VENDOR_USER), urlopen=fake_urlopen
        )
        with self.assertRaises(EzlynxApiConfigurationError) as ctx:
            client.get_token()
        self.assertIn("agency username", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_search_uses_proven_path_and_results_id(self):
        seen = {}

        def handler(url, *, data, headers, timeout):
            seen["url"] = url
            seen["auth"] = headers.get("Authorization")
            return FakeResponse(
                json.dumps(
                    {
                        "totalSize": 1,
                        "results": [
                            {
                                "id": int(UPLOADED_ID),
                                "documentUrl": "https://old-wrong.example/x",
                                "name": "applied.pdf",
                            }
                        ],
                    }
                ).encode()
            )

        client = EzlynxApiClient(_agency_config(), urlopen=_token_then(handler))
        payload = client.search_applicant_documents(TEST_APPLICANT)
        self.assertEqual(
            seen["url"],
            "https://app.ezlynx.com"
            + DOCUMENT_API_SEARCH_PATH.format(applicant_id=TEST_APPLICANT),
        )
        self.assertIn("/documentapi/documents/v1/account/", seen["url"])
        self.assertNotIn("documentUrl", seen["url"])
        self.assertEqual(seen["auth"], "Bearer tok-doc")
        rows = extract_document_api_results(payload)
        self.assertEqual(rows[0]["id"], UPLOADED_ID)

    def test_download_uses_document_id_not_document_url(self):
        seen = {}

        def handler(url, *, data, headers, timeout):
            seen["url"] = url
            return FakeResponse(
                b"%PDF-1.4 dest",
                headers={"Content-Type": "application/pdf"},
            )

        client = EzlynxApiClient(_agency_config(), urlopen=_token_then(handler))
        downloaded = client.download_document(UPLOADED_ID)
        self.assertEqual(
            seen["url"],
            "https://app.ezlynx.com"
            + DOCUMENT_API_DOWNLOAD_PATH.format(document_id=UPLOADED_ID),
        )
        self.assertNotIn("documentUrl", seen["url"])
        self.assertEqual(downloaded.body, b"%PDF-1.4 dest")
        self.assertEqual(downloaded.content_type, "application/pdf")

    def test_download_rejects_document_url_as_id(self):
        client = EzlynxApiClient(
            _agency_config(), urlopen=lambda *a, **k: FakeResponse(b"")
        )
        with self.assertRaises(EzlynxApiError):
            client.download_document("https://old-wrong.example/Document/1")

    def test_search_403_as_vendor_is_not_retryable(self):
        def handler(url, *, data, headers, timeout):
            raise error.HTTPError(
                url, 403, "Forbidden", {}, io.BytesIO(b"vendor cannot read applicant")
            )

        client = EzlynxApiClient(_agency_config(), urlopen=_token_then(handler))
        with self.assertRaises(EzlynxApiError) as ctx:
            client.search_applicant_documents(TEST_APPLICANT)
        self.assertEqual(ctx.exception.status, 403)
        self.assertFalse(ctx.exception.retryable)

    def test_upload_posts_proven_multipart_and_parses_numeric_id(self):
        seen = {}

        def handler(url, *, data, headers, timeout):
            seen["url"] = url
            seen["headers"] = headers
            seen["data"] = data
            return FakeResponse(b"818921949", headers={"Content-Type": "text/plain"})

        client = EzlynxApiClient(_agency_config(), urlopen=_token_then(handler))
        doc_id = client.upload_applicant_document(
            TEST_APPLICANT,
            "abcc",
            b"hello dest",
            filename="Testing.txt",
            file_content_type="text/plain",
        )
        self.assertEqual(doc_id, UPLOADED_ID)
        self.assertEqual(
            seen["url"],
            "https://app.ezlynx.com"
            + DOCUMENT_API_UPLOAD_PATH.format(applicant_id=TEST_APPLICANT),
        )
        self.assertIn("multipart/form-data", seen["headers"]["Content-Type"])
        body = seen["data"]
        self.assertIn(b'name="DocumentName"', body)
        self.assertIn(b"abcc", body)
        self.assertIn(b'name="File"', body)
        self.assertIn(b'filename="Testing.txt"', body)
        self.assertIn(b"hello dest", body)
        self.assertIn(b'name="PolicyMasterId"', body)
        self.assertIn(b"0", body)
        self.assertEqual(seen["headers"]["Authorization"], "Bearer tok-doc")

    def test_upload_uses_job_policy_master_id_when_provided(self):
        seen = {}

        def handler(url, *, data, headers, timeout):
            seen["data"] = data
            return FakeResponse(b"120128317")

        client = EzlynxApiClient(_agency_config(), urlopen=_token_then(handler))
        client.upload_applicant_document(
            TEST_APPLICANT, "abcc", b"x", policy_master_id="83184565"
        )
        self.assertIn(b"83184565", seen["data"])

    def test_upload_refuses_live_applicant_without_http_when_allowlist_restricted(self):
        calls = []

        def fake_urlopen(url, *, data, headers, timeout):
            calls.append(url)
            return FakeResponse(b"ok")

        client = EzlynxApiClient(_agency_config(), urlopen=fake_urlopen)
        with patch(
            "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
            frozenset({TEST_APPLICANT}),
        ):
            with self.assertRaises(EzlynxWriteScopeError) as ctx:
                client.upload_applicant_document(LIVE_APPLICANT, "nope.pdf", b"x")
        self.assertIn(EZLYNX_WRITE_SCOPE_REFUSED, str(ctx.exception))
        self.assertEqual(calls, [])

    def test_upload_allows_live_applicant_when_allowlist_unset(self):
        seen = {}

        def handler(url, *, data, headers, timeout):
            seen["url"] = url
            return FakeResponse(b"818921949", headers={"Content-Type": "text/plain"})

        client = EzlynxApiClient(_agency_config(), urlopen=_token_then(handler))
        with patch(
            "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
            None,
        ):
            doc_id = client.upload_applicant_document(LIVE_APPLICANT, "note.pdf", b"x")
        self.assertEqual(doc_id, UPLOADED_ID)
        self.assertIn(LIVE_APPLICANT, seen["url"])

    def test_uat_search_uses_uatezlynx_host(self):
        seen = {}

        def handler(url, *, data, headers, timeout):
            seen["url"] = url
            return FakeResponse(json.dumps({"results": []}).encode())

        client = EzlynxApiClient(
            _agency_config(
                token_endpoint="https://app.uatezlynx.com/auth/connect/token",
                document_base_url="https://app.uatezlynx.com/DocumentApi/",
            ),
            urlopen=_token_then(handler),
        )
        client.search_applicant_documents(TEST_APPLICANT)
        self.assertTrue(seen["url"].startswith("https://app.uatezlynx.com/documentapi/"))


class DocumentApiReadPortTests(unittest.TestCase):
    def test_port_lists_search_ids_and_downloads_by_id(self):
        calls = []

        class FakeClient:
            def search_applicant_documents(self, applicant_id):
                calls.append(("search", applicant_id))
                return {
                    "results": [
                        {
                            "id": UPLOADED_ID,
                            "documentUrl": "https://old-wrong.example/x",
                            "name": "applied.pdf",
                        }
                    ]
                }

            def download_document(self, document_id):
                calls.append(("download", document_id))
                return type("D", (), {"body": b"%PDF-1.4 dest", "content_type": "application/pdf"})()

        port = EzlynxApiClientReadPort(FakeClient())
        rows = port.documents_for_applicant(TEST_APPLICANT)
        self.assertEqual(rows, [{"id": UPLOADED_ID, "name": "applied.pdf"}])
        self.assertEqual(port.download_document(UPLOADED_ID), b"%PDF-1.4 dest")
        self.assertEqual(calls, [("search", TEST_APPLICANT), ("download", UPLOADED_ID)])

    def test_port_never_exposes_document_url_as_id(self):
        class FakeClient:
            def search_applicant_documents(self, applicant_id):
                return {
                    "results": [
                        {"documentUrl": "https://old-wrong.example/x", "name": "x.pdf"}
                    ]
                }

        self.assertEqual(
            EzlynxApiClientReadPort(FakeClient()).documents_for_applicant(TEST_APPLICANT),
            [],
        )


class DocumentApiConfigLoadTests(unittest.TestCase):
    def setUp(self):
        self._old = dict(os.environ)
        os.environ.pop(ENV_UAT_SECRET, None)
        os.environ.pop(ENV_PROD_SECRET, None)
        os.environ["ROBIE_ENV"] = "PRODUCTION"
        os.environ[ENV_PROD_SECRET] = "projects/p/secrets/ezlynx-api-prod/versions/latest"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)

    def test_loads_vendor_username_but_keeps_agency_username(self):
        cfg = load_ezlynx_api_config(
            accessor=FakeAccessor(
                {
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "username": AGENCY_USER,
                    "vendor_username": VENDOR_USER,
                    "password": "vendor-password-not-for-ssrobie",
                    "integration_group_id": "183",
                    "token_endpoint": "https://app.ezlynx.com/auth/connect/token",
                    "document_base_url": "https://app.ezlynx.com/DocumentApi/",
                    "scope": "DocumentApi PolicyApi openid",
                }
            )
        )
        self.assertEqual(cfg.username, AGENCY_USER)
        self.assertEqual(cfg.vendor_username, VENDOR_USER)
        self.assertFalse(hasattr(cfg, "password") and getattr(cfg, "password", ""))


if __name__ == "__main__":
    unittest.main()
