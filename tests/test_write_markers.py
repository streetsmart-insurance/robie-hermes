"""Slice 4 step 1: worker-unforgeable write markers.

Every successful guarded Playwright write and every EZLynx / Ascend API
mutation records one ``write_markers`` row. Nothing reads the rows yet —
this step is inert by design. The tests prove the recording happens, that a
worker cannot reach the marker functions, and that marker failures never
break the write.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from robie_job_engine.playwright_write_guard import install_playwright_write_guards
from robie_job_engine.store import JobStore
from robie_job_engine.write_markers import (
    current_job_id,
    list_write_markers,
    record_write_marker,
    writes_observed,
)


class _FakePage:
    def __init__(self, url="https://example.com/form"):
        self.url = url


def _make_locator_class(*, delete_shaped=False):
    """Fresh stand-in per install: install wraps the class, so sharing one
    class across tests would double-wrap the methods."""

    class FreshFakeLocator:
        def __init__(self, page=None):
            self.page = page or _FakePage()
            self.filled = []

        def count(self):
            return 1

        def fill(self, value):
            self.filled.append(value)
            return "filled"

        def click(self):
            return "clicked"

    if delete_shaped:
        # A delete-shaped click is refused outright by the cardinal rule.
        FreshFakeLocator.get_attribute = (
            lambda self, name: "Delete policy" if name == "aria-label" else None
        )
        FreshFakeLocator.inner_text = lambda self: "Delete policy"
        FreshFakeLocator.text_content = lambda self: "Delete policy"

    return FreshFakeLocator


def _install_for(locator_cls):
    scope: dict = {"Locator": locator_cls}
    install_playwright_write_guards(scope)
    return scope


class WriteMarkerStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "jobs.db")
        self.store = JobStore(self.db)
        self.job = self.store.create_job("browser.read", {})
        self.job_id = self.job["id"]
        self._env = mock.patch.dict(
            os.environ,
            {"ROBIE_JOB_DB": self.db, "ROBIE_JOB_ID": self.job_id},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        os.environ.pop("ROBIE_JOB_ID", None)
        os.environ.pop("ROBIE_JOB_DB", None)
        self.tmp.cleanup()

    def test_record_write_marker_inserts_row(self):
        self.assertTrue(
            record_write_marker(method="fill", url="https://example.com/form")
        )
        markers = list_write_markers(self.store, self.job_id)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["method"], "fill")
        self.assertIn("example.com/form", markers[0]["url"])
        self.assertTrue(markers[0]["created_at"])
        self.assertEqual(writes_observed(self.store, self.job_id), 1)

    def test_record_write_marker_counts_every_write(self):
        for _ in range(3):
            self.assertTrue(record_write_marker(method="fill", url="https://x.test/"))
        self.assertEqual(writes_observed(self.store, self.job_id), 3)

    def test_no_job_id_records_nothing(self):
        os.environ.pop("ROBIE_JOB_ID", None)
        os.environ.pop("ROBIE_CURRENT_JOB_ID", None)
        os.environ.pop("JOB_ID", None)
        self.assertFalse(record_write_marker(method="fill", url="https://x.test/"))
        self.assertEqual(writes_observed(self.store, self.job_id), 0)

    def test_missing_db_records_nothing_and_never_raises(self):
        os.environ["ROBIE_JOB_DB"] = str(Path(self.tmp.name) / "nope.db")
        self.assertFalse(record_write_marker(method="fill", url="https://x.test/"))

    def test_unknown_job_id_records_nothing(self):
        os.environ["ROBIE_JOB_ID"] = "does-not-exist"
        self.assertFalse(record_write_marker(method="fill", url="https://x.test/"))
        self.assertEqual(writes_observed(self.store, self.job_id), 0)

    def test_secret_query_params_are_redacted(self):
        record_write_marker(
            method="fill",
            url="https://app.ezlynx.com/p?applicant=1&token=secret-value",
        )
        markers = list_write_markers(self.store, self.job_id)
        self.assertEqual(len(markers), 1)
        self.assertNotIn("secret-value", markers[0]["url"])

    def test_current_job_id_prefers_robie_job_id(self):
        os.environ["JOB_ID"] = "other"
        self.assertEqual(current_job_id(), self.job_id)


class GuardMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "jobs.db")
        self.store = JobStore(self.db)
        self.job_id = self.store.create_job("browser.read", {})["id"]
        self._env = mock.patch.dict(
            os.environ,
            {"ROBIE_JOB_DB": self.db, "ROBIE_JOB_ID": self.job_id},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        os.environ.pop("ROBIE_JOB_ID", None)
        os.environ.pop("ROBIE_JOB_DB", None)
        self.tmp.cleanup()

    def test_successful_guarded_write_records_marker(self):
        locator_cls = _make_locator_class()
        scope = _install_for(locator_cls)
        locator = locator_cls()
        self.assertEqual(locator.fill("hello"), "filled")
        markers = list_write_markers(self.store, self.job_id)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["method"], "fill")
        self.assertIn("example.com/form", markers[0]["url"])

    def test_marker_carries_page_url(self):
        locator_cls = _make_locator_class()
        scope = _install_for(locator_cls)
        locator = locator_cls(page=_FakePage("https://carrier.example.com/policy/123"))
        locator.fill("x")
        markers = list_write_markers(self.store, self.job_id)
        self.assertEqual(len(markers), 1)
        self.assertIn("carrier.example.com/policy/123", markers[0]["url"])

    def test_blocked_write_records_no_marker(self):
        # A delete-shaped click is refused outright: no write, no marker.
        locator_cls = _make_locator_class(delete_shaped=True)
        scope = _install_for(locator_cls)
        locator = locator_cls()
        with self.assertRaises(RuntimeError):
            locator.click()
        self.assertEqual(writes_observed(self.store, self.job_id), 0)

    def test_write_succeeds_with_no_job_env_and_records_nothing(self):
        os.environ.pop("ROBIE_JOB_ID", None)
        locator_cls = _make_locator_class()
        scope = _install_for(locator_cls)
        locator = locator_cls()
        # The write itself must not break when no job is bound.
        self.assertEqual(locator.fill("hello"), "filled")
        self.assertEqual(writes_observed(self.store, self.job_id), 0)

    def test_worker_scope_does_not_expose_marker_functions(self):
        scope = _install_for(_make_locator_class())
        blob = " ".join(str(key) for key in scope.keys())
        self.assertNotIn("write_marker", blob)
        self.assertNotIn("record_write", blob)
        for key in scope.keys():
            self.assertFalse(
                str(key).startswith("record_write"),
                f"worker-reachable scope key leaks marker function: {key}",
            )


class EzlynxApiMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "jobs.db")
        self.store = JobStore(self.db)
        self.job_id = self.store.create_job("ezlynx.reassign", {})["id"]
        self._env = mock.patch.dict(
            os.environ,
            {"ROBIE_JOB_DB": self.db, "ROBIE_JOB_ID": self.job_id},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        os.environ.pop("ROBIE_JOB_ID", None)
        os.environ.pop("ROBIE_JOB_DB", None)
        self.tmp.cleanup()

    def _client(self, payload):
        import json

        from robie_job_engine.ezlynx_api import EzlynxApiClient, EzlynxApiConfig

        body = json.dumps(payload).encode("utf-8")

        class FakeResp:
            def read(self):
                return body

        def fake_urlopen(url, *, data=None, headers=None, timeout=None):
            return FakeResp()

        config = EzlynxApiConfig(
            token_endpoint="https://auth.ezlynx.test/oauth/token",
            document_base_url="https://api.ezlynx.test",
            client_id="test-client",
            client_secret="test-secret",
            username="test-user",
            integration_group_id="test-group",
            scope="test-scope",
        )
        return EzlynxApiClient(config, urlopen=fake_urlopen)

    def test_api_post_records_marker(self):
        client = self._client({"status": "success", "data": []})
        client._request_json("POST", "https://api.ezlynx.test/documents", data=b"{}",
                             headers={})
        markers = list_write_markers(self.store, self.job_id)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["method"], "ezlynx_api.POST")

    def test_api_get_records_no_marker(self):
        client = self._client({"status": "success", "data": []})
        client._request_json("GET", "https://api.ezlynx.test/documents", data=None,
                             headers={})
        self.assertEqual(writes_observed(self.store, self.job_id), 0)

    def test_token_post_records_no_marker(self):
        import json

        from robie_job_engine.ezlynx_api import EzlynxApiClient, EzlynxApiConfig

        config = EzlynxApiConfig(
            token_endpoint="https://auth.ezlynx.test/oauth/token",
            document_base_url="https://api.ezlynx.test",
            client_id="test-client",
            client_secret="test-secret",
            username="test-user",
            integration_group_id="test-group",
            scope="test-scope",
        )
        body = json.dumps({"access_token": "tok", "expires_in": 3600}).encode()

        class FakeResp:
            def read(self):
                return body

        client = EzlynxApiClient(config, urlopen=lambda url, **kw: FakeResp())
        client._request_json("POST", config.token_endpoint, data=b"x", headers={})
        self.assertEqual(writes_observed(self.store, self.job_id), 0)


class AscendApiMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "jobs.db")
        self.store = JobStore(self.db)
        self.job_id = self.store.create_job("ascend.create_program", {})["id"]
        self._env = mock.patch.dict(
            os.environ,
            {"ROBIE_JOB_DB": self.db, "ROBIE_JOB_ID": self.job_id},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        os.environ.pop("ROBIE_JOB_ID", None)
        os.environ.pop("ROBIE_JOB_DB", None)
        self.tmp.cleanup()

    def _client(self):
        from robie_job_engine.ascend_api import AscendApiClient

        class FakeTransport:
            def request(self, method, path, **kwargs):
                return {
                    "data": {
                        "id": "11111111-2222-3333-4444-555555555555",
                        "type": "program",
                    }
                }

        return AscendApiClient(FakeTransport())

    def test_create_program_records_marker(self):
        client = self._client()
        client.create_program({"business_name": "Test"})
        markers = list_write_markers(self.store, self.job_id)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["method"], "ascend.create_program")

    def test_get_program_records_no_marker(self):
        client = self._client()
        client.get_program("11111111-2222-3333-4444-555555555555")
        self.assertEqual(writes_observed(self.store, self.job_id), 0)

    def test_create_billable_records_marker(self):
        client = self._client()
        client.create_billable({"premium_cents": 100})
        markers = list_write_markers(self.store, self.job_id)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["method"], "ascend.create_billable")


if __name__ == "__main__":
    unittest.main()
