from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace

from durable_temp import durable_temporary_directory

from robie_job_engine.attachments import (
    AttachmentRef,
    CallableDrivePort,
    drive_chip_download_failed_message,
    ingest_attachment_refs,
    is_attachment_ingestion_error,
    proven_drive_share_identity,
    refs_from_chat_payload,
    unmatched_drive_chip_refs,
)
from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


ROOT = Path(__file__).resolve().parents[1]


class FakeDrive:
    def __init__(self, files=None, *, error=None):
        self.files = files or {}
        self.error = error
        self.fetches = []

    def fetch(self, drive_file_id):
        self.fetches.append(drive_file_id)
        if self.error is not None:
            raise self.error
        if drive_file_id not in self.files:
            raise FileNotFoundError(drive_file_id)
        return self.files[drive_file_id]


class DriveChipIngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "jobs.db")
        self.store = JobStore(self.db)
        self.artifacts = self.root / "artifacts"

    def tearDown(self):
        self.tmp.cleanup()

    def test_drive_chip_successful_fetch_stages_one(self):
        drive = FakeDrive({
            "file-carlo": (b"%PDF drive picker", "application/pdf", "carlo.pdf"),
        })
        job_id = open_chat_job(
            self.db,
            "spaces/s/messages/drive-ok",
            "upload the attached renewal document",
            attachment_refs=[
                AttachmentRef(
                    kind="drive_chip",
                    source_external_id="chip:file-carlo",
                    drive_file_id="file-carlo",
                    content_name="carlo.pdf",
                    mime_type="application/pdf",
                )
            ],
            expected_attachment_count=1,
            drive_port=drive,
            artifact_root=str(self.artifacts),
        )
        job = self.store.get_job(job_id)
        self.assertIn(job["status"], {JobStatus.PENDING, JobStatus.RUNNING})
        self.assertNotEqual(job["status"], JobStatus.COMPLETE)
        ingestion = self.store.get_checkpoint(job_id, "ingestion")
        self.assertIsNotNone(ingestion)
        artifacts = ingestion["artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(
            artifacts[0]["sha256"],
            hashlib.sha256(b"%PDF drive picker").hexdigest(),
        )
        self.assertEqual(drive.fetches, ["file-carlo"])

    def test_drive_chip_failed_fetch_fails_closed(self):
        drive = FakeDrive(error=PermissionError("app identity cannot read file"))
        job_id = open_chat_job(
            self.db,
            "spaces/s/messages/drive-denied",
            "upload the attached renewal document",
            attachment_refs=[
                AttachmentRef(
                    kind="drive_chip",
                    source_external_id="chip:denied",
                    drive_file_id="denied",
                )
            ],
            expected_attachment_count=1,
            drive_port=drive,
            artifact_root=str(self.artifacts),
        )
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertNotEqual(job["status"], JobStatus.COMPLETE)
        self.assertTrue(is_attachment_ingestion_error(job["last_error"]))
        self.assertIn("attachment ingestion failed", job["last_error"])
        self.assertIsNone(self.store.get_checkpoint(job_id, "ingestion"))
        self.assertEqual(drive.fetches, ["denied"])

    def test_drive_chip_without_port_fails_closed_received_staged_mismatch(self):
        job_id = open_chat_job(
            self.db,
            "spaces/s/messages/drive-no-port",
            "upload the attached renewal document",
            attachment_refs=[
                AttachmentRef(
                    kind="drive_chip",
                    source_external_id="chip:missing",
                    drive_file_id="missing",
                )
            ],
            expected_attachment_count=1,
            artifact_root=str(self.artifacts),
        )
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.FAILED)
        self.assertIn("received 1", job["last_error"])
        self.assertIn("staged 0", job["last_error"])
        self.assertTrue(is_attachment_ingestion_error(job["last_error"]))

    def test_paperclip_media_download_path_still_stages(self):
        upload = self.root / "paperclip.pdf"
        upload.write_bytes(b"%PDF paperclip upload")
        job_id = open_chat_job(
            self.db,
            "spaces/s/messages/paperclip",
            "upload the attached renewal document",
            attachments=[(str(upload), "application/pdf")],
            expected_attachment_count=1,
            artifact_root=str(self.artifacts),
        )
        job = self.store.get_job(job_id)
        self.assertIn(job["status"], {JobStatus.PENDING, JobStatus.RUNNING})
        self.assertNotEqual(job["status"], JobStatus.COMPLETE)
        ingestion = self.store.get_checkpoint(job_id, "ingestion")
        self.assertEqual(len(ingestion["artifacts"]), 1)
        self.assertEqual(
            ingestion["artifacts"][0]["sha256"],
            hashlib.sha256(b"%PDF paperclip upload").hexdigest(),
        )

    def test_chat_upload_ref_with_local_path_stages_like_paperclip(self):
        upload = self.root / "media-download.pdf"
        upload.write_bytes(b"%PDF chat media.download")
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua"},
            idempotency_key="media-ref",
        )
        records = ingest_attachment_refs(
            self.db,
            job["id"],
            "spaces/s/messages/m",
            [
                AttachmentRef(
                    kind="chat_upload",
                    source_external_id="spaces/s/messages/m/attachments/a1:media",
                    local_path=str(upload),
                    mime_type="application/pdf",
                    content_name="media-download.pdf",
                )
            ],
            artifact_root=str(self.artifacts),
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source_platform"], "google_chat")
        self.assertEqual(
            records[0]["sha256"],
            hashlib.sha256(b"%PDF chat media.download").hexdigest(),
        )

    def test_drive_chip_with_local_path_stages_without_second_fetch(self):
        cached = self.root / "already-downloaded.pdf"
        cached.write_bytes(b"%PDF adapter already fetched")
        drive = FakeDrive()
        job = self.store.create_job(
            "hermes.google_chat_task",
            {"worker": "hermes-cua"},
            idempotency_key="local-drive",
        )
        records = ingest_attachment_refs(
            self.db,
            job["id"],
            "spaces/s/messages/m",
            [
                AttachmentRef(
                    kind="drive_chip",
                    source_external_id="chip:local",
                    drive_file_id="file-local",
                    local_path=str(cached),
                    mime_type="application/pdf",
                    content_name="already-downloaded.pdf",
                )
            ],
            drive_port=drive,
            artifact_root=str(self.artifacts),
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source_platform"], "google_drive")
        self.assertEqual(drive.fetches, [])

    def test_unmatched_drive_chip_refs_skip_when_local_download_covered(self):
        payload = [
            {
                "name": "spaces/s/messages/m/attachments/a1",
                "driveDataRef": {"driveFileId": "file-1"},
                "source": "DRIVE_FILE",
            }
        ]
        self.assertEqual(unmatched_drive_chip_refs(payload, staged_count=0)[0].drive_file_id, "file-1")
        self.assertEqual(unmatched_drive_chip_refs(payload, staged_count=1), [])
        paperclip = [
            {
                "name": "spaces/s/messages/m/attachments/a2",
                "attachmentDataRef": {"resourceName": "spaces/s/messages/m/attachments/a2"},
            }
        ]
        self.assertEqual(unmatched_drive_chip_refs(paperclip, staged_count=0), [])

    def test_drive_chip_download_failed_message_is_honest(self):
        plain = drive_chip_download_failed_message()
        self.assertIn("Drive file did not download", plain)
        self.assertIn("Paperclip the PDF", plain)
        self.assertNotIn("@robie", plain.casefold())
        self.assertNotIn("@", plain)
        self.assertNotIn("robie@streetsmart.insurance", plain)
        shared = drive_chip_download_failed_message(
            "chat-bot@streetsmart-hermes-poc.iam.gserviceaccount.com"
        )
        self.assertIn("share the file with chat-bot@streetsmart-hermes-poc.iam.gserviceaccount.com", shared)
        self.assertNotIn("@robie", shared.casefold())

    def test_proven_drive_share_identity_never_invents(self):
        self.assertIsNone(proven_drive_share_identity(None))
        self.assertIsNone(proven_drive_share_identity(SimpleNamespace()))
        self.assertIsNone(
            proven_drive_share_identity(SimpleNamespace(service_account_email="robie@streetsmart.insurance"))
        )
        self.assertEqual(
            proven_drive_share_identity(
                SimpleNamespace(
                    service_account_email="bot@streetsmart-hermes-poc.iam.gserviceaccount.com"
                )
            ),
            "bot@streetsmart-hermes-poc.iam.gserviceaccount.com",
        )

    def test_callable_drive_port_rejects_empty_fetch(self):
        port = CallableDrivePort(lambda _id: None)
        with self.assertRaises(RuntimeError):
            port.fetch("file-x")

    def test_adapter_wires_drive_port_and_halts_before_execution(self):
        adapter = (ROOT / "integrations/google_chat/adapter.py").read_text(encoding="utf-8")
        self.assertIn("unmatched_drive_chip_refs", adapter)
        self.assertIn("drive_port=attachment_kwargs[\"drive_port\"]", adapter)
        self.assertIn("attachment_refs=attachment_kwargs[\"attachment_refs\"]", adapter)
        self.assertIn("_halt_failed_drive_ingestion", adapter)
        self.assertIn("drive_chip_download_failed_message", adapter)
        self.assertIn("https://www.googleapis.com/auth/drive.readonly", adapter)
        self.assertIn("_fetch_drive_file_sync", adapter)
        self.assertIn("_fetch_drive_bytes", adapter)
        open_at = adapter.index(
            "job_id = await asyncio.to_thread(\n                open_chat_job"
        )
        halt_at = adapter.index("_halt_failed_drive_ingestion", open_at)
        enqueue_at = adapter.index(
            "if job_id and await self._enqueue_bounded_chat_job(", halt_at
        )
        hermes_at = adapter.index("await self._run_generic_chat_job(job_id, event)", halt_at)
        self.assertLess(open_at, halt_at)
        self.assertLess(halt_at, enqueue_at)
        self.assertLess(halt_at, hermes_at)
        self.assertIn("reply_to=None", adapter[adapter.index("async def _halt_failed_drive_ingestion"):])

    def test_destination_verify_and_complete_guard_unchanged(self):
        complete = (ROOT / "robie_job_engine/complete_guard.py").read_text(encoding="utf-8")
        self.assertIn("cannot authorize COMPLETE", complete)
        chat_guard = (ROOT / "robie_job_engine/chat_guard.py").read_text(encoding="utf-8")
        self.assertIn("attachment ingestion incomplete: received", chat_guard)
        self.assertIn("attachment ingestion failed:", chat_guard)

    def test_refs_from_chat_payload_still_splits_paperclip_and_drive(self):
        refs = refs_from_chat_payload(
            [
                {
                    "name": "spaces/s/messages/m/attachments/a1",
                    "attachmentDataRef": {"resourceName": "spaces/s/messages/m/attachments/a1"},
                },
                {
                    "name": "spaces/s/messages/m/attachments/a2",
                    "driveDataRef": {"driveFileId": "file-123"},
                    "source": "DRIVE_FILE",
                },
            ]
        )
        self.assertEqual([ref.kind for ref in refs], ["chat_upload", "drive_chip"])


if __name__ == "__main__":
    unittest.main()
