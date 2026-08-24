from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from robie_job_engine.confidence import assess_job_confidence
from robie_job_engine.recording import RecordingManager, RecordingStore
from robie_job_engine.store import JobStore


class FakeCapture:
    def start(self, output_path: Path, stop_file: Path) -> int:
        self.output_path = output_path
        return 4242

    def stop(self, pid: int, stop_file: Path, output_path: Path) -> None:
        stop_file.touch()
        output_path.write_bytes(b"fake-webm-for-acceptance-test")


class FakeUploader:
    def upload(self, path: Path, file_name: str) -> tuple[str, str]:
        self.file_name = file_name
        return "drive-file-123", "https://drive.google.com/file/d/drive-file-123/view"


class RecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "jobs.db"
        self.jobs = JobStore(self.db)
        self.job = self.jobs.create_job("ezlynx.move_document", {"task": "Move test file"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_recording_is_uploaded_and_linked_to_job(self) -> None:
        manager = RecordingManager(
            self.db, root=self.root / "recordings", capture=FakeCapture(),
            uploader=FakeUploader(), enabled=True,
        )
        manager.start(self.job["id"])
        result = manager.stop_and_upload(self.job["id"], "UNVERIFIED")
        self.assertEqual("READY", result["status"])
        self.assertEqual("drive-file-123", result["drive_file_id"])
        self.assertTrue(result["sha256"])
        self.assertGreater(result["size_bytes"], 0)
        segments = manager.list_for_job(self.job["id"])
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["segment_number"], 1)
        self.assertEqual(segments[0]["drive_file_id"], "drive-file-123")

    def test_video_cannot_enter_reference_or_training_set_without_review(self) -> None:
        store = RecordingStore(self.db)
        recording = store.create(self.job["id"], self.root / "a.webm", self.root / "a.stop")
        with self.assertRaises(ValueError):
            store.approve_reference(recording["id"], approved_by="Carlo", notes="reviewed", redacted=False)
        with self.assertRaises(ValueError):
            store.approve_training(recording["id"], approved_by="Carlo")
        store.approve_reference(recording["id"], approved_by="Carlo", notes="PII removed", redacted=True)
        approved = store.approve_training(recording["id"], approved_by="Carlo")
        self.assertEqual(1, approved["training_approved"])

    def test_confidence_is_evidence_based(self) -> None:
        complete = assess_job_confidence({
            "status": "COMPLETE", "verified_evidence_count": 1,
            "authoritative_evidence_count": 1, "recording_status": "READY",
        })
        unverified = assess_job_confidence({
            "status": "UNVERIFIED", "verified_evidence_count": 0,
            "authoritative_evidence_count": 0, "recording_status": "READY",
        })
        self.assertEqual((98, "HIGH"), (complete.score, complete.level))
        self.assertEqual("LOW", unverified.level)
        self.assertIn("Destination state was not independently verified", unverified.issues)


if __name__ == "__main__":
    unittest.main()
