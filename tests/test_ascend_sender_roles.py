"""Sender-agent resolver and programs spinner wait. No live Ascend."""

from __future__ import annotations

import unittest

from robie_job_engine.ascend_sender_roles import (
    ACCESSIBLE_NAME_SCENARIO_ID,
    CARLO_FERRARA,
    CARET_ACCESSIBLE_NAME,
    DUMPED_PROGRAMS_PRIMARY_BUTTON,
    JAKE_FERRARA,
    NEW_PROGRAM_ACCESSIBLE_NAME,
    NEW_PROGRAM_ACCESSIBLE_NAME_CHAR_CODES,
    NEW_PROGRAM_LOCATOR,
    PLUS_PREFIXED_NEW_PROGRAM_LOCATOR,
    PLUS_PREFIXED_NEW_PROGRAM_NAME,
    ROBIE_AI,
    ROLES_SCENARIO_ID,
    SPINNER_SCENARIO_ID,
    SSROBIE,
    ascend_new_program_contract_lines,
    locator_is_new_program_caret,
    plus_prefixed_exact_locator_matches_dump,
    programs_spinner_timeout_error,
    playwright_exact_name_matches,
    refuse_robie_ai_when_sender_known,
    requested_by_from_job,
    requested_by_from_payload,
    resolve_sender_agent,
    roles_for_requested_by,
    run_accessible_name_scenario,
    run_sender_not_robie_ai_scenario,
    run_wait_spinner_scenario,
    unknown_sender_hitl,
)
from robie_job_engine.hitl import sanitize_hitl_chat_text
from robie_job_engine.ascend_locator_audit import (
    PRODUCTION_EB96_ARTIFACT_ID,
    PRODUCTION_EB96_JOB_ID,
    sliced_concat_job_folder,
    worker_lookup_artifact_dir,
)


class SenderAgentResolverTests(unittest.TestCase):
    def test_jake_requested_by_is_jake_ferrara_not_robie_ai(self):
        for raw in (
            "Jake",
            "jake",
            "Jake Ferrara",
            "jake@streetsmart.insurance",
            "StreetSmartJake",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_sender_agent(raw), JAKE_FERRARA)
                roles = roles_for_requested_by(raw)
                self.assertEqual(roles["producer"], JAKE_FERRARA)
                self.assertEqual(roles["account_manager"], JAKE_FERRARA)
                self.assertFalse(roles["hitl_required"])
                self.assertNotEqual(roles["producer"], ROBIE_AI)
                leak = refuse_robie_ai_when_sender_known(
                    requested_by=raw,
                    producer=ROBIE_AI,
                    account_manager=ROBIE_AI,
                )
                self.assertIsNotNone(leak)

    def test_carlo_requested_by_is_carlo_ferrara(self):
        for raw in (
            "Carlo",
            "carlo",
            "Carlo Ferrara",
            "carlo@streetsmart.insurance",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_sender_agent(raw), CARLO_FERRARA)
                roles = roles_for_requested_by(raw)
                self.assertEqual(roles["producer"], CARLO_FERRARA)
                self.assertEqual(roles["account_manager"], CARLO_FERRARA)

    def test_unknown_requested_by_does_not_write_robie_ai(self):
        for raw in (
            "Robie AI",
            "SSRobie",
            "robie@streetsmart.insurance",
            "",
            "Google Chat user",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(resolve_sender_agent(raw))
                roles = roles_for_requested_by(raw)
                self.assertTrue(roles["hitl_required"])
                self.assertIsNone(roles["producer"])
                self.assertFalse(roles["write_robie_ai"])
                self.assertIn("PLAYWRIGHT_BLOCKED", roles["hitl_text"])
                self.assertNotIn("Listen up", roles["hitl_text"])
                leak = refuse_robie_ai_when_sender_known(
                    requested_by=raw,
                    producer=ROBIE_AI,
                    account_manager=SSROBIE,
                )
                self.assertIsNotNone(leak)

    def test_uses_existing_requested_by_field_not_a_second_identity(self):
        payload = {"requested_by": "Jake Ferrara", "sender": "ignored"}
        self.assertEqual(requested_by_from_payload(payload), "Jake Ferrara")
        self.assertEqual(
            requested_by_from_job({"payload": {"from": "carlo@streetsmart.insurance"}}),
            "carlo@streetsmart.insurance",
        )
        self.assertEqual(
            resolve_sender_agent(requested_by_from_payload(payload)),
            JAKE_FERRARA,
        )

    def test_named_roles_scenario_passes(self):
        report = run_sender_not_robie_ai_scenario()
        self.assertEqual(report["id"], ROLES_SCENARIO_ID)
        self.assertTrue(report["ok"], report.get("evidence"))


class ProgramsSpinnerWaitTests(unittest.TestCase):
    def test_unique_new_program_not_caret(self):
        self.assertIn(NEW_PROGRAM_ACCESSIBLE_NAME, NEW_PROGRAM_LOCATOR)
        self.assertNotIn(PLUS_PREFIXED_NEW_PROGRAM_NAME, NEW_PROGRAM_LOCATOR)
        self.assertIn("exact=True", NEW_PROGRAM_LOCATOR)
        self.assertTrue(locator_is_new_program_caret("split-menu caret"))
        self.assertTrue(locator_is_new_program_caret('page.locator("button").nth(1)'))
        self.assertFalse(locator_is_new_program_caret(NEW_PROGRAM_LOCATOR))

    def test_plus_exact_locator_fails_against_dumped_accessible_name(self):
        dumped = str(DUMPED_PROGRAMS_PRIMARY_BUTTON["accessible_name"])
        self.assertEqual(dumped, "New program")
        self.assertEqual(
            tuple(ord(char) for char in dumped),
            NEW_PROGRAM_ACCESSIBLE_NAME_CHAR_CODES,
        )
        self.assertNotIn("+", dumped)
        self.assertIsNone(DUMPED_PROGRAMS_PRIMARY_BUTTON["aria_label"])
        self.assertFalse(plus_prefixed_exact_locator_matches_dump(dumped))
        self.assertFalse(
            playwright_exact_name_matches(PLUS_PREFIXED_NEW_PROGRAM_NAME, dumped)
        )
        self.assertTrue(playwright_exact_name_matches(NEW_PROGRAM_ACCESSIBLE_NAME, dumped))
        self.assertNotEqual(PLUS_PREFIXED_NEW_PROGRAM_LOCATOR, NEW_PROGRAM_LOCATOR)
        self.assertEqual(CARET_ACCESSIBLE_NAME, "Open split button menu")
        report = run_accessible_name_scenario()
        self.assertEqual(report["id"], ACCESSIBLE_NAME_SCENARIO_ID)
        self.assertTrue(report["ok"], report.get("evidence"))
        self.assertFalse(report["observed"]["plus_exact_matches_dump"])

    def test_spinner_timeout_is_playwright_blocked_no_gemini(self):
        text = programs_spinner_timeout_error("TimeoutError")
        self.assertTrue(text.startswith("PLAYWRIGHT_BLOCKED"))
        self.assertIn("Gemini", text)
        report = run_wait_spinner_scenario()
        self.assertEqual(report["id"], SPINNER_SCENARIO_ID)
        self.assertTrue(report["ok"], report.get("evidence"))

    def test_ascend_contract_lines_include_wait_and_roles(self):
        lines = ascend_new_program_contract_lines(
            "Open Ascend and create a program",
            {"requested_by": "StreetSmartJake", "action_type": "hermes.google_chat_task"},
        )
        blob = "\n".join(lines)
        self.assertIn("spinner", blob.casefold())
        self.assertIn(NEW_PROGRAM_LOCATOR, blob)
        self.assertNotIn(PLUS_PREFIXED_NEW_PROGRAM_NAME, blob)
        self.assertIn("Jake Ferrara", blob)
        self.assertIn("Import document", blob)
        self.assertIn("0-element", blob.casefold())
        self.assertIn("line of business", blob.casefold())
        self.assertIn("seconds", blob.casefold())
        self.assertIn("/create/new", blob)
        self.assertIn("500", blob)
        self.assertNotIn("write Robie AI", blob)


class ExistingGuardsStillHoldTests(unittest.TestCase):
    def test_job_id_slice_plus_artifact_id_still_fail(self):
        wrong = sliced_concat_job_folder(
            PRODUCTION_EB96_JOB_ID, PRODUCTION_EB96_ARTIFACT_ID
        )
        legal = worker_lookup_artifact_dir(
            "/tmp/artifacts",
            PRODUCTION_EB96_JOB_ID,
            artifact_id=PRODUCTION_EB96_ARTIFACT_ID,
        )
        self.assertNotEqual(wrong, PRODUCTION_EB96_JOB_ID)
        self.assertEqual(legal.name, PRODUCTION_EB96_JOB_ID)

    def test_cowboy_hitl_still_rewritten(self):
        cowboy = "Listen up, Jake! it ain't my fault"
        rewritten = sanitize_hitl_chat_text(cowboy)
        self.assertNotIn("Listen up", rewritten)
        self.assertNotIn("ain't", rewritten)
        self.assertIn("PLAYWRIGHT_BLOCKED", rewritten)
        hitl = unknown_sender_hitl(requested_by="Google Chat user")
        self.assertIn("PLAYWRIGHT_BLOCKED", hitl)
        self.assertNotIn("Listen up", hitl)


if __name__ == "__main__":
    unittest.main()
