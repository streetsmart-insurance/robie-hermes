from __future__ import annotations

import signal
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from robie_job_engine.confidence import assess_job_confidence
from robie_job_engine.browser_capture import SENSITIVE_CAPTURE_SELECTOR
from robie_job_engine.recording import (
    RecordingManager,
    RecordingRequiredError,
    RecordingStore,
    SubprocessTabCapture,
    recording_health,
)
from robie_job_engine.store import JobStore


class FakeCapture:
    def start(self, output_path: Path, stop_file: Path) -> int:
        self.output_path = output_path
        return 4242

    def stop(self, pid: int, stop_file: Path, output_path: Path) -> None:
        stop_file.touch()
        output_path.write_bytes(b"fake-webm-for-acceptance-test")


class DistinctSegmentCapture:
    def __init__(self) -> None:
        self.stops = 0

    def start(self, output_path: Path, stop_file: Path) -> int:
        return 4300 + self.stops

    def stop(self, pid: int, stop_file: Path, output_path: Path) -> None:
        self.stops += 1
        stop_file.touch()
        output_path.write_bytes(f"segment-bytes-{self.stops}\n".encode())


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

    def test_subprocess_capture_waits_for_first_frame_readiness(self) -> None:
        output = self.root / "ready.webm"
        stop_file = self.root / "ready.stop"
        process = Mock(pid=31337, returncode=None)
        process.poll.side_effect = lambda: None

        def launch(command, **_kwargs):
            ready_path = Path(command[command.index("--ready-file") + 1])
            ready_path.touch(mode=0o600)
            return process

        capture = SubprocessTabCapture(ready_timeout=0.2)
        with patch("robie_job_engine.recording.subprocess.Popen", side_effect=launch):
            pid = capture.start(output, stop_file)
        self.assertEqual(31337, pid)
        self.assertTrue(output.with_suffix(".ready").is_file())

    def test_required_recording_fails_before_work_when_capture_never_ready(self) -> None:
        process = Mock(pid=31338, returncode=None)
        process.poll.return_value = None
        capture = SubprocessTabCapture(ready_timeout=0.01)
        manager = RecordingManager(
            self.db,
            root=self.root / "recordings",
            capture=capture,
            uploader=FakeUploader(),
            enabled=True,
        )
        with patch("robie_job_engine.recording.subprocess.Popen", return_value=process), \
             patch("robie_job_engine.recording.os.killpg") as killpg:
            with self.assertRaisesRegex(RecordingRequiredError, "did not become ready"):
                manager.start_required(self.job["id"])
        killpg.assert_called_once_with(31338, signal.SIGTERM)
        latest = manager.store.latest(self.job["id"])
        self.assertEqual("FAILED", latest["status"])
        self.assertEqual("START", latest["failure_stage"])

    def test_sensitive_fields_are_in_capture_mask_policy(self) -> None:
        for marker in ("password", "one-time-code", "cc-", "mfa", "otp", "card", "cvv", "ssn"):
            self.assertIn(marker, SENSITIVE_CAPTURE_SELECTOR)

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

    def test_same_second_segments_keep_distinct_paths_and_contents(self) -> None:
        frozen = datetime(2026, 8, 24, 23, 50, 0, tzinfo=timezone.utc)
        capture = DistinctSegmentCapture()
        manager = RecordingManager(
            self.db,
            root=self.root / "recordings",
            capture=capture,
            uploader=FakeUploader(),
            enabled=True,
            keep_local=True,
        )
        with patch("robie_job_engine.recording.datetime") as mocked:
            mocked.now.return_value = frozen
            manager.start(self.job["id"])
            manager.stop_and_upload(self.job["id"], "RETRY_WAIT")
            manager.start(self.job["id"])
            manager.stop_and_upload(self.job["id"], "COMPLETE")
        segments = manager.list_for_job(self.job["id"])
        self.assertEqual(len(segments), 2)
        paths = [item["local_path"] for item in segments]
        self.assertEqual(len(set(paths)), 2)
        contents = [Path(path).read_bytes() for path in paths]
        self.assertEqual(len(set(contents)), 2)
        self.assertTrue(all(Path(path).is_file() for path in paths))
        self.assertTrue(all("-" in Path(path).name for path in paths))

    def test_health_reports_missing_required_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             patch("robie_job_engine.recording.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("robie_job_engine.recording.importlib.util.find_spec", return_value=object()):
            report = recording_health(
                environ={"ROBIE_RECORDING_ROOT": tmp},
                check_cdp=False,
            )
        self.assertFalse(report["ready"])
        self.assertIn("ROBIE_RECORD_ALL_JOBS is not enabled", report["issues"])
        self.assertIn(
            "ROBIE_RECORDINGS_DRIVE_FOLDER_ID is not configured",
            report["issues"],
        )

    def test_health_can_be_ready_without_exposing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             patch("robie_job_engine.recording.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("robie_job_engine.recording.importlib.util.find_spec", return_value=object()):
            token = Path(tmp) / "workspace-token.json"
            token.write_text("secret")
            report = recording_health(
                environ={
                    "ROBIE_RECORD_ALL_JOBS": "1",
                    "ROBIE_RECORDING_ROOT": tmp,
                    "ROBIE_RECORDINGS_DRIVE_FOLDER_ID": "private-folder-id",
                    "ROBIE_GOOGLE_TOKEN_FILE": str(token),
                    "ROBIE_BROWSER_CDP_URL": "http://user:password@127.0.0.1:9222",
                },
                check_cdp=False,
            )
            token_path = str(token)
        self.assertTrue(report["ready"])
        self.assertNotIn("private-folder-id", str(report))
        self.assertNotIn(token_path, str(report))
        self.assertNotIn("password", str(report))

    def test_confidence_surfaces_recording_failure_reason(self) -> None:
        result = assess_job_confidence({
            "status": "UNVERIFIED",
            "recording_status": "FAILED",
            "recording_failure": (
                "RuntimeError: browser capture produced no video\naccess_token=do-not-show"
            ),
        })
        self.assertIn(
            "Diagnostic recording failed: RuntimeError: browser capture produced no video access_token=[REDACTED]",
            result.issues,
        )
        self.assertNotIn("do-not-show", str(result.issues))


if __name__ == "__main__":
    unittest.main()
