import unittest
from unittest.mock import MagicMock

from robie_job_engine.ascend_workflow import AscendWorkflowManager, WorkflowResult
from robie_job_engine.ezlynx_note_poster import EZLynxAgreementPoster, format_ascend_agreement_note
from robie_job_engine.quote_extractor import ExtractedQuote, QuoteExtractor


SAMPLE_AMBIGUOUS_QUOTE = """
NAMED INSURED: Acme Hauling LLC
CARRIER: Nautilus Insurance Group
WHOLESALER: Tapco Underwriters
COVERAGE: Commercial Auto
QUOTE NUMBER: QUOTE-100234
POLICY PERIOD: 10/01/2026 to 10/01/2027
BASE PREMIUM: $12,000.00
OPTION 1 (with TRIA): $12,600.00
OPTION 2 (without TRIA): $12,000.00
"""

SAMPLE_CLEAR_QUOTE = """
NAMED INSURED: Apex Transport Inc
CARRIER: Nautilus Insurance Group
WHOLESALER: Tapco Underwriters
COVERAGE: Commercial Auto
QUOTE NUMBER: APX-8831
POLICY PERIOD: 10/01/2026 to 10/01/2027
PURE PREMIUM: $15,000.00
AGENCY FEE: $350.00
COMMISSION: 12.5%
SURPLUS LINES TAX: $750.00
STAMPING FEE: $25.00
OPTION 1 (with TRIA): $15,750.00
OPTION 2 (without TRIA): $15,000.00
"""


class TestQuoteExtractor(unittest.TestCase):
    def setUp(self):
        self.extractor = QuoteExtractor()

    def test_ambiguous_quote_requires_hitl(self):
        quote = self.extractor.extract_from_text(SAMPLE_AMBIGUOUS_QUOTE)
        self.assertTrue(quote.requires_hitl)
        self.assertIn("agency_fee_unspecified", quote.hitl_reasons)
        self.assertIn("commission_rate_unspecified", quote.hitl_reasons)
        self.assertIn("surplus_lines_tax_verification", quote.hitl_reasons)
        self.assertIn("dual_terrorism_options_present", quote.hitl_reasons)
        self.assertEqual(len(quote.hitl_questions), 4)

    def test_clear_quote_with_instruction_skips_hitl(self):
        quote = self.extractor.extract_from_text(
            SAMPLE_CLEAR_QUOTE,
            user_instruction="Please bind with terrorism coverage included",
        )
        self.assertFalse(quote.requires_hitl)
        self.assertEqual(quote.insured_name, "Apex Transport Inc")
        self.assertEqual(quote.carrier_name, "Nautilus Insurance Group")
        self.assertEqual(quote.wholesaler_name, "Tapco Underwriters")
        self.assertEqual(quote.agency_fees_cents, 35000)
        self.assertEqual(quote.commission_rate, 0.125)
        self.assertEqual(quote.surplus_lines_tax_cents, 77500)
        self.assertTrue(quote.terrorism_included)
        self.assertEqual(quote.pure_premium_cents, 1575000)

    def test_apply_user_clarifications(self):
        quote = self.extractor.extract_from_text(SAMPLE_AMBIGUOUS_QUOTE)
        self.assertTrue(quote.requires_hitl)

        reply = "1. Yes standard $350 fee. 2. 10% commission. 3. Surplus tax is $600. 4. Include terrorism"
        resolved = self.extractor.apply_user_clarifications(quote, reply)
        self.assertFalse(resolved.requires_hitl)
        self.assertEqual(resolved.agency_fees_cents, 35000)
        self.assertEqual(resolved.commission_rate, 0.10)
        self.assertEqual(resolved.surplus_lines_tax_cents, 60000)
        self.assertTrue(resolved.terrorism_included)
        self.assertEqual(resolved.pure_premium_cents, 1260000)


class TestEZLynxNotePoster(unittest.TestCase):
    def test_format_note(self):
        quote = ExtractedQuote(
            insured_name="Acme Hauling LLC",
            carrier_name="Nautilus Insurance Group",
            wholesaler_name="Tapco Underwriters",
            coverage_title="Commercial Auto",
            pure_premium_cents=1260000,
            agency_fees_cents=35000,
            commission_rate=0.10,
            surplus_lines_tax_cents=60000,
            terrorism_included=True,
        )
        url = "https://checkout.useascend.com/streetsmart_insurance_agency/overview?program_id=prog-123"
        note = format_ascend_agreement_note(quote, url)
        self.assertIn("Acme Hauling LLC", note)
        self.assertIn("Nautilus Insurance Group", note)
        self.assertIn("Tapco Underwriters", note)
        self.assertIn("$12,600.00", note)
        self.assertIn("$350.00", note)
        self.assertIn("10.0%", note)
        self.assertIn("Terrorism Coverage: Included", note)
        self.assertIn(url, note)
        self.assertIn("Robie was here", note)


class TestAscendWorkflowManager(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.search_carriers.return_value = [
            {"identifier": "nautilus_insurance_group_scottsdale_e3f1c1", "title": "Nautilus Insurance Group"}
        ]
        self.mock_client.search_wholesalers.return_value = [
            {"identifier": "tapco_underwriters_burlington_b23d35", "title": "Tapco Underwriters"}
        ]
        self.mock_client.find_or_create_insured.return_value = (
            "11111111-1111-1111-1111-111111111111",
            {"id": "11111111-1111-1111-1111-111111111111", "business_name": "Apex Transport Inc"},
        )
        self.mock_client.resolve_user.return_value = "22222222-2222-2222-2222-222222222222"
        self.mock_client.create_program.return_value = (
            "33333333-3333-3333-3333-333333333333",
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "program_url": "https://checkout.useascend.com/streetsmart_insurance_agency/overview?program_id=33333333-3333-3333-3333-333333333333",
            },
        )
        self.mock_client.create_billable.return_value = (
            "44444444-4444-4444-4444-444444444444",
            {"id": "44444444-4444-4444-4444-444444444444"},
        )

        self.mock_ezlynx_poster = MagicMock()
        self.mock_ezlynx_poster.post_agreement_note.return_value = {"status": "success", "note_id": 9999}

        self.manager = AscendWorkflowManager(
            client_factory=lambda: self.mock_client,
            ezlynx_poster=self.mock_ezlynx_poster,
        )

    def test_workflow_requires_hitl_on_ambiguous_quote(self):
        res = self.manager.process_quote_request(
            SAMPLE_AMBIGUOUS_QUOTE,
            sender_name="Carlo",
            sender_email="carlo@streetsmart.insurance",
        )
        self.assertEqual(res.status, "NEEDS_CLARIFICATION")
        self.assertIn("Clarification Needed", res.reply_email_subject)
        self.assertIn("Hi Carlo,", res.reply_email_body)
        self.assertIn("Agency Fee", res.reply_email_body)
        self.assertIn("Commission Rate", res.reply_email_body)
        self.assertIn("Surplus Lines Tax", res.reply_email_body)
        self.assertIn("Terrorism Coverage", res.reply_email_body)
        self.mock_client.create_program.assert_not_called()

    def test_workflow_completes_when_clarified(self):
        res_initial = self.manager.process_quote_request(
            SAMPLE_AMBIGUOUS_QUOTE,
            sender_name="Carlo",
            sender_email="carlo@streetsmart.insurance",
        )
        self.assertEqual(res_initial.status, "NEEDS_CLARIFICATION")

        reply = "1. Yes $350 fee 2. 10% commission 3. $500 surplus tax 4. Option 1 with terrorism"
        res_completed = self.manager.resume_with_clarifications(
            res_initial.quote,
            reply,
            sender_name="Carlo",
            sender_email="carlo@streetsmart.insurance",
            applicant_id="123456",
        )
        self.assertEqual(res_completed.status, "COMPLETED")
        self.assertEqual(res_completed.program_id, "33333333-3333-3333-3333-333333333333")
        self.assertTrue(res_completed.program_url.startswith("https://checkout.useascend.com/"))
        self.mock_client.create_program.assert_called_once()
        self.mock_client.create_billable.assert_called_once()
        self.mock_ezlynx_poster.post_agreement_note.assert_called_once()
        self.assertIn("Ascend Agreement Ready", res_completed.reply_email_subject)
        self.assertIn("Robie was here", res_completed.reply_email_body)


class TestAscendGoogleChatIntegration(unittest.TestCase):
    def test_ascend_routes_to_google_chat_task_when_enabled(self):
        import os
        from unittest.mock import patch
        from robie_job_engine.request_routing import classify_request

        with patch.dict(os.environ, {"ROBIE_ASCEND_API_ENABLED": "true"}):
            classification = classify_request("Create a program in Ascend for Progressive")
            self.assertEqual(classification.action_type, "hermes.google_chat_task")
            self.assertIsNone(classification.hold_status)

    def test_ascend_routes_to_unavailable_when_disabled(self):
        import os
        from unittest.mock import patch
        from robie_job_engine.request_routing import classify_request

        with patch.dict(os.environ, {"ROBIE_ASCEND_API_ENABLED": ""}):
            classification = classify_request("Create a program in Ascend for Progressive")
            self.assertEqual(classification.action_type, "hermes.unavailable")
            self.assertEqual(classification.hold_status, "FAILED")

    def test_google_chat_job_opens_successfully_when_enabled(self):
        import os
        from pathlib import Path
        from unittest.mock import patch
        from durable_temp import durable_temporary_directory
        from robie_job_engine.chat_guard import open_chat_job
        from robie_job_engine.models import JobStatus
        from robie_job_engine.store import JobStore

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with patch.dict(os.environ, {"ROBIE_ASCEND_API_ENABLED": "true"}):
                job_id = open_chat_job(
                    db,
                    "msg-chat-1",
                    "@Robie create an Ascend agreement for Acme Hauling LLC with Nautilus Insurance",
                    requested_by="carlo@streetsmart.insurance",
                    conversation_id="spaces/chat-room",
                )
                store = JobStore(db)
                job = store.get_job(job_id)
                self.assertEqual(job["action_type"], "hermes.google_chat_task")
                self.assertNotEqual(job["status"], JobStatus.FAILED.value)
                self.assertNotIn("ASCEND_UNAVAILABLE", job.get("last_error") or "")


if __name__ == "__main__":
    unittest.main()

