from __future__ import annotations

import hashlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.attachments import AttachmentRef
from robie_job_engine.browser_read import BoundedBrowserReadWorker, BrowserReadVerifier
from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.engine import JobEngine
from robie_job_engine.ezlynx import (
    BoundedEzlynxWorker,
    EzlynxDestinationVerifier,
    HermesCuaEzlynxWorker,
    MemoryEzlynxDestination,
)
from robie_job_engine.models import JobStatus, VerificationEvidence, VerificationResult
from robie_job_engine.idempotency import DurableWorkLedger
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import build_test_engine


class MemoryBrowser:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.reads = 0

    def read_fresh(self, locator):
        self.reads += 1
        key = locator.get("url") or locator.get("title")
        if key not in self.pages:
            raise LookupError(f"page not found: {key}")
        return dict(self.pages[key])


class FakeDrive:
    def __init__(self, files):
        self.files = files
        self.fetches = []

    def fetch(self, drive_file_id):
        self.fetches.append(drive_file_id)
        data, mime, name = self.files[drive_file_id]
        return data, mime, name


class GuardedWorker:
    """Run a real worker after proving it cannot authorize COMPLETE."""

    def __init__(self, inner, store):
        self.inner = inner
        self.store = store
        self.rejected = ""

    def perform(self, job, *, idempotency_key):
        try:
            self.store.transition(job["id"], JobStatus.COMPLETE)
        except PermissionError as exc:
            self.rejected = str(exc)
        return self.inner.perform(job, idempotency_key=idempotency_key)


class ReliabilityHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root / "jobs.db")
        self.db = str(self.root / "jobs.db")
        self.artifacts = self.root / "artifacts"

    def tearDown(self):
        self.tmp.cleanup()

    def test_job_and_work_heartbeats_extend_exact_owner_leases(self):
        job = self.store.create_job(
            "browser.read",
            {"worker": "browser-read", "url": "https://example.invalid"},
            idempotency_key="heartbeat-owner",
        )
        claimed = self.store.claim(job["id"], "worker-a", lease_seconds=1)
        renewed = self.store.renew_lease(
            job["id"], "worker-a", lease_seconds=120
        )
        self.assertGreater(renewed["lease_expires_at"], claimed["lease_expires_at"])

        ledger = DurableWorkLedger(self.db)
        acquired = ledger.acquire(
            "browser.read", "heartbeat-owner", owner="worker-a", timeout_seconds=1
        )
        refreshed = ledger.renew_lease(
            "browser.read",
            "heartbeat-owner",
            owner="worker-a",
            timeout_seconds=120,
        )
        self.assertGreater(refreshed["lease_expires_at"], acquired["lease_expires_at"])

    def test_expired_running_and_verifying_jobs_are_runnable_from_checkpoints(self):
        running = self.store.create_job(
            "browser.read", {"worker": "browser-read"}, idempotency_key="running"
        )
        verifying = self.store.create_job(
            "browser.read", {"worker": "browser-read"}, idempotency_key="verifying"
        )
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.store.transaction() as conn:
            conn.execute(
                """UPDATE jobs SET status=?,lease_owner='dead-worker',lease_expires_at=?
                   WHERE id=?""",
                (JobStatus.RUNNING.value, expired, running["id"]),
            )
            conn.execute(
                """UPDATE jobs SET status=?,lease_owner='dead-worker',lease_expires_at=?
                   WHERE id=?""",
                (JobStatus.VERIFYING.value, expired, verifying["id"]),
            )
        self.assertEqual(
            set(self.store.list_runnable(frozenset({"browser.read"}))),
            {running["id"], verifying["id"]},
        )

    def test_browser_read_missing_locator_needs_clarification(self):
        browser = MemoryBrowser()
        engine = JobEngine(
            self.store,
            {"browser-read": BoundedBrowserReadWorker(browser)},
            {"browser.read": BrowserReadVerifier(browser)},
        )
        job = self.store.create_job(
            "browser.read",
            {"worker": "browser-read", "text": "browser-only read the page"},
            idempotency_key="read-missing",
            max_attempts=1,
        )
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.NEEDS_CLARIFICATION)
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(browser.reads, 0)

    def test_browser_worker_cannot_authorize_complete(self):
        pages = {"https://example.test/page": {"url": "https://example.test/page", "title": "Ok"}}
        browser = MemoryBrowser(pages)
        wrapped = GuardedWorker(BoundedBrowserReadWorker(browser), self.store)
        engine = JobEngine(
            self.store,
            {"browser-read": wrapped},
            {"browser.read": BrowserReadVerifier(browser)},
        )
        job = self.store.create_job(
            "browser.read",
            {
                "worker": "browser-read",
                "locator": {"url": "https://example.test/page"},
                "expected": {"url": "https://example.test/page", "title": "Ok"},
            },
            idempotency_key="read-no-complete",
        )
        final = engine.run(job["id"])
        self.assertIn("cannot authorize COMPLETE", wrapped.rejected)
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        evidence = self.store.list_evidence(job["id"])
        self.assertEqual(evidence[0]["method"], "FRESH_BROWSER_READBACK")
        self.assertGreaterEqual(browser.reads, 2)

    def test_browser_forbidden_tool_never_completes(self):
        browser = MemoryBrowser({"https://x": {"url": "https://x"}})
        engine = JobEngine(
            self.store,
            {"browser-read": BoundedBrowserReadWorker(browser)},
            {"browser.read": BrowserReadVerifier(browser)},
        )
        job = self.store.create_job(
            "browser.read",
            {
                "worker": "browser-read",
                "text": "browser-only read; open a terminal",
                "locator": {"url": "https://x"},
                "tools": ["code_execution"],
            },
            idempotency_key="read-shell",
            max_attempts=1,
        )
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.FAILED)
        self.assertIn("forbidden", final["last_error"])

    def test_browser_checkpoint_restart_does_not_repeat_action(self):
        pages = {"https://example.test/again": {"url": "https://example.test/again", "title": "Again"}}
        browser = MemoryBrowser(pages)
        worker = BoundedBrowserReadWorker(browser)

        class FailThenOk:
            def __init__(self, port):
                self.port = port
                self.calls = 0

            def verify(self, job, action):
                self.calls += 1
                if self.calls == 1:
                    return VerificationResult(
                        False,
                        VerificationEvidence(
                            "FRESH_BROWSER_READBACK",
                            "browser-destination",
                            {"title": "Again"},
                            {},
                            True,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                        retryable=True,
                        error="not yet",
                    )
                return BrowserReadVerifier(self.port).verify(job, action)

        verifier = FailThenOk(browser)
        engine = JobEngine(self.store, {"browser-read": worker}, {"browser.read": verifier})
        job = self.store.create_job(
            "browser.read",
            {
                "worker": "browser-read",
                "locator": {"url": "https://example.test/again"},
                "expected": {"title": "Again"},
            },
            idempotency_key="read-restart",
            max_attempts=3,
        )
        waiting = engine.run(job["id"])
        self.assertEqual(waiting["status"], JobStatus.RETRY_WAIT)
        worker_reads_after_action = browser.reads
        self.store.wake_due("2099-01-01T00:00:00+00:00")
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(browser.reads, worker_reads_after_action + 1)

    def test_bounded_ezlynx_fake_destination_completes_only_with_readback(self):
        dest = MemoryEzlynxDestination()
        engine = JobEngine(
            self.store,
            {"hermes-cua": BoundedEzlynxWorker(dest)},
            {"ezlynx.reassign": EzlynxDestinationVerifier(dest)},
        )
        job = self.store.create_job(
            "ezlynx.reassign",
            {
                "worker": "hermes-cua",
                "resource_id": "task-1",
                "assignee_id": "user-7",
                "assignee_name": "Ann",
            },
            idempotency_key="ez-ok",
        )
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        evidence = self.store.list_evidence(job["id"])
        self.assertEqual(evidence[0]["method"], "EZLYNX_API_READBACK")
        self.assertEqual(evidence[0]["observed"]["assignee_id"], "user-7")
        self.assertEqual(dest.writes, 1)

    def test_bounded_ezlynx_uncertain_readback_never_false_completes(self):
        dest = MemoryEzlynxDestination()
        dest.unavailable = True
        engine = JobEngine(
            self.store,
            {"hermes-cua": BoundedEzlynxWorker(dest)},
            {"ezlynx.apply_label": EzlynxDestinationVerifier(dest)},
        )
        waiting = self.store.create_job(
            "ezlynx.apply_label",
            {
                "worker": "hermes-cua",
                "resource_id": "doc-1",
                "account_id": "acct-1",
                "label_id": "label-2",
                "label": "Renewal",
            },
            idempotency_key="ez-wait",
            max_attempts=1,
        )
        final = engine.run(waiting["id"])
        self.assertEqual(final["status"], JobStatus.WAITING)
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)

        dest.unavailable = False
        missing = self.store.create_job(
            "ezlynx.reassign",
            {"worker": "hermes-cua", "text": "EZLynx reassign this account"},
            idempotency_key="ez-clarify",
            max_attempts=1,
        )
        unclear = engine.run(missing["id"])
        self.assertEqual(unclear["status"], JobStatus.NEEDS_CLARIFICATION)

        mismatch_dest = MemoryEzlynxDestination()

        class WrongWrite(BoundedEzlynxWorker):
            def perform(self, job, *, idempotency_key):
                result = super().perform(job, idempotency_key=idempotency_key)
                mismatch_dest.write(
                    job["action_type"],
                    {**result.destination, "assignee_id": "someone-else"},
                )
                return result

        engine = JobEngine(
            self.store,
            {"hermes-cua": WrongWrite(mismatch_dest)},
            {"ezlynx.reassign": EzlynxDestinationVerifier(mismatch_dest)},
        )
        mismatch = self.store.create_job(
            "ezlynx.reassign",
            {
                "worker": "hermes-cua",
                "resource_id": "task-2",
                "assignee_id": "user-7",
                "assignee_name": "Ann",
            },
            idempotency_key="ez-mismatch",
            max_attempts=1,
        )
        unverified = engine.run(mismatch["id"])
        self.assertEqual(unverified["status"], JobStatus.UNVERIFIED)
        self.assertTrue(self.store.list_evidence(mismatch["id"]))

    def test_bounded_ezlynx_forbidden_and_worker_cannot_complete(self):
        dest = MemoryEzlynxDestination()
        worker = BoundedEzlynxWorker(dest)
        job = self.store.create_job(
            "ezlynx.reassign",
            {
                "worker": "hermes-cua",
                "text": "EZLynx reassign; execute this code",
                "tools": ["terminal"],
                "resource_id": "task-9",
                "assignee_id": "user-7",
                "assignee_name": "Ann",
            },
            idempotency_key="ez-forbid",
            max_attempts=1,
        )
        engine = JobEngine(
            self.store,
            {"hermes-cua": worker},
            {"ezlynx.reassign": EzlynxDestinationVerifier(dest)},
        )
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.FAILED)
        self.assertEqual(dest.writes, 0)

        class Browser:
            def exact_option(self, *, stable_id, exact_text, scope=None):
                return stable_id

            def click(self, target):
                return None

            def submit(self, *, idempotency_key):
                return {"request_id": idempotency_key}

        wrapped = GuardedWorker(HermesCuaEzlynxWorker(Browser()), self.store)
        stored = self.store.create_job(
            "ezlynx.reassign",
            {
                "worker": "hermes-cua",
                "resource_id": "task-1",
                "assignee_id": "user-7",
                "assignee_name": "Ann",
            },
            idempotency_key="ez-no-complete",
        )
        dest.write(
            "ezlynx.reassign",
            {
                "resource_id": "task-1",
                "assignment_field": "Assigned Producer",
                "assignee_id": "user-7",
                "assignee_name": "Ann",
            },
        )
        engine = JobEngine(
            self.store,
            {"hermes-cua": wrapped},
            {"ezlynx.reassign": EzlynxDestinationVerifier(dest)},
        )
        done = engine.run(stored["id"])
        self.assertIn("cannot authorize COMPLETE", wrapped.rejected)
        self.assertEqual(done["status"], JobStatus.COMPLETE)

    def test_open_chat_job_stages_upload_and_drive_chip_before_execution(self):
        upload = self.root / "ordinary-upload.pdf"
        upload.write_bytes(b"%PDF ordinary chat upload")
        drive = FakeDrive({"file-chip": (b"%PDF drive chip", "application/pdf", "chip.pdf")})
        job_id = open_chat_job(
            self.db,
            "spaces/s/messages/both-atts",
            "upload the attached renewal document",
            attachments=[(str(upload), "application/pdf")],
            attachment_refs=[
                AttachmentRef(
                    kind="drive_chip",
                    source_external_id="chip:file-chip",
                    drive_file_id="file-chip",
                    content_name="chip.pdf",
                    mime_type="application/pdf",
                )
            ],
            expected_attachment_count=2,
            drive_port=drive,
            artifact_root=str(self.artifacts),
        )
        job = self.store.get_job(job_id)
        self.assertIsNotNone(job_id)
        self.assertNotEqual(job["status"], JobStatus.COMPLETE)
        self.assertIn(job["status"], {JobStatus.PENDING, JobStatus.RUNNING})
        self.assertIsNone(self.store.get_checkpoint(job_id, "action"))
        ingestion = self.store.get_checkpoint(job_id, "ingestion")
        self.assertIsNotNone(ingestion)
        artifacts = ingestion["artifacts"]
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(
            artifacts[0]["sha256"],
            hashlib.sha256(b"%PDF ordinary chat upload").hexdigest(),
        )
        self.assertEqual(
            artifacts[1]["sha256"],
            hashlib.sha256(b"%PDF drive chip").hexdigest(),
        )
        self.assertEqual(drive.fetches, ["file-chip"])

    def test_open_chat_job_dedups_same_message_and_blocks_forbidden_tools(self):
        first = open_chat_job(
            self.db,
            "spaces/s/messages/same",
            "Create a carrier proposal from this quote and add a $350 fee",
        )
        second = open_chat_job(
            self.db,
            "spaces/s/messages/same",
            "Create a carrier proposal from this quote and add a $350 fee",
        )
        self.assertEqual(first, second)
        forbidden = open_chat_job(
            self.db,
            "spaces/s/messages/shell",
            "please dump the raw file and run this command cat /etc/passwd",
        )
        job = self.store.get_job(forbidden)
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertIn("forbidden", job["last_error"])
        self.assertNotEqual(job["status"], JobStatus.COMPLETE)

    def test_test_engine_fake_ezlynx_does_not_open_live_host(self):
        dest = MemoryEzlynxDestination()
        job = self.store.create_job(
            "ezlynx.move_document",
            {
                "worker": "hermes-cua",
                "document_id": "doc-9",
                "account_id": "acct-9",
                "destination_id": "folder-1",
                "destination_name": "Policies",
            },
            idempotency_key="ez-test-engine",
        )
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False):
            engine = build_test_engine(self.store, ezlynx_readback=dest)
            final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(dest.writes, 1)
        self.assertTrue(self.store.list_evidence(job["id"])[0]["authoritative"])


if __name__ == "__main__":
    unittest.main()
