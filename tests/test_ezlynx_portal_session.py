"""Session-cookie Portal helpers. No network, no Playwright, no secrets."""

from __future__ import annotations

import io
import json
import unittest
from urllib import error

from robie_job_engine.ezlynx_api import EzlynxApiError
from robie_job_engine.ezlynx_org_labels import note_labels_path
from robie_job_engine.ezlynx_portal_session import (
    AUTH_PATH_CDP_SESSION,
    AUTH_PATH_OAUTH_BEARER,
    format_cookie_header,
    is_ezlynx_cookie_domain,
    load_cdp_session_cookie_header,
    portal_session_headers,
    portal_session_json,
)


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self._status = status

    def read(self) -> bytes:
        return self._payload

    def getcode(self) -> int:
        return self._status


class CookieHeaderTests(unittest.TestCase):
    def test_keeps_ezlynx_cookies_and_drops_other_hosts(self):
        header = format_cookie_header(
            [
                {"name": "sid", "value": "ez-session", "domain": ".ezlynx.com"},
                {"name": "other", "value": "nope", "domain": "example.com"},
                {"name": "uat", "value": "uat-session", "domain": "app.uatezlynx.com"},
            ]
        )
        self.assertIn("sid=ez-session", header)
        self.assertIn("uat=uat-session", header)
        self.assertNotIn("nope", header)

    def test_ezlynx_domain_matcher(self):
        self.assertTrue(is_ezlynx_cookie_domain(".app.ezlynx.com"))
        self.assertTrue(is_ezlynx_cookie_domain("uatezlynx.com"))
        self.assertFalse(is_ezlynx_cookie_domain("evil.com"))


class PortalSessionHeaderTests(unittest.TestCase):
    def test_session_headers_have_cookie_and_no_bearer(self):
        headers = portal_session_headers(
            "sid=ez-session",
            "https://app.ezlynx.com/DocumentApi/",
        )
        self.assertEqual(headers["Cookie"], "sid=ez-session")
        self.assertEqual(headers["Origin"], "https://app.ezlynx.com")
        self.assertNotIn("Authorization", headers)
        self.assertFalse(any(k.casefold() == "x-ezlynx-user" for k in headers))

    def test_empty_cookie_is_fail_closed(self):
        with self.assertRaises(EzlynxApiError):
            portal_session_headers("", "https://app.ezlynx.com")


class PortalSessionJsonTests(unittest.TestCase):
    def test_success_posts_json(self):
        seen = {}

        def fake_urlopen(url, *, data, headers, timeout):
            seen["url"] = url
            seen["data"] = json.loads(data.decode("utf-8"))
            seen["headers"] = dict(headers)
            return FakeResponse(json.dumps({"applied": True}).encode())

        parsed = portal_session_json(
            fake_urlopen,
            "POST",
            "https://app.ezlynx.com" + note_labels_path("1128873902"),
            {"organizationLabelIds": ["110248"]},
            {"Cookie": "sid=ez-session", "Accept": "application/json"},
        )
        self.assertEqual(parsed, {"applied": True})
        self.assertIn("/Notes/1128873902/OrganizationLabels", seen["url"])
        self.assertEqual(seen["data"]["organizationLabelIds"], ["110248"])
        self.assertNotIn("Authorization", seen["headers"])

    def test_http_error_403_is_fail_closed_and_redacted(self):
        def fake_urlopen(url, *, data, headers, timeout):
            raise error.HTTPError(
                url, 403, "Forbidden", {}, io.BytesIO(b"cookie=ez-session leaked")
            )

        with self.assertRaises(EzlynxApiError) as caught:
            portal_session_json(
                fake_urlopen,
                "POST",
                "https://app.ezlynx.com/EZLynxPortalAPI/Notes/1/OrganizationLabels",
                {"organizationLabelIds": ["110248"]},
                {"Cookie": "sid=ez-session"},
            )
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("ez-session", str(caught.exception))
        self.assertNotIn("leaked", str(caught.exception))

    def test_http_client_style_403_status_is_fail_closed(self):
        def fake_urlopen(url, *, data, headers, timeout):
            return FakeResponse(b'{"error":"forbidden"}', status=403)

        with self.assertRaises(EzlynxApiError) as caught:
            portal_session_json(
                fake_urlopen,
                "POST",
                "https://app.ezlynx.com/EZLynxPortalAPI/Notes/1/OrganizationLabels",
                {"organizationLabelIds": ["110248"]},
                {"Cookie": "sid=ez-session"},
            )
        self.assertEqual(caught.exception.status, 403)


class CdpCookieLoaderTests(unittest.TestCase):
    def test_injected_connect_builds_header(self):
        header = load_cdp_session_cookie_header(
            connect=lambda _url: [
                {"name": "sid", "value": "from-cdp", "domain": "app.ezlynx.com"}
            ]
        )
        self.assertEqual(header, "sid=from-cdp")

    def test_empty_cdp_cookies_fail_closed(self):
        with self.assertRaises(EzlynxApiError):
            load_cdp_session_cookie_header(connect=lambda _url: [])

    def test_auth_path_constants_document_working_vs_broken(self):
        self.assertEqual(AUTH_PATH_CDP_SESSION, "cdp_session_cookie")
        self.assertEqual(AUTH_PATH_OAUTH_BEARER, "oauth_bearer")
        self.assertNotEqual(AUTH_PATH_CDP_SESSION, AUTH_PATH_OAUTH_BEARER)


if __name__ == "__main__":
    unittest.main()
