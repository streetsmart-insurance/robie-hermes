"""Request-routing Ascend strong/weak split. No browser-read exemption."""

from __future__ import annotations

import inspect
import os
import unittest
from unittest.mock import patch

from robie_job_engine import action_gate, request_routing
from robie_job_engine.request_routing import classify_request


JOB_264A708F = "Don't use Ascend, just do the EZLynx policy setup on 220250093"

REAL_ASCEND_WORK = (
    "finish the premium finance agreement",
    "Use PAWIVA for this program",
    "open account 221398001",
    "look at https://app.ascend.com/workspace",
    "open https://dashboard.useascend.com/create/new",
    "run ascend-api-create-program",
    "run ascend-locator-artifact-audit",
    "action ascend.create_program",
    "run ascend.locator_artifact_audit",
    "Create a program in Ascend",
)

READONLY_HELD = (
    (
        "check the page on app.ascend.com - new program for PAWIVA, "
        "agency fee $50, import document from the quote"
    ),
    (
        "read the page at dashboard.useascend.com for 221398001 "
        "premium finance and set agency fee"
    ),
    "check the page and set the agency fee on ascend for the quote",
    "read the page at app.ascend.com and tell me what it says",
)

FINANCE_TEAM_REVIEW = "please import document for the finance team review"


def _disabled_env():
    return patch.dict(os.environ, {}, clear=False)


class AscendRoutingStrongWeakTests(unittest.TestCase):
    def setUp(self):
        self._env = _disabled_env()
        self._env.start()
        os.environ.pop("ROBIE_ASCEND_API_ENABLED", None)

    def tearDown(self):
        self._env.stop()

    def test_call_site_is_unchanged_and_has_no_browser_read_exemption(self):
        source = inspect.getsource(request_routing.classify_request)
        self.assertIn("if _is_ascend_request(normalized):", source)
        self.assertNotIn("and not _is_browser_read", source)
        self.assertNotIn("_is_browser_read(normalized)", source.split("if _is_ascend_request")[0])

    def test_264a708f_negated_ascend_name_is_not_unavailable(self):
        result = classify_request(JOB_264A708F)
        self.assertNotEqual(result.action_type, "hermes.unavailable")
        self.assertNotEqual(result.hold_status, "FAILED")

    def test_real_ascend_work_is_failed_when_api_unset(self):
        for text in REAL_ASCEND_WORK:
            with self.subTest(text=text):
                result = classify_request(text)
                self.assertEqual(result.action_type, "hermes.unavailable", text)
                self.assertEqual(result.hold_status, "FAILED", text)


class ReadOnlyPhrasingIsNotAnEscapeHatch(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ROBIE_ASCEND_API_ENABLED", None)

    def test_readonly_phrasing_and_plain_ascend_url_read_are_held(self):
        for text in READONLY_HELD:
            with self.subTest(text=text):
                result = classify_request(text)
                self.assertEqual(result.action_type, "hermes.unavailable", text)
                self.assertEqual(result.hold_status, "FAILED", text)

    def test_import_document_for_finance_team_is_not_held(self):
        result = classify_request(FINANCE_TEAM_REVIEW)
        self.assertNotEqual(result.action_type, "hermes.unavailable")
        self.assertNotEqual(result.hold_status, "FAILED")


class AscendRoutingRegressionTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ROBIE_ASCEND_API_ENABLED", None)

    def test_skill_sync_still_routes(self):
        result = classify_request("sync skills")
        self.assertEqual(result.action_type, "drive.skill_sync")

    def test_carrier_proposal_still_routes(self):
        result = classify_request(
            "Create a carrier proposal from Jake's quote PDF and add a $350 fee"
        )
        self.assertEqual(result.action_type, "carrier.proposal")

    def test_ezlynx_reassign_still_routes(self):
        result = classify_request("EZLynx reassign this account to Ann")
        self.assertEqual(result.action_type, "ezlynx.reassign")

    def test_apply_label_still_routes(self):
        result = classify_request("EZLynx apply a label to this account")
        self.assertEqual(result.action_type, "ezlynx.apply_label")

    def test_submission_audit_still_routes(self):
        result = classify_request("Audit the EZLynx Submission Center overdue list")
        self.assertEqual(result.action_type, "ezlynx.submission_audit")

    def test_plain_browser_read_still_routes(self):
        result = classify_request("browser-only read the EZLynx account page")
        self.assertEqual(result.action_type, "browser.read")

    def test_vague_request_still_needs_clarification(self):
        result = classify_request("do it")
        self.assertEqual(result.action_type, "hermes.needs_clarification")
        self.assertEqual(result.hold_status, "NEEDS_CLARIFICATION")

    def test_ordinary_chat_is_not_held_as_ascend(self):
        result = classify_request("Please remind me which renewals are due this week")
        self.assertEqual(result.action_type, "hermes.plain_english")
        self.assertNotEqual(result.action_type, "hermes.unavailable")


class AscendMarkerListsMatchActionGate(unittest.TestCase):
    def test_marker_tuples_are_byte_identical(self):
        self.assertEqual(
            request_routing.STRONG_ASCEND_ACTION_MARKERS,
            action_gate.STRONG_ASCEND_ACTION_MARKERS,
        )
        self.assertEqual(
            request_routing.WEAK_ASCEND_NAME_MARKERS,
            action_gate.WEAK_ASCEND_NAME_MARKERS,
        )
        self.assertEqual(
            request_routing.CREATE_PROGRAM_MARKERS,
            action_gate.CREATE_PROGRAM_MARKERS,
        )
        self.assertEqual(
            request_routing.ASCEND_SKILL_MARKERS,
            action_gate.SKILL_MARKERS,
        )


if __name__ == "__main__":
    unittest.main()
