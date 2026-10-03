"""Multi-LOB tests: one quote with multiple lines of business becomes
multiple sub-policies -> one program with separate billables."""

import unittest

from robie_job_engine.quote_extractor import QuoteExtractor


def _extract(text: str):
    return QuoteExtractor().extract_from_text(text)


MULTI_LOB_QUOTE = """Insured Name: Acme Trucking LLC
Carrier: National Indemnity

COMMERCIAL AUTO
Policy Number: CA-12345
Premium: $8,000.00
Effective: 01/01/2027
Expiration: 01/01/2028

GENERAL LIABILITY
Policy Number: GL-67890
Premium: $5,000.00
Writing Company: Berkshire Hathaway
Effective: 01/01/2027
Expiration: 01/01/2028

MOTOR TRUCK CARGO
Policy Number: MTC-11111
Premium: $3,000.00
"""


class TestMultiLobDetection(unittest.TestCase):
    def test_three_lobs_detected(self):
        q = _extract(MULTI_LOB_QUOTE)
        self.assertEqual(len(q.sub_policies), 3)

    def test_each_lob_has_own_premium(self):
        q = _extract(MULTI_LOB_QUOTE)
        premiums = sorted(sp["pure_premium_cents"] for sp in q.sub_policies)
        self.assertEqual(premiums, [300000, 500000, 800000])

    def test_each_lob_has_own_policy_number(self):
        q = _extract(MULTI_LOB_QUOTE)
        numbers = sorted(sp.get("policy_number", "") for sp in q.sub_policies)
        self.assertEqual(numbers, ["CA-12345", "GL-67890", "MTC-11111"])

    def test_each_lob_has_own_coverage(self):
        q = _extract(MULTI_LOB_QUOTE)
        coverages = sorted(sp["coverage_identifier"] for sp in q.sub_policies)
        self.assertIn("commercial_auto", coverages)
        self.assertIn("gl", coverages)

    def test_writing_carrier_captured_per_lob(self):
        q = _extract(MULTI_LOB_QUOTE)
        gl = next(sp for sp in q.sub_policies if sp["coverage_identifier"] == "gl")
        self.assertEqual(gl.get("writing_carrier_name"), "Berkshire Hathaway")

    def test_per_lob_dates_captured(self):
        q = _extract(MULTI_LOB_QUOTE)
        auto = next(
            sp for sp in q.sub_policies if sp["coverage_identifier"] == "commercial_auto"
        )
        self.assertEqual(auto.get("effective_date"), "2027-01-01")
        self.assertEqual(auto.get("expiration_date"), "2028-01-01")

    def test_single_lob_no_sub_policies(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "COMMERCIAL AUTO\nPremium: $8,000.00\n"
        )
        self.assertEqual(q.sub_policies, [])

    def test_parent_premium_is_sum(self):
        q = _extract(MULTI_LOB_QUOTE)
        self.assertEqual(q.pure_premium_cents, 1600000)


class TestMultiLobValidation(unittest.TestCase):
    def test_zero_premium_lob_skipped(self):
        # A section without a detectable premium cannot become a billable
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "COMMERCIAL AUTO\nPremium: $8,000.00\n"
            "GENERAL LIABILITY\n(no premium listed)\n"
            "WORKERS COMP\nPremium: $2,000.00\n"
        )
        # Only sections with premiums become sub-policies
        for sp in q.sub_policies:
            self.assertGreater(sp["pure_premium_cents"], 0)


if __name__ == "__main__":
    unittest.main()
