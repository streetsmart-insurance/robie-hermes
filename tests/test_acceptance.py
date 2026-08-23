from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from robie_job_engine.engine import JobEngine
from robie_job_engine.ezlynx import EzlynxDestinationVerifier, HermesCuaEzlynxWorker
from robie_job_engine.models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from robie_job_engine.store import JobStore


class FakeEzlynx:
    def __init__(self):
        self.server = {}
        self.options = {"user-7": "Ann", "user-71": "Ann Marie", "acct-9": "Acme Test", "label-2": "Renewal"}
        self.dialog_after = 0
        self.polls = 0
        self.persist = True
        self.pending = None
        self.calls = []

    def exact_option(self, *, stable_id, exact_text, scope=None):
        if self.options.get(stable_id) != exact_text:
            raise LookupError("stable selector and exact visible text did not identify one target")
        self.pending = stable_id
        self.calls.append(("exact_option", stable_id, exact_text, scope))
        return stable_id

    def click(self, target):
        return None

    def wait_interactable(self, *, role, name, timeout_ms, scope=None):
        self.polls += 1
        self.calls.append(("wait_interactable", role, name, scope))
        return f"{role}:{name}:{scope}"

    def wait_frame_interactable(self, *, src_contains, heading, button, timeout_ms):
        self.polls += 1
        if self.polls <= self.dialog_after:
            # A real port polls attached + visible + enabled until timeout.
            self.polls = self.dialog_after + 1
        self.calls.append(("wait_frame", src_contains, heading, button))
        return "move-frame"

    def fill_like_user(self, target, value):
        self.pending_label = value

    def submit(self, *, idempotency_key):
        if self.persist:
            self.last_key = idempotency_key
        return {"request_id": "req-1"}

    def api_state(self, action_type, expected):
        return self.server.get((action_type, expected["resource_id"]))

    def fresh_page_state(self, action_type, expected):
        return self.server.get((action_type, expected["resource_id"]), {})


class RecordingWorker:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def perform(self, job, *, idempotency_key):
        self.calls += 1
        return self.result


class StaticVerifier:
    def __init__(self, verified, authoritative=True, retryable=False):
        self.verified = verified
        self.authoritative = authoritative
        self.retryable = retryable

    def verify(self, job, action):
        evidence = VerificationEvidence("API", "destination", action["destination"], action["destination"] if self.verified else {}, self.authoritative, datetime.now(timezone.utc).isoformat())
        return VerificationResult(self.verified, evidence, self.retryable)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tmp.name) / "jobs.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_wrong_loose_reassignment_target_can_never_false_complete(self):
        result = WorkerResult(True, "ezlynx.reassign", {"resource_id": "task-1", "assignee_id": "user-7"})
        worker = RecordingWorker(result)
        engine = JobEngine(self.store, {"hermes-cua": worker}, {"ezlynx.reassign": StaticVerifier(False)})
        job = self.store.create_job("ezlynx.reassign", {"worker": "hermes-cua"}, max_attempts=1)
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)
        self.assertEqual(worker.calls, 1)

    def test_reassignment_uses_stable_id_not_partial_text(self):
        browser = FakeEzlynx()
        worker = HermesCuaEzlynxWorker(browser)
        job = {"action_type": "ezlynx.reassign", "payload": {"resource_id": "task-1", "assignee_id": "user-7", "assignee_name": "Ann"}}
        result = worker.perform(job, idempotency_key="same-key")
        self.assertEqual(result.destination["assignee_id"], "user-7")

    def test_angular_dialog_race_waits_for_interactable_dialog(self):
        browser = FakeEzlynx()
        browser.dialog_after = 3
        worker = HermesCuaEzlynxWorker(browser)
        job = {"action_type": "ezlynx.move_document", "payload": {"account_id": "account-4", "document_id": "doc-1", "document_name": "policy.pdf", "move_control": "move", "destination_id": "acct-9", "destination_name": "Acme Test"}}
        result = worker.perform(job, idempotency_key="move-key")
        self.assertTrue(result.succeeded)
        self.assertGreaterEqual(browser.polls, 1)
        self.assertIn(("wait_frame", "/Applicant/account-4/DocumentLibrary/MoveDocument", "Select the folder you would like to move your file to...", "Move"), browser.calls)

    def test_local_label_value_without_server_persistence_is_unverified(self):
        result = WorkerResult(True, "ezlynx.apply_label", {"resource_id": "doc-1", "label_id": "label-2"})
        engine = JobEngine(self.store, {"hermes-cua": RecordingWorker(result)}, {"ezlynx.apply_label": StaticVerifier(False)})
        job = self.store.create_job("ezlynx.apply_label", {"worker": "hermes-cua"}, max_attempts=1)
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)

    def test_label_uses_real_option_and_apply_not_direct_value_mutation(self):
        browser = FakeEzlynx()
        worker = HermesCuaEzlynxWorker(browser)
        job = {
            "action_type": "ezlynx.apply_label",
            "payload": {
                "account_id": "account-4",
                "resource_id": "doc-1",
                "document_name": "policy.pdf",
                "label_control": "add-label",
                "label_id": "label-2",
                "label": "Renewal",
            },
        }
        result = worker.perform(job, idempotency_key="label-key")
        self.assertTrue(result.succeeded)
        self.assertIn(("wait_interactable", "textbox", "Search Labels", None), browser.calls)
        self.assertIn(("wait_interactable", "button", "Apply", None), browser.calls)

    def test_verified_authoritative_readback_is_only_path_to_complete(self):
        result = WorkerResult(True, "ezlynx.apply_label", {"resource_id": "doc-1", "label_id": "label-2"})
        engine = JobEngine(self.store, {"hermes-cua": RecordingWorker(result)}, {"ezlynx.apply_label": StaticVerifier(True)})
        job = self.store.create_job("ezlynx.apply_label", {"worker": "hermes-cua"})
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)

    def test_deduplication_and_resume_do_not_repeat_completed_action(self):
        result = WorkerResult(True, "ezlynx.apply_label", {"resource_id": "doc-1", "label_id": "label-2"})
        worker = RecordingWorker(result)
        engine = JobEngine(self.store, {"hermes-cua": worker}, {"ezlynx.apply_label": StaticVerifier(False, retryable=True)})
        first = self.store.create_job("ezlynx.apply_label", {"worker": "hermes-cua"}, idempotency_key="message-123", max_attempts=2)
        duplicate = self.store.create_job("ezlynx.apply_label", {"worker": "hermes-cua"}, idempotency_key="message-123", max_attempts=2)
        self.assertEqual(first["id"], duplicate["id"])
        waiting = engine.run(first["id"])
        self.assertEqual(waiting["status"], JobStatus.RETRY_WAIT)
        self.store.transition(first["id"], JobStatus.VERIFYING, expected={JobStatus.RETRY_WAIT})
        engine.run(first["id"])
        self.assertEqual(worker.calls, 1)

    def test_pause_resume_preserves_verification_checkpoint(self):
        self.store.create_job("ezlynx.move_document", {"worker": "hermes-cua"}, idempotency_key="pause-me")
        job = self.store.create_job("ezlynx.move_document", {"worker": "hermes-cua"}, idempotency_key="pause-me")
        self.store.checkpoint(job["id"], "action", {"action": "ezlynx.move_document", "destination": {}})
        self.store.transition(job["id"], JobStatus.VERIFYING, expected={JobStatus.PENDING})
        self.store.pause(job["id"])
        resumed = self.store.resume(job["id"])
        self.assertEqual(resumed["status"], JobStatus.VERIFYING)


if __name__ == "__main__":
    unittest.main()
