#!/usr/bin/env python3
"""Tests for the proof pipeline: gold-payload create, search-first, label fill.

stdlib unittest — no browser, no network, no box. The API client is exercised
through an injectable urlopen; the label filler through a fake page.
"""
from __future__ import annotations

import io
import json
import unittest
from urllib import error as urlerror

from robie_job_engine import formentry_coverages as fc
from robie_job_engine import policy_setup_proof as proof
from robie_job_engine.ezlynx_api import EzlynxApiClient, EzlynxApiConfig


def make_config() -> EzlynxApiConfig:
    return EzlynxApiConfig(
        token_endpoint="https://auth.example/token",
        document_base_url="https://app.ezlynx.com/",
        client_id="cid",
        client_secret="csecret",
        username="SSRobie",
        integration_group_id="159",
        scope="PolicyApi",
    )


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode()


class FakeTokenClient(EzlynxApiClient):
    """Skips the OAuth dance; records POST bodies."""

    def __init__(self, config, search_rows, create_response="999"):
        super().__init__(config, urlopen=self._fake_urlopen)
        self.search_rows = search_rows
        self.create_response = create_response
        self.posts: list[dict] = []
        self.gets = 0

    def get_token(self):  # noqa: D102 - test double
        return "fake-token"

    def _fake_urlopen(self, url, data=None, headers=None, timeout=None):
        if data is None:
            self.gets += 1
            return FakeResp({"status": "success", "data": self.search_rows})
        self.posts.append(json.loads(data.decode()))
        return FakeResp(self.create_response)


class TestCreatePolicyGoldPayload(unittest.TestCase):
    def test_gold_payload_types_and_values(self):
        client = FakeTokenClient(make_config(), [])
        client.create_policy(
            applicant_id="220250093",
            policy_number="TEST-X",
            effective_date="2026-10-02T00:00:00",
            expiration_date="2027-10-02T00:00:00",
        )
        self.assertEqual(len(client.posts), 1)
        payload = client.posts[0]
        # The gold identity: writingCompany is the STRING "10048",
        # masterCompany is the INT 13585.
        self.assertEqual(payload["writingCompany"], "10048")
        self.assertIsInstance(payload["writingCompany"], str)
        self.assertEqual(payload["masterCompany"], 13585)
        self.assertIsInstance(payload["masterCompany"], int)
        self.assertEqual(payload["accountId"], 220250093)
        self.assertEqual(payload["policyNumber"], "TEST-X")
        self.assertEqual(payload["transactionType"], "NBS")

    def test_exactly_one_post(self):
        client = FakeTokenClient(make_config(), [])
        client.create_policy(
            applicant_id="220250093",
            policy_number="TEST-X",
            effective_date="2026-10-02T00:00:00",
            expiration_date="2027-10-02T00:00:00",
        )
        self.assertEqual(len(client.posts), 1)

    def test_refuses_wrong_applicant(self):
        with self.assertRaises(ValueError):
            proof.ensure_applicant_scope("221398001")
        with self.assertRaises(ValueError):
            proof.ensure_applicant_scope("")


class TestSearchFirstCreate(unittest.TestCase):
    def test_skips_create_when_policy_exists(self):
        row = {"policyNumber": "TEST-EXISTS", "policyId": 123}
        client = FakeTokenClient(make_config(), [row])
        report = proof.search_first_create(
            client,
            applicant_id="220250093",
            policy_number="TEST-EXISTS",
            effective_date="2026-10-02T00:00:00",
            expiration_date="2027-10-02T00:00:00",
        )
        self.assertEqual(report["verdict"], "ALREADY_EXISTS")
        self.assertEqual(len(client.posts), 0)

    def test_creates_and_reads_back_when_absent(self):
        client = FakeTokenClient(make_config(), [])
        # Pre-create search finds nothing; post-create search finds the row.
        searches = iter([[], [{"policyNumber": "TEST-NEW", "policyId": 456}]])
        client.search_rows = None  # sentinel: use the iterator instead
        orig = client._fake_urlopen

        def seq_urlopen(url, data=None, headers=None, timeout=None):
            if data is None:
                client.search_rows = next(searches)
            return orig(url, data=data, headers=headers, timeout=timeout)

        client._urlopen = seq_urlopen
        report = proof.search_first_create(
            client,
            applicant_id="220250093",
            policy_number="TEST-NEW",
            effective_date="2026-10-02T00:00:00",
            expiration_date="2027-10-02T00:00:00",
        )
        self.assertEqual(report["verdict"], "CREATED_AND_READ_BACK")
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(report["read_back"]["policyId"], 456)


class TestExtractPolicyId(unittest.TestCase):
    def test_all_key_variants(self):
        for key in ("policyId", "policyID", "PolicyId", "PolicyID", "id", "policy_id"):
            with self.subTest(key=key):
                self.assertEqual(
                    proof._extract_policy_id({key: 42, "policyNumber": "X"}), "42"
                )

    def test_case_insensitive_fallback(self):
        self.assertEqual(proof._extract_policy_id({"POLICYID": 77}), "77")

    def test_non_dict_returns_none(self):
        self.assertIsNone(proof._extract_policy_id(None))
        self.assertIsNone(proof._extract_policy_id("83669148"))

    def test_no_id_key_returns_none(self):
        self.assertIsNone(proof._extract_policy_id({"policyNumber": "X"}))

    def test_already_exists_report_carries_policy_id(self):
        # Regression: job 4dfee5f4 found TEST-HO-20260911-E01 via the
        # pre-create search (ALREADY_EXISTS) but the row used "PolicyID",
        # which the old 5-key list missed. The engine then failed with
        # "Create HTTP None" for a create that was never attempted.
        row = {"policyNumber": "TEST-HO-20260911-E01", "PolicyID": 83669148}
        client = FakeTokenClient(make_config(), [row])
        report = proof.search_first_create(
            client,
            applicant_id="220250093",
            policy_number="TEST-HO-20260911-E01",
            effective_date="2026-10-02T00:00:00",
            expiration_date="2027-10-02T00:00:00",
        )
        self.assertEqual(report["verdict"], "ALREADY_EXISTS")
        self.assertEqual(report["policy_id"], "83669148")
        self.assertEqual(len(client.posts), 0)


class TestCoverageLabels(unittest.TestCase):
    def test_literal_labels_match_carlo_evidence(self):
        self.assertEqual(
            list(fc.COVERAGE_LABELS),
            [
                "Dwelling",
                "Other Structures",
                "Personal Property",
                "Loss of Use",
                "Blanket",
                "Personal Liability EA OCC",
                "Medical Payments EA PER",
            ],
        )

    def test_no_invented_ho_selectors_anywhere(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        for rel in (
            "robie_job_engine/formentry_coverages.py",
            "robie_job_engine/policy_setup_proof.py",
            "scripts/proof_policy_setup.py",
        ):
            text = (root / rel).read_text()
            self.assertNotIn("#HO_CoverageA", text, rel)
            self.assertNotIn("HO_CoverageA", text, rel)


class FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    def fill(self, value):
        self._page.filled[self._selector] = value

    def select_option(self, value):
        self._page.filled[self._selector] = value

    def input_value(self):
        return self._page.filled.get(self._selector, "")


class FakePage:
    """Minimal stand-in for the label->input resolution path."""

    def __init__(self, label_map):
        # label_map: label -> {"id": ..., "tag": ...}
        self._label_map = label_map
        self.filled: dict[str, str] = {}

    def evaluate(self, _js, label=None):
        if label is None:
            return list(self._label_map.keys())
        desc = self._label_map.get(label)
        if not desc:
            return {"found": False, "reason": "label text not found"}
        return {"found": True, "via": "label-for", "input": desc}

    def locator(self, selector):
        return FakeLocator(self, selector)


class TestFillCoveragesByLabel(unittest.TestCase):
    def test_fills_and_reads_back(self):
        page = FakePage({"Dwelling": {"id": "cov1", "tag": "input", "name": "cov1"}})
        report = fc.fill_coverages_by_label(page, {"Dwelling": "250000"})
        entry = report["labels"]["Dwelling"]
        self.assertTrue(entry["found"])
        self.assertTrue(entry["filled"])
        self.assertTrue(entry["matches"])
        self.assertEqual(report["filled_count"], 1)

    def test_missing_label_reported_not_guessed(self):
        page = FakePage({})
        report = fc.fill_coverages_by_label(page, {"Dwelling": "250000"})
        entry = report["labels"]["Dwelling"]
        self.assertFalse(entry["found"])
        self.assertIn("Dwelling", report["not_found"])
        self.assertEqual(report["filled_count"], 0)

    def test_live_prefixed_label_matches_job_dwelling(self):
        self.assertEqual(
            fc.match_wanted_to_live_label(
                "Dwelling",
                ["Coverage A - Dwelling", "Other Structures"],
            ),
            "Coverage A - Dwelling",
        )
        page = FakePage(
            {"Coverage A - Dwelling": {"id": "liveA", "tag": "input", "name": "a"}}
        )
        report = fc.fill_coverages_by_label(page, {"Dwelling": "250000"})
        self.assertEqual(report["filled_count"], 1)
        self.assertTrue(report["labels"]["Dwelling"]["filled"])
        self.assertNotIn("HO_CoverageA", str(report))


if __name__ == "__main__":
    unittest.main()
