from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from robie_job_engine.attachments import AttachmentRef, ingest_attachment_refs, refs_from_chat_payload
from robie_job_engine.browser_read import BoundedBrowserReadWorker, BrowserReadVerifier
from robie_job_engine.carrier_proposal import (
    BoundedCarrierProposalWorker,
    CarrierProposalVerifier,
    MemoryProposalDestination,
)
from robie_job_engine.chat_guard import guard_chat_response, open_chat_job
from robie_job_engine.engine import JobEngine
from robie_job_engine.ezlynx import HermesCuaEzlynxWorker
from robie_job_engine.models import VERIFIER_AUTHORITY, JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from robie_job_engine.request_routing import classify_request
from robie_job_engine.store import JobStore


class RecordingWorker:
    def __init__(self, result, store=None):
        self.result = result
        self.store = store
        self.calls = 0
        self.keys = []

    def perform(self, job, *, idempotency_key):
        self.calls += 1
        self.keys.append(idempotency_key)
        if self.store is not None:
            try:
                self.store.transition(job["id"], JobStatus.COMPLETE)
            except PermissionError as exc:
                self.rejected = str(exc)
        return self.result


class SequenceVerifier:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def verify(self, job, action):
        self.calls += 1
        return self.results.pop(0)


class MemoryBrowser:
    def __init__(self, pages=None, fail_times=0):
        self.pages = pages or {}
        self.fail_times = fail_times
        self.reads = 0

    def read_fresh(self, locator):
        self.reads += 1
        if self.reads <= self.fail_times:
            raise RuntimeError("browser destination unavailable")
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
        if drive_file_id not in self.files:
            raise FileNotFoundError(drive_file_id)
        data, mime, name = self.files[drive_file_id]
        return data, mime, name


def _evidence(verified=True, authoritative=True, expected=None, observed=None):
    payload = expected or {"ok": True}
    return VerificationEvidence(
        "TEST",
        "destination",
        payload,
        payload if verified else (observed or {}),
        authoritative,
        datetime.now(timezone.utc).isoformat(),
    )


class ReliabilityMvpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root / "jobs.db")
        self.artifacts = self.root / "artifacts"

    def tearDown(self):
        self.tmp.cleanup()

    def _engine(self, worker, verifiers, worker_name="hermes-cua"):
        return JobEngine(self.store, {worker_name: worker}, verifiers)

    def test_every_request_creates_a_durable_job_before_execution(self):
        created = []

        class Probe:
            def perform(self, job, *, idempotency_key):
                created.append(self.store.get_job(job["id"])["status"] if False else job["id"])
                self.seen_status = JobStore(self.store.path).get_job(job["id"])["status"]
                return WorkerResult(True, "hermes.plain_english", {"id": job["id"]})

            def __init__(self, store):
                self.store = store
                self.seen_status = None

        job = self.store.create_job(
            "hermes.plain_english",
            {"worker": "hermes-cua", "text": "Please check the renewal list"},
            idempotency_key="plain-1",
        )
        self.assertEqual(job["status"], JobStatus.PENDING)
        probe = Probe(self.store)
        engine = self._engine(probe, {"hermes.plain_english": SequenceVerifier([
            VerificationResult(True, _evidence(), False)
        ])})
        engine.run(job["id"])
        self.assertTrue(created)
        self.assertEqual(probe.seen_status, JobStatus.RUNNING)

    def test_worker_cannot_authorize_complete(self):
        job = self.store.create_job("carrier.proposal", {"worker": "rogue"}, idempotency_key="rogue-1")
        worker = RecordingWorker(
            WorkerResult(True, "carrier.proposal", {"proposal_id": "p1"}),
            store=self.store,
        )
        engine = JobEngine(
            self.store,
            {"rogue": worker},
            {"carrier.proposal": SequenceVerifier([VerificationResult(False, _evidence(False), False)])},
        )
        final = engine.run(job["id"])
        self.assertIn("cannot authorize COMPLETE", getattr(worker, "rejected", ""))
        self.assertNotEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)

    def test_store_rejects_complete_without_verifier_authority_or_evidence(self):
        job = self.store.create_job("browser.read", {"worker": "x"}, idempotency_key="no-auth")
        with self.assertRaises(PermissionError):
            self.store.transition(job["id"], JobStatus.COMPLETE)
        with self.assertRaises(PermissionError):
            self.store.transition(
                job["id"], JobStatus.COMPLETE, authority=VERIFIER_AUTHORITY
            )
        self.assertEqual(self.store.get_job(job["id"])["status"], JobStatus.PENDING)

    def test_carrier_proposal_completes_only_after_fresh_fee_evidence(self):
        destination = MemoryProposalDestination()
        quote = "Acme quote\nPremium: $1200\n"
        quote_path = self.root / "jake-quote.pdf"
        quote_path.write_text(quote)
        job_id = open_chat_job(
            str(self.root / "jobs.db"),
            "spaces/s/messages/quote-1",
            "Create a carrier proposal from this quote and add a $350 fee",
            attachments=[(str(quote_path), "application/pdf")],
            expected_attachment_count=1,
            artifact_root=str(self.artifacts),
        )
        job = self.store.get_job(job_id)
        self.assertEqual(job["action_type"], "carrier.proposal")
        self.assertEqual(job["status"], JobStatus.PENDING)
        worker = BoundedCarrierProposalWorker(destination, self.store)
        verifier = CarrierProposalVerifier(destination)
        engine = JobEngine(self.store, {"carrier-proposal": worker}, {"carrier.proposal": verifier})
        final = engine.run(job_id)
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        evidence = self.store.list_evidence(job_id)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["observed"]["agency_fee_occurrences"], 1)
        self.assertEqual(evidence[0]["observed"]["page_count"], 10)
        self.assertTrue(evidence[0]["authoritative"])
        self.assertNotEqual(evidence[0]["evidence_sha256"], "")

    def test_duplicate_fee_or_missing_destination_never_completes(self):
        destination = MemoryProposalDestination()
        worker = BoundedCarrierProposalWorker(destination)
        verifier = CarrierProposalVerifier(destination)
        engine = JobEngine(self.store, {"carrier-proposal": worker}, {"carrier.proposal": verifier})
        duplicate = self.store.create_job(
            "carrier.proposal",
            {
                "worker": "carrier-proposal",
                "quote_text": "Quote already billed Agency fee: $350.00\n",
                "text": "generate a proposal and add a $350 fee",
                "expected_page_count": 10,
            },
            idempotency_key="dup-fee",
            max_attempts=1,
        )
        final = engine.run(duplicate["id"])
        self.assertEqual(final["status"], JobStatus.FAILED)
        missing = self.store.create_job(
            "carrier.proposal",
            {
                "worker": "carrier-proposal",
                "quote_text": "Clean quote",
                "text": "generate a proposal and add a $350 fee",
                "proposal_id": "missing-dest",
                "expected_page_count": 10,
            },
            idempotency_key="missing-dest",
            max_attempts=1,
        )

        class WriteOnly(MemoryProposalDestination):
            def read_fresh(self, proposal_id):
                return None

        empty = WriteOnly()
        engine = JobEngine(
            self.store,
            {"carrier-proposal": BoundedCarrierProposalWorker(empty)},
            {"carrier.proposal": CarrierProposalVerifier(empty)},
        )
        waiting = engine.run(missing["id"])
        self.assertEqual(waiting["status"], JobStatus.WAITING)
        self.assertNotEqual(waiting["status"], JobStatus.COMPLETE)

    def test_forbidden_chat_tools_fail_and_never_complete(self):
        destination = MemoryProposalDestination()
        engine = JobEngine(
            self.store,
            {"carrier-proposal": BoundedCarrierProposalWorker(destination)},
            {"carrier.proposal": CarrierProposalVerifier(destination)},
        )
        job = self.store.create_job(
            "carrier.proposal",
            {
                "worker": "carrier-proposal",
                "quote_text": "quote",
                "text": "generate a proposal; run this command rm -rf /",
                "tools": ["terminal"],
            },
            idempotency_key="shell-1",
            max_attempts=1,
        )
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.FAILED)
        self.assertIn("forbidden", final["last_error"])

    def test_browser_read_and_browser_failure_do_not_false_complete(self):
        pages = {"https://ezlynx.example/account": {"url": "https://ezlynx.example/account", "title": "Acme"}}
        browser = MemoryBrowser(pages)
        worker = BoundedBrowserReadWorker(browser)
        verifier = BrowserReadVerifier(browser)
        engine = JobEngine(self.store, {"browser-read": worker}, {"browser.read": verifier})
        job = self.store.create_job(
            "browser.read",
            {
                "worker": "browser-read",
                "text": "browser-only read the page",
                "locator": {"url": "https://ezlynx.example/account"},
                "expected": {"url": "https://ezlynx.example/account", "title": "Acme"},
            },
            idempotency_key="read-ok",
        )
        self.assertEqual(engine.run(job["id"])["status"], JobStatus.COMPLETE)
        self.assertGreaterEqual(browser.reads, 2)

        failing = MemoryBrowser(pages, fail_times=99)
        engine = JobEngine(
            self.store,
            {"browser-read": BoundedBrowserReadWorker(failing)},
            {"browser.read": BrowserReadVerifier(failing)},
        )
        bad = self.store.create_job(
            "browser.read",
            {
                "worker": "browser-read",
                "locator": {"url": "https://ezlynx.example/account"},
                "expected": {"title": "Acme"},
            },
            idempotency_key="read-fail",
            max_attempts=1,
        )
        final = engine.run(bad["id"])
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)
        self.assertTrue(self.store.list_evidence(bad["id"]))

    def test_checkpoint_restart_does_not_repeat_action(self):
        worker = RecordingWorker(WorkerResult(True, "ezlynx.apply_label", {"resource_id": "doc-1"}))
        first_verify = VerificationResult(False, _evidence(False), True, error="not yet")
        later = VerificationResult(True, _evidence(True, expected={"resource_id": "doc-1"}), False)
        verifier = SequenceVerifier([first_verify, later])
        engine = self._engine(worker, {"ezlynx.apply_label": verifier})
        job = self.store.create_job(
            "ezlynx.apply_label",
            {"worker": "hermes-cua"},
            idempotency_key="restart-1",
            max_attempts=3,
        )
        waiting = engine.run(job["id"])
        self.assertEqual(waiting["status"], JobStatus.RETRY_WAIT)
        self.assertEqual(worker.calls, 1)
        woke = self.store.wake_due("2099-01-01T00:00:00+00:00")
        self.assertEqual(woke, [job["id"]])
        final = engine.run(job["id"])
        self.assertEqual(worker.calls, 1)
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        self.assertEqual(len(self.store.list_evidence(job["id"])), 2)

    def test_dedup_idempotency_and_bounded_backoff(self):
        worker = RecordingWorker(WorkerResult(False, "ezlynx.reassign", {}, retryable=True, error="busy"))
        engine = self._engine(worker, {"ezlynx.reassign": SequenceVerifier([])})
        first = self.store.create_job(
            "ezlynx.reassign", {"worker": "hermes-cua"}, idempotency_key="gchat:same", max_attempts=3
        )
        second = self.store.create_job(
            "ezlynx.reassign", {"worker": "hermes-cua"}, idempotency_key="gchat:same", max_attempts=3
        )
        self.assertEqual(first["id"], second["id"])
        waiting = engine.run(first["id"])
        self.assertEqual(waiting["status"], JobStatus.RETRY_WAIT)
        self.assertIsNotNone(waiting["next_wakeup_at"])
        third = engine.run(first["id"])
        self.assertEqual(third["status"], JobStatus.RETRY_WAIT)
        self.assertEqual(worker.calls, 1)

    def test_chat_upload_and_drive_chip_ingestion(self):
        upload = self.root / "ordinary.pdf"
        upload.write_bytes(b"%PDF ordinary chat upload")
        drive = FakeDrive({"file-123": (b"%PDF drive chip", "application/pdf", "chip.pdf")})
        payload = [
            {
                "name": "spaces/s/messages/m/attachments/a1",
                "contentType": "application/pdf",
                "contentName": "ordinary.pdf",
                "attachmentDataRef": {"resourceName": "spaces/s/messages/m/attachments/a1"},
            },
            {
                "name": "spaces/s/messages/m/attachments/a2",
                "contentType": "application/pdf",
                "contentName": "chip.pdf",
                "driveDataRef": {"driveFileId": "file-123"},
                "source": "DRIVE_FILE",
            },
        ]
        refs = refs_from_chat_payload(payload)
        self.assertEqual([ref.kind for ref in refs], ["chat_upload", "drive_chip"])
        refs[0] = AttachmentRef(
            kind="chat_upload",
            source_external_id=refs[0].source_external_id,
            local_path=str(upload),
            mime_type="application/pdf",
        )
        job = self.store.create_job("hermes.google_chat_task", {"worker": "hermes-cua"}, idempotency_key="atts")
        records = ingest_attachment_refs(
            str(self.root / "jobs.db"),
            job["id"],
            "spaces/s/messages/m",
            refs,
            drive_port=drive,
            artifact_root=str(self.artifacts),
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["sha256"], hashlib.sha256(b"%PDF ordinary chat upload").hexdigest())
        self.assertEqual(records[1]["sha256"], hashlib.sha256(b"%PDF drive chip").hexdigest())
        self.assertEqual(records[1]["source_platform"], "google_drive")
        self.assertEqual(drive.fetches, ["file-123"])

    def test_missing_drive_chip_fails_closed(self):
        job_id = open_chat_job(
            str(self.root / "jobs.db"),
            "spaces/s/messages/drive-miss",
            "upload the attached renewal document",
            attachments=[],
            expected_attachment_count=1,
            attachment_refs=[
                AttachmentRef(
                    kind="drive_chip",
                    source_external_id="chip:missing",
                    drive_file_id="missing",
                )
            ],
            artifact_root=str(self.artifacts),
        )
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertIn("staged 0", job["last_error"])

    def test_uncertain_outcomes_never_silently_succeed(self):
        vague = open_chat_job(
            str(self.root / "jobs.db"),
            "spaces/s/messages/vague",
            "do it",
        )
        self.assertEqual(self.store.get_job(vague)["status"], JobStatus.NEEDS_CLARIFICATION)
        self.assertIn("NEEDS_CLARIFICATION", guard_chat_response(str(self.root / "jobs.db"), vague, "Done"))

        worker = RecordingWorker(WorkerResult(True, "ezlynx.apply_label", {"resource_id": "x"}))
        engine = self._engine(
            worker,
            {"ezlynx.apply_label": SequenceVerifier([
                VerificationResult(True, _evidence(True, authoritative=False), False)
            ])},
        )
        job = self.store.create_job("ezlynx.apply_label", {"worker": "hermes-cua"}, idempotency_key="soft")
        final = engine.run(job["id"])
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)

        missing = self.store.create_job("unknown.action", {"worker": "hermes-cua"}, idempotency_key="no-ver")
        engine = JobEngine(
            self.store,
            {"hermes-cua": RecordingWorker(WorkerResult(True, "unknown.action", {"id": "1"}))},
            {},
        )
        self.assertEqual(engine.run(missing["id"])["status"], JobStatus.UNVERIFIED)

    def test_ezlynx_bounded_task_still_requires_verifier(self):
        class Browser:
            def exact_option(self, *, stable_id, exact_text, scope=None):
                return stable_id

            def click(self, target):
                return None

            def submit(self, *, idempotency_key):
                return {"request_id": idempotency_key}

        worker = HermesCuaEzlynxWorker(Browser())
        job = {
            "action_type": "ezlynx.reassign",
            "payload": {
                "resource_id": "task-1",
                "assignee_id": "user-7",
                "assignee_name": "Ann",
            },
        }
        receipt = worker.perform(job, idempotency_key="ez-1")
        self.assertTrue(receipt.succeeded)
        engine = JobEngine(
            self.store,
            {"hermes-cua": RecordingWorker(receipt)},
            {"ezlynx.reassign": SequenceVerifier([
                VerificationResult(False, _evidence(False), False, error="destination mismatch")
            ])},
        )
        stored = self.store.create_job("ezlynx.reassign", {"worker": "hermes-cua"}, idempotency_key="ez-job")
        final = engine.run(stored["id"])
        self.assertEqual(final["status"], JobStatus.UNVERIFIED)

    def test_classify_testing_week_workflows(self):
        self.assertEqual(
            classify_request("Please remind me which renewals are due this week").action_type,
            "hermes.plain_english",
        )
        self.assertEqual(
            classify_request("browser-only read the EZLynx account page").action_type,
            "browser.read",
        )
        self.assertEqual(
            classify_request("Create a carrier proposal from Jake's quote PDF and add a $350 fee").action_type,
            "carrier.proposal",
        )
        self.assertEqual(
            classify_request("EZLynx reassign this account to Ann").action_type,
            "ezlynx.reassign",
        )

    def test_failure_mode_matrix_has_no_false_complete(self):
        cases = []
        destination = MemoryProposalDestination()
        cases.append(self._run_case(
            "attachment-missing",
            lambda: open_chat_job(
                str(self.root / "jobs.db"),
                "spaces/s/messages/fail-att",
                "Create a carrier proposal from this quote and add a $350 fee",
                attachments=[],
                expected_attachment_count=1,
                artifact_root=str(self.artifacts),
            ),
        ))
        browser = MemoryBrowser({}, fail_times=99)
        job = self.store.create_job(
            "browser.read",
            {"worker": "browser-read", "locator": {"url": "https://x"}, "expected": {"title": "X"}},
            idempotency_key="fail-browser",
            max_attempts=1,
        )
        engine = JobEngine(
            self.store,
            {"browser-read": BoundedBrowserReadWorker(browser)},
            {"browser.read": BrowserReadVerifier(browser)},
        )
        cases.append(engine.run(job["id"]))
        job = self.store.create_job(
            "carrier.proposal",
            {"worker": "carrier-proposal", "quote_text": "q", "text": "generate a proposal"},
            idempotency_key="fail-verify",
            max_attempts=1,
        )
        broken = MemoryProposalDestination()
        worker = BoundedCarrierProposalWorker(destination)
        engine = JobEngine(
            self.store,
            {"carrier-proposal": worker},
            {"carrier.proposal": CarrierProposalVerifier(broken)},
        )
        cases.append(engine.run(job["id"]))
        statuses = {item["status"] for item in cases}
        self.assertNotIn(JobStatus.COMPLETE, statuses)
        self.assertTrue(statuses <= {
            JobStatus.FAILED, JobStatus.UNVERIFIED, JobStatus.WAITING,
            JobStatus.NEEDS_CLARIFICATION, JobStatus.RETRY_WAIT, JobStatus.PAUSED,
        })

    def _run_case(self, _name, opener):
        job_id = opener()
        return self.store.get_job(job_id)


if __name__ == "__main__":
    unittest.main()
