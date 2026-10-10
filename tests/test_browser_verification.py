"""Contracts for reusable browser verification + CDP runtime wiring."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.browser_verification import (
    ACTION_PRECONDITIONS,
    classify_browser_retry,
    plan_browser_verification,
)
from robie_job_engine.ezlynx import EZLYNX_REQUIRED_FIELDS, HermesCuaEzlynxWorker
from robie_job_engine.ezlynx_cdp_port import maybe_build_ezlynx_cdp_ports
from robie_job_engine.test_runtime import build_runtime_engine
from robie_job_engine.store import JobStore

from durable_temp import durable_temporary_directory


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "ezlynx-document-actions" / "SKILL.md"
DEPLOY_SKILL = ROOT / "deploy" / "hermes" / "skills" / "ezlynx-document-actions" / "SKILL.md"


class BrowserVerificationContractTests(unittest.TestCase):
    def test_preconditions_cover_three_actions(self):
        for action in (
            "ezlynx.reassign",
            "ezlynx.move_document",
            "ezlynx.apply_label",
        ):
            self.assertIn(action, ACTION_PRECONDITIONS)
            self.assertEqual(
                EZLYNX_REQUIRED_FIELDS[action], ACTION_PRECONDITIONS[action]
            )

    def test_plan_flags_missing_move_control(self):
        plan = plan_browser_verification(
            "ezlynx.move_document",
            {
                "document_id": "doc-1",
                "document_name": "a.pdf",
                "account_id": "220250093",
                "destination_id": "folder-1",
                "destination_name": "Claims",
            },
        )
        self.assertFalse(plan.preconditions_ok)
        self.assertIn("move_control", plan.missing_preconditions)

    def test_retry_classifies_network_vs_locator(self):
        self.assertTrue(classify_browser_retry("net::ERR_CONNECTION_RESET")["retryable"])
        self.assertFalse(
            classify_browser_retry("PLAYWRIGHT_BLOCKED: click target matched 3")[
                "retryable"
            ]
        )

    def test_worker_holds_for_missing_label_control(self):
        class _Browser:
            def exact_option(self, **kwargs):
                raise AssertionError("must not run")

            def click(self, target):
                raise AssertionError("must not run")

            def wait_interactable(self, **kwargs):
                raise AssertionError("must not run")

            def fill_like_user(self, target, value):
                raise AssertionError("must not run")

            def submit(self, *, idempotency_key):
                raise AssertionError("must not run")

        worker = HermesCuaEzlynxWorker(_Browser())
        result = worker.perform(
            {
                "action_type": "ezlynx.apply_label",
                "payload": {
                    "account_id": "220250093",
                    "resource_id": "doc-1",
                    "document_name": "a.pdf",
                    "label_id": "label-2",
                    "label": "Renewal",
                },
            },
            idempotency_key="k1",
        )
        self.assertFalse(result.succeeded)
        self.assertIn("label_control", result.error or "")

    def test_cdp_ports_only_on_test_with_url(self):
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
            self.assertEqual(maybe_build_ezlynx_cdp_ports(), (None, None))
        with patch.dict(
            os.environ,
            {"ROBIE_ENV": "TEST", "ROBIE_PLAYWRIGHT_CDP_URL": ""},
            clear=False,
        ):
            os.environ.pop("ROBIE_PLAYWRIGHT_CDP_URL", None)
            os.environ.pop("ROBIE_BROWSER_CDP_URL", None)
            self.assertEqual(maybe_build_ezlynx_cdp_ports(), (None, None))

    def test_runtime_stays_unavailable_without_cdp(self):
        with durable_temporary_directory() as tmp:
            store = JobStore(Path(tmp) / "jobs.db")
            with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
                os.environ.pop("ROBIE_PLAYWRIGHT_CDP_URL", None)
                os.environ.pop("ROBIE_BROWSER_CDP_URL", None)
                engine = build_runtime_engine(store)
            worker = engine.workers["hermes-cua"]
            job = store.create_job(
                "ezlynx.apply_label",
                {"worker": "hermes-cua", "account_id": "220250093"},
            )
            result = worker.perform(job, idempotency_key="x")
            self.assertFalse(result.succeeded)
            self.assertIn("not registered", result.error or "")

    def test_skill_documents_evidence_and_actions(self):
        text = SKILL.read_text(encoding="utf-8")
        deploy = DEPLOY_SKILL.read_text(encoding="utf-8")
        for needle in (
            "ezlynx.reassign",
            "ezlynx.move_document",
            "ezlynx.apply_label",
            "EZLYNX_API_READBACK",
            "FRESH_PAGE_READBACK",
            "COMPLETE",
        ):
            self.assertIn(needle, text)
            self.assertIn(needle, deploy)


if __name__ == "__main__":
    unittest.main()
