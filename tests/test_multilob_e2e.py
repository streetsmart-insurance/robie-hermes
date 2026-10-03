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


class FakeTransport:
    """Minimal fake transport serving the verifier's read-back."""

    def __init__(self, client):
        self._client = client

    def request(self, method, path, query=None):
        if method == "GET" and path == "/billables":
            return {"data": self._client._billable_records()}
        raise AssertionError(f"unexpected {method} {path}")


class FakeAscendClient:
    """Minimal fake matching the methods the workflow calls.

    Persists what was created so get_program / transport.request can serve
    the verify-before-complete read-back, mirroring Ascend's record shape.
    """

    def __init__(self):
        self.created_billables = []
        self.transport = FakeTransport(self)
        self._insured_name = ""
        self._insured_address = {}
        self._insured_contact = {}

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
        self._insured_name = kwargs.get("business_name") or ""
        self._insured_address = dict(kwargs.get("address") or {})
        self._insured_contact = dict(kwargs.get("contact") or {})
        return ("12345678-1234-5678-1234-567812345678", False)

    def create_program(self, payload):
        return ("12345678-1234-5678-1234-567812345678",
                {"program_url": "https://example.com/p/prog-123"})

    def create_billable(self, payload):
        self.created_billables.append(dict(payload))
        return (f"b-{len(self.created_billables)}", {})

    def get_program(self, program_id):
        addr = self._insured_address
        contact = self._insured_contact
        return {
            "id": program_id,
            "status": "ready_for_checkout",
            "insured": {
                "business_name": self._insured_name,
                "mailing_address_street_one": addr.get("mailing_address_street_one") or "",
                "mailing_address_city": addr.get("mailing_address_city") or "",
                "mailing_address_state": addr.get("mailing_address_state") or "",
                "mailing_address_zip_code": addr.get("mailing_address_zip_code") or "",
                "insured_contacts": [{
                    "first_name": contact.get("first_name") or "",
                    "last_name": contact.get("last_name") or "",
                    "email": contact.get("email") or "",
                    "phone": contact.get("phone") or "",
                }],
            },
        }

    def _billable_records(self):
        records = []
        for b in self.created_billables:
            rate = b.get("organization_commission_rate") or 0
            rec = {
                "billable_identifier": b.get("billable_identifier"),
                "carrier": {"identifier": b.get("carrier_identifier")},
                "premium_cents": b.get("premium_cents"),
                "taxes_and_fees_cents": b.get("taxes_and_fees_cents", 0),
                "policy_fee_cents": b.get("policy_fee_cents", 0),
                "agency_fees_cents": b.get("agency_fees_cents", 0),
                "seller_commission_rate": rate,
                "seller_commission_amount_cents": round((b.get("premium_cents") or 0) * rate),
                "effective_date": b.get("effective_date"),
                "expiration_date": b.get("expiration_date"),
            }
            if b.get("wholesaler_identifier"):
                rec["wholesaler"] = {"identifier": b["wholesaler_identifier"]}
            records.append(rec)
        return records


class TestMultiLobEndToEnd(unittest.TestCase):
    def test_full_workflow_creates_separate_billables(self):
        ext = QuoteExtractor()
        quote = ext.extract_from_text(MULTI_LOB_TEXT)
        # Pre-resolve fields the workflow needs
        quote.carrier_identifier = "carr-national-indemnity"
        quote.commission_rate = 0.10
        quote.mailing_address = {
            "mailing_address_street_one": "123 Main St",
            "mailing_address_city": "Newark",
            "mailing_address_state": "NJ",
            "mailing_address_zip_code": "07101",
        }
        quote.primary_contact = {
            "first_name": "Test", "last_name": "Person",
            "email": "test@example.com", "phone": "555-0100",
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
