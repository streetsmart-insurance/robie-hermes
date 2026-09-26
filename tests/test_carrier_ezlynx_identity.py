"""Unit tests for EZLynx carrier directory identity resolution. No network."""

from __future__ import annotations

import os
import sys
import unittest

sys.modules.pop("robie_job_engine.verification_common", None)

from robie_job_engine import carrier_channel_routing as ccr


class EzlynxIdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        ccr.set_ezlynx_carrier_lookup(None)
        os.environ.pop(ccr.EZLYNX_CARRIER_LOOKUP_DISABLE_ENV, None)

    def tearDown(self):
        ccr.set_ezlynx_carrier_lookup(None)
        os.environ.pop(ccr.EZLYNX_CARRIER_LOOKUP_DISABLE_ENV, None)

    def test_no_lookup_configured_falls_back_to_json(self):
        # No EZLynx client wired -> original JSON behavior preserved.
        config = ccr.route_carrier("Coterie")
        self.assertEqual(config["channel"], "PORTAL")

    def test_ezlynx_hit_takes_precedence_for_identity(self):
        # FMI lesson: "FMI" must resolve via EZLynx, not via JSON fuzzy guess.
        ccr.set_ezlynx_carrier_lookup(
            lambda name: ccr.EzlynxCarrierHit(
                canonical_name="Franklin Mutual Insurance Company", raw={}
            )
            if name == "FMI"
            else None
        )
        hit = ccr.resolve_carrier_via_ezlynx("FMI")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.canonical_name, "Franklin Mutual Insurance Company")

    def test_ezlynx_canonical_name_routes_via_json(self):
        # EZLynx resolves identity; JSON still provides the channel.
        ccr.set_ezlynx_carrier_lookup(
            lambda name: ccr.EzlynxCarrierHit(canonical_name="Coterie", raw={})
        )
        config = ccr.route_carrier("coterie misspelled c0terie")
        self.assertEqual(config["channel"], "PORTAL")
        self.assertIn("portal_url", config)

    def test_ezlynx_miss_falls_back_to_json_fuzzy(self):
        ccr.set_ezlynx_carrier_lookup(lambda name: None)
        config = ccr.route_carrier("Hartford")
        self.assertEqual(config["channel"], "PORTAL")

    def test_ezlynx_error_falls_back_safely(self):
        def boom(name):
            raise RuntimeError("directory exploded")

        ccr.set_ezlynx_carrier_lookup(boom)
        # Must not raise; JSON fallback keeps the worker moving.
        config = ccr.route_carrier("Coterie")
        self.assertEqual(config["channel"], "PORTAL")

    def test_ezlynx_resolved_but_no_channel_entry_defaults_email(self):
        ccr.set_ezlynx_carrier_lookup(
            lambda name: ccr.EzlynxCarrierHit(
                canonical_name="Some Carrier With No Channel Entry", raw={}
            )
        )
        config = ccr.route_carrier("whatever")
        self.assertEqual(config["channel"], "EMAIL")

    def test_lookup_can_be_disabled_via_env(self):
        ccr.set_ezlynx_carrier_lookup(
            lambda name: ccr.EzlynxCarrierHit(canonical_name="Coterie", raw={})
        )
        os.environ[ccr.EZLYNX_CARRIER_LOOKUP_DISABLE_ENV] = "1"
        self.assertIsNone(ccr.resolve_carrier_via_ezlynx("Coterie"))

    def test_blank_name_never_hits_ezlynx(self):
        calls = []
        ccr.set_ezlynx_carrier_lookup(
            lambda name: calls.append(name) or None
        )
        self.assertIsNone(ccr.resolve_carrier_via_ezlynx(""))
        self.assertIsNone(ccr.resolve_carrier_via_ezlynx(None))
        self.assertEqual(calls, [])


class BuildLookupTests(unittest.TestCase):
    def test_build_lookup_uses_api_get(self):
        seen = {}

        class FakeClient:
            def api_get(self, path, query=None):
                seen["path"] = path
                seen["query"] = query
                return {"data": [{"name": "Franklin Mutual Insurance Company"}]}

        lookup = ccr.build_ezlynx_carrier_lookup(FakeClient())
        hit = lookup("FMI")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.canonical_name, "Franklin Mutual Insurance Company")
        self.assertIn("CarrierApi", seen["path"])

    def test_build_lookup_empty_response_returns_none(self):
        class FakeClient:
            def api_get(self, path, query=None):
                return {"data": []}

        lookup = ccr.build_ezlynx_carrier_lookup(FakeClient())
        self.assertIsNone(lookup("FMI"))

    def test_build_lookup_respects_path_override(self):
        seen = {}

        class FakeClient:
            def api_get(self, path, query=None):
                seen["path"] = path
                return {"data": []}

        os.environ[ccr.EZLYNX_CARRIER_API_PATH_ENV] = "/CustomApi/v9/carriers"
        try:
            lookup = ccr.build_ezlynx_carrier_lookup(FakeClient())
            lookup("FMI")
            self.assertEqual(seen["path"], "/CustomApi/v9/carriers")
        finally:
            os.environ.pop(ccr.EZLYNX_CARRIER_API_PATH_ENV, None)


if __name__ == "__main__":
    unittest.main()
