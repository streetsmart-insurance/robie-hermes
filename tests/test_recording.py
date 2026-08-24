import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from robie_job_engine.confidence import assess_job_confidence
from robie_job_engine.recording import recording_health


class RecordingTests(unittest.TestCase):
    def test_health_reports_missing_required_configuration(self):
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

    def test_health_can_be_ready_without_exposing_secrets(self):
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

    def test_confidence_surfaces_recording_failure_reason(self):
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
