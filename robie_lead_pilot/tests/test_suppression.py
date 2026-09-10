"""Unit tests for the Suppression Engine and SHA-256 Hashing."""

import unittest
from pathlib import Path

from src.gates.suppression_engine import SuppressionEngine
from src.models.cadence_models import CadenceType, ChannelType, StopReason, hash_identifier


class TestSuppressionEngine(unittest.TestCase):
    def setUp(self):
        self.tmp_file = Path("data/test_suppression.json")
        self.engine = SuppressionEngine(persistence_file=self.tmp_file)
        self.engine.clear()

    def tearDown(self):
        self.engine.clear()
        if self.tmp_file.exists():
            self.tmp_file.unlink()

    def test_hash_identifier_consistency(self):
        h1 = hash_identifier("+17325551234")
        h2 = hash_identifier(" +17325551234 ")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_add_and_query_suppression_by_phone(self):
        rec = self.engine.add_suppression(
            phone="+17325551234",
            reason=StopReason.OPT_OUT_CALL,
        )
        self.assertTrue(rec.record_id.startswith("SUPP-"))

        is_supp, found = self.engine.is_suppressed(phone="+17325551234")
        self.assertTrue(is_supp)
        self.assertEqual(found.record_id, rec.record_id)

        # Unrelated number should not be suppressed
        is_supp_other, _ = self.engine.is_suppressed(phone="+17325559999")
        self.assertFalse(is_supp_other)

    def test_add_and_query_by_applicant_id_and_email(self):
        rec = self.engine.add_suppression(
            applicant_id="AP-999",
            email="test@example.com",
            reason=StopReason.OPT_OUT_EMAIL,
        )

        is_supp_app, _ = self.engine.is_suppressed(applicant_id="AP-999")
        self.assertTrue(is_supp_app)

        is_supp_email, _ = self.engine.is_suppressed(email="test@example.com")
        self.assertTrue(is_supp_email)

    def test_persistence_and_reload(self):
        self.engine.add_suppression(phone="+17325558888", reason=StopReason.OPT_OUT_SMS_STOP)

        # Create new engine pointing to same file
        engine2 = SuppressionEngine(persistence_file=self.tmp_file)
        is_supp, rec = engine2.is_suppressed(phone="+17325558888")
        self.assertTrue(is_supp)
        self.assertEqual(rec.reason, StopReason.OPT_OUT_SMS_STOP)


if __name__ == "__main__":
    unittest.main()
