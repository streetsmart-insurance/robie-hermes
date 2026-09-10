"""Unit tests for robie_job_engine.carrier_channel_routing. No network."""

from __future__ import annotations

import sys
import unittest

sys.modules.pop("robie_job_engine.verification_common", None)

from robie_job_engine import carrier_channel_routing as ccr


class ChannelRoutingTests(unittest.TestCase):
    def test_directory_has_29_carriers(self):
        directory = ccr.load_channel_directory()
        self.assertEqual(len(directory), 29)

    def test_all_channels_valid(self):
        for name, entry in ccr.load_channel_directory().items():
            self.assertIn(
                entry["channel"], ccr.CHANNELS, f"carrier {name} has bad channel"
            )

    def test_exact_match(self):
        config = ccr.route_carrier("Coterie")
        self.assertEqual(config["channel"], "PORTAL")
        self.assertIn("portal_url", config)

    def test_fuzzy_match_case_insensitive(self):
        config = ccr.route_carrier("coterie")
        self.assertEqual(config["channel"], "PORTAL")

    def test_fuzzy_match_partial(self):
        config = ccr.route_carrier("Hartford")
        self.assertEqual(config["channel"], "PORTAL")

    def test_email_carrier(self):
        config = ccr.route_carrier("AmWINS MGA")
        self.assertEqual(config["channel"], "EMAIL")
        self.assertIn("underwriter_email", config)

    def test_email_ask_portal_carrier(self):
        config = ccr.route_carrier("JIMCOR MGA")
        self.assertEqual(config["channel"], "EMAIL_ASK_PORTAL")

    def test_unknown_carrier_defaults_to_email(self):
        config = ccr.route_carrier("Some Carrier That Does Not Exist")
        self.assertEqual(config["channel"], "EMAIL")

    def test_blank_carrier_defaults_to_email(self):
        self.assertEqual(ccr.route_carrier("").get("channel"), "EMAIL")
        self.assertEqual(ccr.route_carrier(None).get("channel"), "EMAIL")

    def test_portal_carriers_listed(self):
        portals = ccr.portal_carriers()
        self.assertIn("Coterie", portals)
        self.assertIn("The Hartford", portals)
        for name in portals:
            self.assertEqual(ccr.load_channel_directory()[name]["channel"], "PORTAL")

    def test_list_carriers_sorted(self):
        names = ccr.list_carriers()
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), 29)


if __name__ == "__main__":
    unittest.main()
