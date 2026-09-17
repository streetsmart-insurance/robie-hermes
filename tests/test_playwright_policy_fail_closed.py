"""Regressions for wrong-policy Playwright writes and false COMPLETE.

No live EZLynx. No real customer, policy, coverage, or payment data.
Production release stays FAIL. live_test_complete stays false.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import guard_chat_response, open_chat_job
from robie_job_engine.complete_guard import (
    destination_identity_missing,
    expected_postcondition_missing,
    postcondition_mismatch,
    require_complete_postcondition,
)
from robie_job_engine.engine import JobEngine
from robie_job_engine.models import VERIFIER_AUTHORITY, JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from robie_job_engine.playwright_write_guard import (
    attested_test_form_entry_block_reason,
    collect_blocked_dialog,
    consult_gemini_for_blocked_write,
    install_playwright_write_guards,
    locator_is_positional_guess,
    require_unique_write_target,
    unique_write_block_reason,
    unwritable_control_block_reason,
)
from robie_job_engine.release_gate import production_release_decision
from robie_job_engine.store import JobStore


ROOT = Path(__file__).resolve().parents[1]
ENGINE_GUARD = ROOT / "robie_job_engine" / "playwright_write_guard.py"
OVERLAY_GUARD = ROOT / "deploy" / "hermes" / "tools" / "playwright_write_guard.py"


class FakeLocator:
    def __init__(self, matches: int, selector: str, page=None):
        self.matches = matches
        self.selector = selector
        self.page = page
        self.fills: list[str] = []

    def count(self):
        return self.matches

    @property
    def first(self):
        return FakeLocator(1, f"{self.selector} >> nth=0", page=self.page)

    def nth(self, index: int):
        return FakeLocator(1, f"{self.selector} >> nth={index}", page=self.page)

    def fill(self, value: str):
        require_unique_write_target(self)
        self.fills.append(value)


class FakePage:
    def __init__(self, fields: dict[str, FakeLocator], *, title="Add Vehicle", visible_labels=None):
        self.fields = fields
        self.dialog_title = title
        self.visible_labels = list(visible_labels or ["VIN", "Year"])
        for item in fields.values():
            item.page = self

    def locator(self, selector: str):
        return self.fields.get(selector, FakeLocator(0, selector, page=self))

    def get_by_label(self, name: str, exact: bool = False):
        return self.fields.get(name, FakeLocator(0, name, page=self))

    def fill(self, selector: str, value: str):
        require_unique_write_target(self, selector=selector)
        self.locator(selector).fill(value)


class FakePlaywrightLocator:
    fill = FakeLocator.fill


class PlaywrightPolicyFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.store = JobStore(Path(self.tmp.name) / "jobs.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_overlay_write_guard_matches_job_engine_source(self):
        self.assertEqual(ENGINE_GUARD.read_text(), OVERLAY_GUARD.read_text())

    def test_positional_first_fill_fails_closed_and_does_not_write(self):
        effective_dates = FakeLocator(2, "textbox:effective-date")
        self.assertTrue(locator_is_positional_guess(effective_dates.first))
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
            effective_dates.first.fill("2026-09-01")
        self.assertEqual(effective_dates.fills, [])
        self.assertEqual(effective_dates.first.fills, [])

    def test_ambiguous_label_fill_fails_closed_instead_of_guessing(self):
        name_fields = FakeLocator(2, "textbox:name")
        reason = unique_write_block_reason(name_fields)
        self.assertIn("matched 2 fields", reason)
        with self.assertRaisesRegex(RuntimeError, "refuse to guess"):
            name_fields.fill("WRONG-INSURED")
        self.assertEqual(name_fields.fills, [])

    def test_missing_field_fill_fails_closed(self):
        missing = FakeLocator(0, "textbox:policy-number")
        with self.assertRaisesRegex(RuntimeError, "matched 0 fields"):
            missing.fill("TEST-POLICY-A")
        self.assertEqual(missing.fills, [])

    def test_unique_labeled_field_may_write(self):
        policy_number = FakeLocator(1, "textbox:policy-number")
        policy_number.fill("TEST-POLICY-A")
        self.assertEqual(policy_number.fills, ["TEST-POLICY-A"])

    def test_page_fill_uses_the_same_unique_identity_rule(self):
        page = FakePage(
            {
                "input.name": FakeLocator(2, "input.name"),
                "input.policy-number": FakeLocator(1, "input.policy-number"),
            }
        )
        with self.assertRaisesRegex(RuntimeError, "matched 2 fields"):
            page.fill("input.name", "WRONG-INSURED")
        page.fill("input.policy-number", "TEST-POLICY-A")
        self.assertEqual(page.locator("input.policy-number").fills, ["TEST-POLICY-A"])

    def test_installed_guard_patches_fill_before_user_code_can_guess(self):
        scope = {"Locator": FakePlaywrightLocator}
        install_playwright_write_guards(scope)
        self.assertTrue(scope["_robie_unique_write_guard"])
        target = FakeLocator(2, "input.fein")
        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED"):
            FakePlaywrightLocator.fill(target, "12-3456789")
        self.assertEqual(target.fills, [])

    def test_generic_ezlynx_write_requires_matching_compiled_applicant_scope(self):
        class ScopedLocator:
            fill = FakeLocator.fill

        allowed_page = FakePage({})
        allowed_page.url = "https://app.ezlynx.com/web/account/220250093/policies"
        allowed = FakeLocator(1, "#PolicyNumber", page=allowed_page)
        scope = {"Locator": ScopedLocator}
        install_playwright_write_guards(scope)
        with patch.dict(
            os.environ,
            {"ROBIE_EZLYNX_WRITE_APPLICANT_ID": "220250093"},
            clear=False,
        ):
            ScopedLocator.fill(allowed, "SYNTHETIC-POLICY")
        self.assertEqual(allowed.fills, ["SYNTHETIC-POLICY"])

        wrong_page = FakePage({})
        wrong_page.url = "https://app.ezlynx.com/web/account/220250094/policies"
        wrong = FakeLocator(1, "#PolicyNumber", page=wrong_page)
        with patch.dict(
            os.environ,
            {"ROBIE_EZLYNX_WRITE_APPLICANT_ID": "220250093"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "EZLYNX_WRITE_SCOPE_REFUSED"):
                ScopedLocator.fill(wrong, "MUST-NOT-WRITE")
        self.assertEqual(wrong.fills, [])

    def test_numeric_form_entry_requires_visible_robie_test_policy_attestation(self):
        class EvidenceLocator:
            def __init__(self, *, count=1, visible=True, text="", href=None):
                self._count = count
                self._visible = visible
                self._text = text
                self._href = href

            def count(self):
                return self._count

            def is_visible(self):
                return self._visible

            def inner_text(self):
                return self._text

            def get_attribute(self, name):
                return self._href if name == "href" else None

        class EvidencePage:
            def __init__(self, *, account="ROBIE Test LLC", href=None, header=""):
                self.account = EvidenceLocator(
                    text=account,
                    href=href
                    or "https://app.ezlynx.com/web/account/220250093/overview",
                )
                self.body = EvidenceLocator(text=header)

            def locator(self, selector):
                if selector == 'a[title="Go to Applicant Overview"]':
                    return self.account
                if selector == "body":
                    return self.body
                return EvidenceLocator(count=0, visible=False)

        url = (
            "https://app.ezlynx.com/applicantportal/Policy/90000001/"
            "FormEntry/Index/70000001?prevApplied=70000001"
        )
        valid_header = (
            "Policy Number: TEST-HO-08312026-02 - Inactive "
            "Line of Business: Homeowners Term: 10/5/2024 - 10/5/2025 "
            "Full Term Premium: $1.00 Source: Manual"
        )
        self.assertIsNone(
            attested_test_form_entry_block_reason(
                EvidencePage(header=valid_header),
                url=url,
                requested_applicant_id="220250093",
            )
        )
        self.assertIsNone(
            attested_test_form_entry_block_reason(
                EvidencePage(
                    account="Example Client",
                    href="https://app.ezlynx.com/web/account/220250094/overview",
                    header=valid_header,
                ),
                url=url,
                requested_applicant_id="220250094",
            )
        )
        for page, applicant, marker in (
            (EvidencePage(account="A Real Client", header=valid_header), "220250093", "ROBIE Test"),
            (EvidencePage(header=valid_header.replace("TEST-HO-08312026-02", "REAL-01")), "220250093", "synthetic"),
            (EvidencePage(header=valid_header.replace("$1.00", "$1,796.00")), "220250093", "$1.00"),
            (EvidencePage(header=valid_header), "220250094", "different or unknown client"),
        ):
            reason = attested_test_form_entry_block_reason(
                page,
                url=url,
                requested_applicant_id=applicant,
            )
            self.assertIsNotNone(reason)
            self.assertIn("EZLYNX_WRITE_SCOPE_REFUSED", reason)
            self.assertIn(marker, reason)

    def test_timeout_on_hidden_combobox_fill_is_playwright_blocked_not_bare_timeout(self):
        """c31f9c69: unique hidden/combobox fill TimeoutError must HITL, not retry."""

        class PlaywrightTimeoutError(Exception):
            """playwright.sync_api.TimeoutError is not builtin TimeoutError."""

        PlaywrightTimeoutError.__name__ = "TimeoutError"
        PlaywrightTimeoutError.__module__ = "playwright.sync_api"

        class TimeoutComboboxLocator:
            def __init__(self):
                self.selector = "input[name='quotes.0.carrier_id']"
                self.fills = []

            def count(self):
                return 1

            def fill(self, value):
                raise PlaywrightTimeoutError(
                    "Timeout 30000ms exceeded.\n"
                    "waiting for locator(\"input[name='quotes.0.carrier_id']\") "
                    "to be visible; element is hidden / combobox"
                )

        class HiddenComboboxLocator(TimeoutComboboxLocator):
            def is_hidden(self):
                return True

            def is_visible(self):
                return False

            def get_attribute(self, name):
                return {
                    "aria-hidden": "true",
                    "type": "hidden",
                    "role": "combobox",
                }.get(name)

        class LocatorTimeout:
            fill = TimeoutComboboxLocator.fill

        class LocatorHidden:
            fill = HiddenComboboxLocator.fill

        hidden = HiddenComboboxLocator()
        hidden_reason = unwritable_control_block_reason(hidden)
        self.assertIsNotNone(hidden_reason)
        self.assertIn("PLAYWRIGHT_BLOCKED", hidden_reason)
        self.assertIn("quotes.0.carrier_id", hidden_reason)
        self.assertIn("ask Gemini then HITL Carlo", hidden_reason)
        self.assertIn("do not retry-loop", hidden_reason)

        for cls, factory in (
            (LocatorTimeout, TimeoutComboboxLocator),
            (LocatorHidden, HiddenComboboxLocator),
        ):
            scope = {"Locator": cls}
            install_playwright_write_guards(scope)
            target = factory()
            with self.assertRaises(RuntimeError) as ctx:
                cls.fill(target, "invented-carrier")
            msg = str(ctx.exception)
            self.assertIn("PLAYWRIGHT_BLOCKED", msg)
            self.assertIn("quotes.0.carrier_id", msg)
            self.assertIn("ask Gemini then HITL Carlo", msg)
            self.assertIn("do not retry-loop", msg)
            self.assertIn("HITL Carlo", msg)
            self.assertEqual(type(ctx.exception).__name__, "RuntimeError")
            self.assertNotIsInstance(ctx.exception, TimeoutError)
            self.assertEqual(target.fills, [])

    def test_ascend_hidden_field_uses_same_gemini_apply_or_hitl(self):
        page = FakePage(
            {"Carrier": FakeLocator(1, "Carrier")},
            title="Quote Details",
            visible_labels=["Carrier"],
        )
        page.url = "https://app.ascend.com/quotes/123"

        class HiddenAscendLocator:
            def __init__(self):
                self.selector = "input[name='quotes.0.carrier_id']"
                self.fills = []
                self.page = page

            def count(self):
                return 1

            def is_hidden(self):
                return True

            def is_visible(self):
                return False

            def get_attribute(self, name):
                return {
                    "aria-hidden": "true",
                    "type": "hidden",
                    "role": "combobox",
                }.get(name)

            def fill(self, value):
                self.fills.append(value)

        class LocatorHidden:
            fill = HiddenAscendLocator.fill

        asked = []

        def ask_hitl(*, dialog_title, visible_labels, block_reason, page_url=""):
            asked.append((dialog_title, list(visible_labels), block_reason, page_url))
            return {"action": "HITL", "field_label": None}

        scope = {"Locator": LocatorHidden, "ask_gemini_unique_field": ask_hitl}
        install_playwright_write_guards(scope)
        hidden = HiddenAscendLocator()
        with self.assertRaisesRegex(RuntimeError, "HITL Carlo"):
            LocatorHidden.fill(hidden, "invented-carrier")
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0][0], "Quote Details")
        self.assertEqual(asked[0][1], ["Carrier"])
        self.assertIn("hidden", asked[0][2])
        self.assertEqual(asked[0][3], "https://app.ascend.com/quotes/123")
        self.assertEqual(hidden.fills, [])
        self.assertEqual(page.fields["Carrier"].fills, [])

        asked.clear()

        def ask_apply(*, dialog_title, visible_labels, block_reason, page_url=""):
            asked.append((dialog_title, list(visible_labels), block_reason, page_url))
            return {"action": "APPLY", "field_label": "Carrier"}

        scope = {"Locator": LocatorHidden, "ask_gemini_unique_field": ask_apply}
        install_playwright_write_guards(scope)
        still_hidden = HiddenAscendLocator()
        LocatorHidden.fill(still_hidden, "travelers")
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0][3], "https://app.ascend.com/quotes/123")
        self.assertEqual(still_hidden.fills, [])
        self.assertEqual(page.fields["Carrier"].fills, ["travelers"])

    def test_blocked_write_asks_gemini_and_applies_only_a_unique_label(self):
        vin = FakeLocator(1, "VIN")
        year = FakeLocator(1, "Year")
        page = FakePage({"VIN": vin, "Year": year}, title="Add Vehicle")
        ambiguous = FakeLocator(2, "input", page=page)
        asked = []

        def ask_gemini(*, dialog_title, visible_labels, block_reason, page_url=""):
            asked.append((dialog_title, list(visible_labels), block_reason, page_url))
            return {"action": "APPLY", "field_label": "VIN"}

        scope = {
            "Locator": FakePlaywrightLocator,
            "ask_gemini_unique_field": ask_gemini,
        }
        install_playwright_write_guards(scope)
        FakePlaywrightLocator.fill(ambiguous, "1HGCM82633A004352")
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0][0], "Add Vehicle")
        self.assertEqual(asked[0][1], ["VIN", "Year"])
        self.assertEqual(asked[0][3], "")
        self.assertEqual(vin.fills, ["1HGCM82633A004352"])
        self.assertEqual(ambiguous.fills, [])
        self.assertEqual(year.fills, [])

    def test_blocked_write_hitls_carlo_when_gemini_is_unsure_or_not_unique(self):
        vin = FakeLocator(2, "VIN")
        page = FakePage({"VIN": vin}, title="Add Vehicle", visible_labels=["VIN"])
        ambiguous = FakeLocator(2, "input", page=page)

        def unsure(**_kwargs):
            return {"action": "HITL", "field_label": None}

        scope = {"Locator": FakePlaywrightLocator, "ask_gemini_unique_field": unsure}
        install_playwright_write_guards(scope)
        with self.assertRaisesRegex(RuntimeError, "HITL Carlo"):
            FakePlaywrightLocator.fill(ambiguous, "guess")
        self.assertEqual(ambiguous.fills, [])
        self.assertEqual(vin.fills, [])

        still_ambiguous = FakeLocator(2, "input", page=page)

        def names_non_unique(**_kwargs):
            return {"action": "APPLY", "field_label": "VIN"}

        scope = {
            "Locator": FakePlaywrightLocator,
            "ask_gemini_unique_field": names_non_unique,
        }
        install_playwright_write_guards(scope)
        with self.assertRaisesRegex(RuntimeError, "HITL Carlo"):
            FakePlaywrightLocator.fill(still_ambiguous, "guess")
        self.assertEqual(still_ambiguous.fills, [])
        self.assertEqual(vin.fills, [])

    def test_collect_blocked_dialog_drops_password_labels(self):
        page = FakePage(
            {},
            title="Sign in",
            visible_labels=["VIN", "Password", "Year"],
        )
        title, labels = collect_blocked_dialog(page)
        self.assertEqual(title, "Sign in")
        self.assertEqual(labels, ["VIN", "Year"])
        self.assertIsNone(
            consult_gemini_for_blocked_write(
                reason="PLAYWRIGHT_BLOCKED: matched 2 fields",
                page=page,
                ask_gemini=None,
            )
        )

    def test_ok_only_postcondition_cannot_complete(self):
        self.assertIn("success flag", expected_postcondition_missing({"ok": True}))
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua", "text": "set up the policy"},
            idempotency_key="ok-only",
        )
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        self.store.checkpoint(
            job["id"],
            "action",
            {"action": "hermes.google_chat_task", "destination": {"record_id": "acct-test"}},
        )
        self.store.add_evidence(
            job["id"],
            True,
            VerificationEvidence(
                "TEST",
                "destination",
                {"ok": True},
                {"ok": True},
                True,
                datetime.now(timezone.utc).isoformat(),
                "acct-test",
            ),
        )
        with self.assertRaises(PermissionError):
            self.store.transition(
                job["id"],
                JobStatus.COMPLETE,
                expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )
        self.assertNotEqual(self.store.get_job(job["id"])["status"], JobStatus.COMPLETE)

    def test_complete_requires_intended_record_identity(self):
        self.assertIn(
            "action target identity is missing",
            destination_identity_missing(
                locator="page",
                expected={"status": "done"},
                observed={"status": "done"},
                intended=None,
            ),
        )
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua"},
            idempotency_key="no-intended",
        )
        self.store.transition(job["id"], JobStatus.RUNNING, expected={JobStatus.PENDING})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.RUNNING})
        self.store.add_evidence(
            job["id"],
            True,
            VerificationEvidence(
                "TEST",
                "destination",
                {"status": "done"},
                {"status": "done"},
                True,
                datetime.now(timezone.utc).isoformat(),
                "page",
            ),
        )
        with self.assertRaisesRegex(PermissionError, "action target identity is missing"):
            self.store.transition(
                job["id"],
                JobStatus.COMPLETE,
                expected={JobStatus.VERIFYING},
                authority=VERIFIER_AUTHORITY,
            )

    def test_wrong_policy_record_cannot_complete(self):
        expected = {
            "record_id": "acct-test",
            "policy_number": "TEST-POLICY-A",
            "status": "Quoted",
        }
        observed = {
            "record_id": "acct-test",
            "policy_number": "TEST-POLICY-B",
            "status": "Quoted",
        }
        self.assertIn("policy_number", postcondition_mismatch(expected, observed) or "")
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua", "record_id": "acct-test"},
            idempotency_key="wrong-policy",
        )

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "hermes.google_chat_task", {"record_id": "acct-test"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "FRESH_PAGE_READBACK",
                        "destination",
                        expected,
                        observed,
                        True,
                        datetime.now(timezone.utc).isoformat(),
                        "acct-test",
                    ),
                )

        final = JobEngine(
            self.store,
            {"hermes-cua": Worker()},
            {"hermes.google_chat_task": Verifier()},
        ).run(job["id"])
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)
        self.assertIn("policy_number", str(final.get("last_error") or ""))

    def test_nothing_happened_cannot_complete_from_worker_receipt(self):
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua", "record_id": "acct-test"},
            idempotency_key="nothing-happened",
        )

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "hermes.google_chat_task", {"record_id": "acct-test"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "FRESH_PAGE_READBACK",
                        "destination",
                        {"record_id": "acct-test", "status": "Quoted"},
                        {"record_id": "acct-test"},
                        True,
                        datetime.now(timezone.utc).isoformat(),
                        "acct-test",
                    ),
                )

        final = JobEngine(
            self.store,
            {"hermes-cua": Worker()},
            {"hermes.google_chat_task": Verifier()},
        ).run(job["id"])
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)

    def test_chat_policy_setup_claim_without_evidence_is_not_success(self):
        job_id = open_chat_job(
            str(self.store.path),
            "spaces/s/messages/policy-setup",
            "Please set up the insurance policy in EZLynx",
        )
        self.assertIsNotNone(job_id)
        response = guard_chat_response(
            str(self.store.path),
            job_id,
            "I set up the policy. The work is done.",
        )
        self.assertIn("FAILED", response)
        self.assertNotIn("— COMPLETE", response)
        self.assertNotIn("I set up the policy", response)
        self.assertIn("PLAYWRIGHT_SILENT", self.store.get_job(job_id)["last_error"])
        self.assertEqual(self.store.get_job(job_id)["status"], JobStatus.FAILED)

    def test_unbound_policy_success_claim_is_not_reported(self):
        response = guard_chat_response(
            str(self.store.path),
            None,
            "I set up the policy in EZLynx.",
        )
        self.assertIn("not treating this as successful", response)
        self.assertNotIn("I set up the policy", response)

    def test_matching_intended_record_still_completes(self):
        expected = {
            "record_id": "acct-test",
            "policy_number": "TEST-POLICY-A",
            "status": "Quoted",
        }
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua", "record_id": "acct-test"},
            idempotency_key="right-policy",
        )

        class Worker:
            def perform(self, current, *, idempotency_key):
                return WorkerResult(True, "hermes.google_chat_task", {"record_id": "acct-test"})

        class Verifier:
            def verify(self, current, action):
                return VerificationResult(
                    True,
                    VerificationEvidence(
                        "FRESH_PAGE_READBACK",
                        "destination",
                        expected,
                        dict(expected),
                        True,
                        datetime.now(timezone.utc).isoformat(),
                        "acct-test",
                    ),
                )

        final = JobEngine(
            self.store,
            {"hermes-cua": Worker()},
            {"hermes.google_chat_task": Verifier()},
        ).run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)

    def test_require_complete_postcondition_rejects_ok_only_even_with_authority(self):
        with self.assertRaises(PermissionError):
            require_complete_postcondition(
                current=JobStatus.VERIFYING,
                authority=VERIFIER_AUTHORITY,
                verified=True,
                authoritative=True,
                expected={"ok": True},
                observed={"ok": True},
                captured_at=datetime.now(timezone.utc).isoformat(),
                evidence_ref="sha",
                locator="acct-test",
                job_id="job-1",
                verifier_authority=VERIFIER_AUTHORITY,
                intended="acct-test",
            )

    def test_production_release_stays_fail(self):
        self.assertEqual(production_release_decision(), "FAIL")
        self.assertEqual(
            production_release_decision(independent_reviewer_pass=True), "FAIL"
        )


if __name__ == "__main__":
    unittest.main()
