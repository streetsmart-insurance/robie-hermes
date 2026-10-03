"""End-to-end workflow test: multi-LOB quote -> one program, separate billables.

Uses a fake Ascend transport to exercise the full
create_agreement_and_file_ezlynx path without live API calls.
"""

import unittest
from unittest.mock import MagicMock

from robie_job_engine.ascend_workflow import AscendWorkflowManager
from robie_job_engine.quote_extractor import QuoteExtractor


MULTI_LOB_TEXT = """Insured Name: Acme Trucking LLC
Carrier: National Indemnity
Agency Fee: $350
Commission: 10%

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
"""


class FakeAscendClient:
    """Minimal fake matching the methods the workflow calls."""

    def __init__(self):
        self.created_billables = []

    def find_program_by_policy(self, policy_number):
        return None  # no duplicates

    def search_carriers(self, name):
        # Single match for any carrier name
        return [{"identifier": f"carr-{name.lower().replace(' ', '-')}",
                 "title": name}]

    def search_wholesalers(self, name):
        return []

    def list_users(self):
        return [{"id": "12345678-1234-5678-1234-567812345678",
                 "email": "jake@streetsmart.insurance",
                 "first_name": "Jake", "last_name": "Ferrara"}]

    def resolve_user(self, hint):
        return "12345678-1234-5678-1234-567812345678"

    def find_or_create_insured(self, **kwargs):
        return ("12345678-1234-5678-1234-567812345678", False)

    def create_program(self, payload):
        return ("12345678-1234-5678-1234-567812345678",
                {"program_url": "https://example.com/p/prog-123"})

    def create_billable(self, payload):
        self.created_billables.append(payload)
        return (f"b-{len(self.created_billables)}", {})


class TestMultiLobEndToEnd(unittest.TestCase):
    def test_full_workflow_creates_separate_billables(self):
        ext = QuoteExtractor()
        quote = ext.extract_from_text(MULTI_LOB_TEXT)
        # Pre-resolve fields the workflow needs
        quote.carrier_identifier = "carr-national-indemnity"
        quote.commission_rate = 0.10
        quote.mailing_address = {
            "street": "123 Main St", "city": "Newark",
            "state": "NJ", "zip": "07101",
        }
        quote.primary_contact = {
            "name": "Test Person", "email": "test@example.com",
            "phone": "555-0100",
        }

        fake_client = FakeAscendClient()
        mgr = AscendWorkflowManager(
            client_factory=lambda: fake_client,
            ezlynx_poster=MagicMock(**{"post_agreement_note.return_value": {"ok": True}}),
        )
        result = mgr.create_agreement_and_file_ezlynx(
            quote,
            sender_email="jake@streetsmart.insurance",
            sender_name="Jake",
        )
        self.assertEqual(result.status, "COMPLETED")
        billables = fake_client.created_billables
        # Two LOBs -> two separate billables under one program
        self.assertEqual(len(billables), 2)
        # Each billable has its own policy-derived identifier
        idents = sorted(b["billable_identifier"] for b in billables)
        self.assertTrue(any("CA-12345" in i for i in idents))
        self.assertTrue(any("GL-67890" in i for i in idents))
        # Premiums are separate, not blended
        premiums = sorted(b["premium_cents"] for b in billables)
        self.assertEqual(premiums, [500000, 800000])
        # GL uses its writing carrier, auto uses parent carrier
        carriers = {b["billable_identifier"]: b["carrier_identifier"]
                    for b in billables}
        gl_key = next(k for k in carriers if "GL-67890" in k)
        auto_key = next(k for k in carriers if "CA-12345" in k)
        self.assertIn("berkshire", carriers[gl_key])
        self.assertIn("national", carriers[auto_key])
        # Agency fee charged once (on first billable only)
        fees = [b["agency_fees_cents"] for b in billables]
        self.assertEqual(sum(1 for f in fees if f > 0), 1)


if __name__ == "__main__":
    unittest.main()
