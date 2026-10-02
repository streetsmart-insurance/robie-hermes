"""Edge-case guards for PFA quote extraction: down payments, conflicting
totals, multiple insureds, expired/backdated/reversed dates."""

import unittest

from robie_job_engine.quote_extractor import QuoteExtractor


def _extract(text: str):
    return QuoteExtractor().extract_from_text(text)


class TestDownPayment(unittest.TestCase):
    def test_down_payment_detected_with_amount(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Premium: $10,000.00\nDown payment: $2,500.00\n"
            "Effective: 01/01/2027\n"
        )
        self.assertTrue(q.down_payment_detected)
        self.assertEqual(q.down_payment_cents, 250000)
        self.assertNotIn("down_payment_amount_unknown", q.hitl_reasons)

    def test_down_payment_percent_without_amount_asks(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Premium: $10,000.00\n25% down required\n"
            "Effective: 01/01/2027\n"
        )
        self.assertTrue(q.down_payment_detected)
        self.assertEqual(q.down_payment_cents, 0)
        self.assertIn("down_payment_amount_unknown", q.hitl_reasons)
        self.assertTrue(q.requires_hitl)

    def test_down_payment_answer_resolves(self):
        ext = QuoteExtractor()
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Premium: $10,000.00\n25% down required\n"
        )
        self.assertIn("down_payment_amount_unknown", q.hitl_reasons)
        q = ext.apply_user_clarifications(q, "down payment was $2,500")
        self.assertEqual(q.down_payment_cents, 250000)
        self.assertNotIn("down_payment_amount_unknown", q.hitl_reasons)

    def test_financed_amount_subtracts_down_payment(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Premium: $10,000.00\nDown payment: $2,500.00\n"
        )
        q.pure_premium_cents = 1000000
        q.agency_fees_cents = 0  # isolate the down-payment math
        self.assertEqual(q.financed_amount_cents, 750000)


class TestConflictingTotals(unittest.TestCase):
    def test_matching_totals_no_question(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "General Liability 5000.00\nCommercial Auto 5000.00\n"
            "Total Premium: $10,000.00\n"
        )
        self.assertNotIn("conflicting_totals", q.hitl_reasons)

    def test_mismatched_totals_asks(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "General Liability 5000.00\nCommercial Auto 4000.00\n"
            "Total Premium: $10,000.00\n"
        )
        # line items sum to 9000, stated total 10000 -> mismatch
        self.assertIn("conflicting_totals", q.hitl_reasons)
        self.assertTrue(q.requires_hitl)

    def test_conflicting_totals_answer_picks_line_items(self):
        ext = QuoteExtractor()
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "General Liability 5000.00\nCommercial Auto 4000.00\n"
            "Total Premium: $10,000.00\n"
        )
        self.assertIn("conflicting_totals", q.hitl_reasons)
        q = ext.apply_user_clarifications(q, "use the line items")
        self.assertNotIn("conflicting_totals", q.hitl_reasons)
        self.assertEqual(q.stated_total_cents, 900000)


class TestNamedInsureds(unittest.TestCase):
    def test_single_insured_no_question(self):
        q = _extract("Named Insured: Acme LLC\nCarrier: TestCarrier\n")
        self.assertNotIn("multiple_named_insureds", q.hitl_reasons)

    def test_dba_triggers_question(self):
        q = _extract(
            "Named Insured: Acme LLC DBA Acme Management\nCarrier: TestCarrier\n"
        )
        self.assertIn("multiple_named_insureds", q.hitl_reasons)
        self.assertTrue(q.requires_hitl)

    def test_named_insured_answer_picks_entity(self):
        ext = QuoteExtractor()
        q = _extract(
            "Named Insured: Acme LLC DBA Acme Management\nCarrier: TestCarrier\n"
        )
        q = ext.apply_user_clarifications(q, "use Acme Management")
        self.assertEqual(q.insured_name, "Acme Management")
        self.assertNotIn("multiple_named_insureds", q.hitl_reasons)


class TestQuoteDates(unittest.TestCase):
    def test_expired_quote_asks(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Quote Date: 01/15/2026\nPremium: $5,000.00\n"
        )
        self.assertIn("quote_expired", q.hitl_reasons)

    def test_expired_quote_answer_still_valid(self):
        ext = QuoteExtractor()
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Quote Date: 01/15/2026\nPremium: $5,000.00\n"
        )
        q = ext.apply_user_clarifications(q, "still valid, proceed")
        self.assertNotIn("quote_expired", q.hitl_reasons)

    def test_backdated_effective_asks(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Effective: 01/01/2026\nExpiration: 01/01/2027\n"
        )
        self.assertIn("effective_date_backdated", q.hitl_reasons)

    def test_reversed_dates_asks(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Effective: 01/01/2027\nExpiration: 01/01/2026\n"
        )
        self.assertIn("dates_reversed", q.hitl_reasons)

    def test_reversed_dates_answer_corrects(self):
        ext = QuoteExtractor()
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Effective: 01/01/2027\nExpiration: 01/01/2026\n"
        )
        q = ext.apply_user_clarifications(
            q, "effective 06/01/2026, expiration 06/01/2027"
        )
        self.assertNotIn("dates_reversed", q.hitl_reasons)
        self.assertEqual(q.effective_date, "2026-06-01")
        self.assertEqual(q.expiration_date, "2027-06-01")


class TestRevisionMarker(unittest.TestCase):
    def test_revision_detected(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "QUOTE revised v2\nPremium: $5,000.00\n"
        )
        self.assertTrue(q.quote_revision)


if __name__ == "__main__":
    unittest.main()


class TestAgencyFeeConfirmation(unittest.TestCase):
    def test_fee_confirmation_asked_when_other_questions(self):
        # Explicit fee + other HITL reasons -> confirmation bundled in
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Agency Fee: $350\n"
            "Quote Date: 01/15/2026\n"  # expired -> triggers HITL
            "Premium: $5,000.00\n"
        )
        self.assertIn("agency_fee_confirm", q.hitl_reasons)
        self.assertIn("quote_expired", q.hitl_reasons)

    def test_no_confirmation_when_no_other_questions(self):
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Agency Fee: $350\n"
            "Commission: 10%\n"
            "Premium: $5,000.00\n"
            "Effective: 01/01/2027\nExpiration: 01/01/2028\n"
        )
        # No other questions, so no confirmation needed
        self.assertNotIn("agency_fee_confirm", q.hitl_reasons)

    def test_confirmation_cleared_on_yes(self):
        ext = QuoteExtractor()
        q = _extract(
            "Insured Name: Test LLC\nCarrier: TestCarrier\n"
            "Agency Fee: $350\n"
            "Quote Date: 01/15/2026\n"
            "Premium: $5,000.00\n"
        )
        self.assertIn("agency_fee_confirm", q.hitl_reasons)
        q = ext.apply_user_clarifications(q, "yes, fee is correct")
        self.assertNotIn("agency_fee_confirm", q.hitl_reasons)
