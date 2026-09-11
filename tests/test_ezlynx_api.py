"""Unit tests for robie_job_engine.ezlynx_api.

All HTTP is mocked; no network, no secrets, no Secret Manager access.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from urllib import error

from robie_job_engine.ezlynx_api import (
    ENV_PROD_SECRET,
    ENV_UAT_SECRET,
    EzlynxApiClient,
    EzlynxApiConfig,
    EzlynxApiConfigurationError,
    EzlynxApiError,
    document_display_fields,
    extract_document_records,
    load_ezlynx_api_config,
)


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
    def __init__(self, payload: bytes):
        self._payload = payload

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


if __name__ == "__main__":
    unittest.main()
