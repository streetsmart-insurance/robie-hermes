"""Action-level Test gate: Production must refuse before any Ascend API POST."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.action_gate import (
    CREATE_PROGRAM_ACTION,
    REFUSAL_TOKEN,
    REQUIRED_CLEAN_TEST_PASSES,
    apply_action_gate,
    classify_action,
    has_clean_test_pass,
    hold_reason_for_job,
    is_action_gate_refusal,
    record_test_action_pass,
    refuse_playwright_start,
    required_clean_test_passes,
)
from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.engine import JobEngine
from robie_job_engine.job_type_gate import (
    REQUIRED_CLEAN_TEST_JOBS,
    is_job_type_production_ready,
)
from robie_job_engine.recording import RecordingStore
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import dispatch_operational_chat
from robie_job_engine.models import JobStatus, WorkerResult


CHAT_SHAPED_ASCEND = (
    "@robie create a program in Ascend for PAWIVA premium finance"
)
SKILL = Path("skills/ascend-api-create-program/SKILL.md")


class ActionGateTests(unittest.TestCase):
    def test_n_is_one_for_an_action_and_three_for_a_new_job_type(self):
        self.assertEqual(REQUIRED_CLEAN_TEST_PASSES, 1)
        self.assertEqual(required_clean_test_passes(), 1)
        self.assertEqual(REQUIRED_CLEAN_TEST_JOBS, 3)
        self.assertFalse(is_job_type_production_ready("ascend.locator_artifact_audit"))
        self.assertTrue(is_job_type_production_ready("ezlynx.commercial_auto"))
        self.assertIn("production_ready: false", SKILL.read_text(encoding="utf-8"))
        self.assertNotIn("production_ready: true", SKILL.read_text(encoding="utf-8"))

    def test_chat_job_type_does_not_hide_ascend_create_program(self):
        action = classify_action(
            CHAT_SHAPED_ASCEND,
            payload={
                "text": CHAT_SHAPED_ASCEND,
                "skill": "ascend-api-create-program",
            },
            action_type="hermes.google_chat_task",
        )
        self.assertEqual(action, CREATE_PROGRAM_ACTION)
        commercial = classify_action(
            "finish the commercial auto quote in EZLynx",
            payload={"text": "finish the commercial auto quote in EZLynx"},
            action_type="hermes.google_chat_task",
        )
        self.assertNotEqual(commercial, CREATE_PROGRAM_ACTION)

    def test_production_without_record_refuses_before_api(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            passes = Path(tmp) / "passes"
            passes.mkdir()
            with patch("robie_job_engine.action_gate.PASSES_DIR", passes):
                with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
                    job_id = open_chat_job(
                        db,
                        "spaces/s/messages/807f8920-shaped",
                        CHAT_SHAPED_ASCEND,
                        requested_by="Carlo Ferrara",
                        conversation_id="spaces/action-gate",
                    )
                    hermes: list[str] = []
                    consumed = dispatch_operational_chat(
                        db, job_id, hermes=lambda: hermes.append("hermes")
                    )
            store = JobStore(db)
            job = store.get_job(job_id)
            self.assertEqual(job["status"], JobStatus.FAILED.value)
            self.assertIn("ASCEND_UNAVAILABLE", job["last_error"])
            self.assertNotIn("PLAYWRIGHT_BLOCKED", job["last_error"])
            self.assertFalse(is_action_gate_refusal(job))
            self.assertTrue(consumed)
            self.assertEqual(hermes, [])
            self.assertIsNone(RecordingStore(db).latest(job_id))
            self.assertFalse(has_clean_test_pass(CREATE_PROGRAM_ACTION))

    def test_test_env_fails_closed_when_ascend_is_excluded(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
                job_id = open_chat_job(
                    db,
                    "spaces/s/messages/test-allow",
                    CHAT_SHAPED_ASCEND,
                    requested_by="Carlo Ferrara",
                    conversation_id="spaces/action-gate-test",
                )
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["action_type"], "hermes.unavailable")
            self.assertEqual(job["status"], JobStatus.FAILED.value)
            self.assertIn("ASCEND_UNAVAILABLE", job["last_error"])
            self.assertFalse(is_action_gate_refusal(job))

    def test_recorded_clean_test_pass_unblocks_production_n1(self):
        with durable_temporary_directory() as tmp:
            passes = Path(tmp) / "passes"
            passes.mkdir()
            with patch("robie_job_engine.action_gate.PASSES_DIR", passes):
                self.assertFalse(has_clean_test_pass(CREATE_PROGRAM_ACTION))
                record_test_action_pass(
                    CREATE_PROGRAM_ACTION,
                    job_id="hermes-test-punch-list",
                    verdict="PASS",
                )
                self.assertTrue(has_clean_test_pass(CREATE_PROGRAM_ACTION))
                reason = hold_reason_for_job(
                    {
                        "id": "after-pass",
                        "action_type": "hermes.google_chat_task",
                        "payload": {"text": CHAT_SHAPED_ASCEND},
                    },
                    env="PRODUCTION",
                )
                self.assertIsNone(reason)

    def test_leftover_production_ids_cannot_retry_around_the_gate(self):
        leftover = hold_reason_for_job(
            {
                "id": "807f8920-aaaa-bbbb",
                "action_type": "hermes.google_chat_task",
                "payload": {"text": "RETRY"},
            },
            env="PRODUCTION",
        )
        self.assertIsNotNone(leftover)
        self.assertIn("807f8920", leftover or "")
        self.assertIn(REFUSAL_TOKEN, leftover or "")
        other = hold_reason_for_job(
            {
                "id": "38c0fa79",
                "action_type": "hermes.google_chat_task",
                "payload": {"text": CHAT_SHAPED_ASCEND},
            },
            env="PRODUCTION",
        )
        self.assertIsNotNone(other)
        self.assertIn("38c0fa79", other or "")

    def test_engine_refuses_before_worker_and_api_request(self):
        calls = {"n": 0}

        class Worker:
            def perform(self, job, *, idempotency_key):
                calls["n"] += 1
                return WorkerResult(True, job["action_type"], {"record_id": "x"})

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": CHAT_SHAPED_ASCEND, "worker": "hermes-cua"},
            )
            with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
                engine = JobEngine(store, {"hermes-cua": Worker()}, {})
                final = engine.run(job["id"])
                pw = refuse_playwright_start(
                    'page.goto("https://dashboard.useascend.com/create/new")'
                )
            self.assertEqual(final["status"], JobStatus.FAILED.value)
            self.assertIn(REFUSAL_TOKEN, final["last_error"])
            self.assertEqual(calls["n"], 0)
            self.assertIsNotNone(pw)
            self.assertIn(REFUSAL_TOKEN, pw or "")
            self.assertNotIn("PLAYWRIGHT_BLOCKED", pw or "")

    def test_apply_action_gate_is_the_one_place_chat_must_pass(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "hermes.google_chat_task",
                {"text": CHAT_SHAPED_ASCEND},
            )
            with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
                refused = apply_action_gate(store, job)
            self.assertIsNotNone(refused)
            self.assertEqual(refused["status"], JobStatus.FAILED.value)
            self.assertTrue(is_action_gate_refusal(refused))

    def test_docs_say_the_engine_refuses_not_a_memory_item(self):
        state = Path("CURRENT_STATE.md").read_text(encoding="utf-8")
        release = Path("RELEASE_PROCESS.md").read_text(encoding="utf-8")
        for text in (state, release):
            flat = " ".join(text.replace("**", "").replace("`", "").split())
            self.assertIn("Job Engine refuses", flat)
            self.assertIn("not a memory item", flat)
            self.assertIn("ascend.create_program", text)
            self.assertIn("ACTION_GATE_REFUSED", text)
            self.assertIn("HITL after a miss is not the gate", flat)
            self.assertIn("generic Chat job type must not hide the action", flat)


if __name__ == "__main__":
    unittest.main()
