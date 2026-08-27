from __future__ import annotations

import shutil
import sqlite3
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import (
    guard_chat_response,
    open_chat_job,
    stop_generic_chat_job_heartbeat,
)
from robie_job_engine.models import JobStatus, VerificationEvidence
from robie_job_engine.post_job_audit import (
    analyze_recording_motion,
    audit_terminal_job,
    format_audit_chat_message,
    frames_show_motion,
    rgb_frame,
    run_post_job_audit,
    tool_calls_claim_mutation,
)
from robie_job_engine.recording import RecordingStore
from robie_job_engine.store import JobStore


def _ready_recording(db: str, job_id: str, path: Path) -> None:
    store = RecordingStore(db)
    recording = store.create(job_id, path, path.with_suffix(".stop"))
    store.update(
        recording["id"],
        status="READY",
        drive_url="https://drive.google.com/file/d/audit-test/view",
        drive_file_id="audit-test",
    )


def _extract(frames: list[bytes]):
    def _inner(_path):
        return frames

    return _inner


class FrameDiffTests(unittest.TestCase):
    def test_identical_frames_are_frozen(self):
        red = rgb_frame(8, 6, (200, 10, 10))
        result = frames_show_motion([red, red, red, red])
        self.assertEqual(result["result"], "FAIL")
        self.assertIn("frozen", result["reason"])
        single = frames_show_motion([red])
        self.assertEqual(single["result"], "FAIL")
        self.assertIn("frozen", single["reason"])

    def test_changing_frames_show_motion(self):
        red = rgb_frame(8, 6, (200, 10, 10))
        blue = rgb_frame(8, 6, (10, 10, 200))
        result = frames_show_motion([red, blue, red, blue])
        self.assertEqual(result["result"], "PASS")
        self.assertGreater(result["max_mean_abs"], 2.5)

    def test_missing_recording_is_fail_not_pass(self):
        with durable_temporary_directory() as tmp:
            missing = Path(tmp) / "gone.webm"
            result = analyze_recording_motion(missing)
            self.assertEqual(result["result"], "FAIL")
            self.assertIn("missing recording", result["reason"])


class PostJobAuditTests(unittest.TestCase):
    def test_heartbeat_absent_is_no(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "message-no-hb",
                "Perform the destination workflow",
                conversation_id="spaces/no-hb",
            )
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "DELETE FROM checkpoints WHERE job_id=? AND kind='gateway_progress'",
                    (job_id,),
                )
            store = JobStore(db)
            store.transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error="stopped for audit",
                release_lease=True,
            )
            audit = run_post_job_audit(
                db, job_id, session_root=Path(tmp) / "sessions"
            )
            self.assertFalse(audit["heartbeat"]["present"])
            self.assertEqual(audit["heartbeat"]["result"], "FAIL")
            text = format_audit_chat_message(audit)
            self.assertIn("Heartbeat gateway_progress: no", text)

    def test_heartbeat_present_reports_first_and_last(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = open_chat_job(
                db,
                "message-hb",
                "Perform the destination workflow",
                conversation_id="spaces/hb",
            )
            stop_generic_chat_job_heartbeat(db, job_id)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "DELETE FROM checkpoints WHERE job_id=? AND kind='gateway_progress'",
                    (job_id,),
                )
            first = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)
            last = first + timedelta(minutes=4)
            store.heartbeat_generic_chat_job(job_id, now=first, source="hermes-gateway")
            store.heartbeat_generic_chat_job(job_id, now=last, source="hermes-gateway")
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="audit fixture",
                release_lease=True,
            )
            audit = run_post_job_audit(
                db, job_id, session_root=Path(tmp) / "sessions"
            )
            self.assertTrue(audit["heartbeat"]["present"])
            self.assertEqual(audit["heartbeat"]["first_at"], first.isoformat())
            self.assertEqual(audit["heartbeat"]["last_at"], last.isoformat())
            text = format_audit_chat_message(audit)
            self.assertIn("first=", text)
            self.assertIn("last=", text)

    def test_worker_prose_is_not_destination_evidence(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = open_chat_job(db, "message-prose", "move it")
            store.checkpoint(
                job_id,
                "worker_response",
                {"response_text": "I uploaded the quote and the destination looks good."},
            )
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="no evidence",
                release_lease=True,
            )
            audit = run_post_job_audit(
                db, job_id, session_root=Path(tmp) / "sessions"
            )
            self.assertEqual(audit["destination_evidence"]["summary"], "ZERO")
            self.assertEqual(audit["destination_evidence"]["evidence_count"], 0)
            self.assertEqual(audit["destination_evidence"]["destination_action_count"], 0)
            self.assertIn("ZERO", format_audit_chat_message(audit))

    def test_real_evidence_rows_are_counted(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job = store.create_job("browser.read", {"locator": "#x"})
            store.checkpoint(
                job["id"],
                "action",
                {"action": "browser.read", "destination": {"locator": "#x"}},
            )
            store.add_evidence(
                job["id"],
                True,
                VerificationEvidence(
                    method="FRESH_READBACK",
                    source="destination",
                    expected={"locator": "#x"},
                    observed={"locator": "#x"},
                    authoritative=True,
                    captured_at=datetime.now(timezone.utc).isoformat(),
                    locator="#x",
                ),
            )
            store.transition(job["id"], JobStatus.UNVERIFIED, error="fixture")
            audit = run_post_job_audit(
                db, job["id"], session_root=Path(tmp) / "sessions"
            )
            self.assertEqual(audit["destination_evidence"]["destination_action_count"], 1)
            self.assertEqual(audit["destination_evidence"]["evidence_count"], 1)
            self.assertNotEqual(audit["destination_evidence"]["summary"], "ZERO")

    def test_frozen_recording_fails_motion_check(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-frozen", "move it")
            JobStore(db).transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
            video = Path(tmp) / "frozen.webm"
            video.write_bytes(b"not-a-real-webm")
            _ready_recording(db, job_id, video)
            frozen = [rgb_frame(8, 6, (30, 30, 30))] * 6
            audit = run_post_job_audit(
                db,
                job_id,
                session_root=Path(tmp) / "sessions",
                extract_frames=_extract(frozen),
            )
            self.assertEqual(audit["recording_motion"]["result"], "FAIL")
            self.assertIn("frozen", audit["recording_motion"]["reason"])

    def test_moving_recording_passes_motion_check(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-moving", "move it")
            JobStore(db).transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
            video = Path(tmp) / "moving.webm"
            video.write_bytes(b"not-a-real-webm")
            _ready_recording(db, job_id, video)
            frames = [
                rgb_frame(8, 6, (10, 10, 10)),
                rgb_frame(8, 6, (240, 10, 10)),
                rgb_frame(8, 6, (10, 240, 10)),
            ]
            audit = run_post_job_audit(
                db,
                job_id,
                session_root=Path(tmp) / "sessions",
                extract_frames=_extract(frames),
            )
            self.assertEqual(audit["recording_motion"]["result"], "PASS")

    def test_frozen_recording_with_playwright_claims_is_mismatch(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = open_chat_job(db, "message-mismatch", "move it")
            store.checkpoint(
                job_id,
                "worker_response",
                {
                    "response_text": (
                        "playwright_exec: page.goto('https://app.ezlynx.com/policies'); "
                        "page.fill('#NamedInsured', 'ROBIE Test LLC')"
                    )
                },
            )
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
            video = Path(tmp) / "static-policies.webm"
            video.write_bytes(b"static")
            _ready_recording(db, job_id, video)
            session = Path(tmp) / "sessions"
            session.mkdir()
            (session / "job.json").write_text(
                f'{{"job_id": "{job_id}", "tool": "playwright_exec", '
                f'"code": "page.goto(\\"https://app.ezlynx.com/policies\\")"}}',
                encoding="utf-8",
            )
            frozen = [rgb_frame(8, 6, (80, 80, 80))] * 5
            audit = run_post_job_audit(
                db,
                job_id,
                session_root=session,
                extract_frames=_extract(frozen),
            )
            self.assertEqual(audit["recording_motion"]["result"], "FAIL")
            self.assertIn("frozen", audit["recording_motion"]["reason"])
            self.assertEqual(audit["tool_vs_recording"]["result"], "MISMATCH")
            self.assertIn("MISMATCH", format_audit_chat_message(audit))
            self.assertFalse(audit["authorizes_complete"])
            self.assertNotEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)

    def test_check3_frozen_fails_even_when_tool_logs_are_busy(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = open_chat_job(db, "message-busy-frozen", "edit policy")
            store.checkpoint(
                job_id,
                "worker_response",
                {
                    "response_text": (
                        "37 unique playwright_exec calls; page.goto Edit; "
                        "page.click('Save and Continue')"
                    )
                },
            )
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
            video = Path(tmp) / "busy-frozen.webm"
            video.write_bytes(b"static")
            _ready_recording(db, job_id, video)
            session = Path(tmp) / "sessions"
            session.mkdir()
            (session / "tools.log").write_text(
                f"{job_id} playwright_exec page.goto FormEntry page.fill",
                encoding="utf-8",
            )
            frozen = [rgb_frame(8, 6, (40, 40, 40))] * 8
            audit = run_post_job_audit(
                db,
                job_id,
                session_root=session,
                extract_frames=_extract(frozen),
            )
            self.assertEqual(audit["recording_motion"]["result"], "FAIL")
            self.assertIn("frozen", audit["recording_motion"]["reason"])
            self.assertTrue(
                tool_calls_claim_mutation(
                    "37 unique playwright_exec calls; page.goto Edit; page.click"
                )
            )

    def test_missing_jobs_db_and_session_are_unknown_fail(self):
        missing_db = Path("/workspace/.robie-durable-test/does-not-exist-jobs.db")
        if missing_db.exists():
            missing_db.unlink()
        audit = run_post_job_audit(missing_db, "job-missing")
        self.assertEqual(audit["verdict"], "FAIL")
        self.assertEqual(audit["heartbeat"]["result"], "UNKNOWN")
        self.assertEqual(audit["recording_motion"]["result"], "FAIL")
        self.assertFalse(audit["authorizes_complete"])

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-session", "move it")
            JobStore(db).transition(
                job_id,
                JobStatus.FAILED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
            audit = run_post_job_audit(
                db, job_id, session_root=Path(tmp) / "missing-session"
            )
            self.assertEqual(audit["tool_vs_recording"]["result"], "UNKNOWN")
            self.assertIn("missing session", audit["tool_vs_recording"]["reason"])

    def test_chat_terminal_reply_includes_audit_as_app_text(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-chat-post", "move it")
            posted = []
            response = guard_chat_response(db, job_id, "Done")
            self.assertIn("UNVERIFIED", response)
            self.assertIn("ROBIE post-job audit", response)
            self.assertIn(job_id, response)
            self.assertIn("Heartbeat gateway_progress", response)
            self.assertIn("Destination evidence", response)
            self.assertIn("Recording motion", response)
            self.assertIn("Tool vs recording", response)
            self.assertIn("does not authorize COMPLETE", response)
            stored = JobStore(db).get_checkpoint(job_id, "post_job_audit")
            self.assertIsNotNone(stored)
            self.assertFalse(stored["authorizes_complete"])
            audit_terminal_job(
                db,
                job_id,
                session_root=Path(tmp) / "sessions",
                chat_poster=lambda audit, text: posted.append(text),
            )
            self.assertEqual(len(posted), 1)
            self.assertIn("ROBIE post-job audit", posted[0])

    def test_tool_claim_detector_requires_real_playwright_or_nav(self):
        self.assertTrue(tool_calls_claim_mutation("playwright_exec page.goto('/app')"))
        self.assertFalse(tool_calls_claim_mutation("I think it went well"))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for webm frame-diff")
class FfmpegRecordingMotionTests(unittest.TestCase):
    def _write_webm(self, path: Path, source: str) -> None:
        command = [
            shutil.which("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source,
            "-t",
            "2",
            "-r",
            "4",
            "-c:v",
            "libvpx",
            "-b:v",
            "80k",
            str(path),
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest(f"ffmpeg could not encode webm: {result.stderr[-200:]}")

    def test_real_webm_frozen_vs_moving(self):
        with durable_temporary_directory() as tmp:
            frozen = Path(tmp) / "frozen.webm"
            moving = Path(tmp) / "moving.webm"
            self._write_webm(frozen, "color=c=red:s=64x36:d=2")
            self._write_webm(moving, "testsrc=s=64x36:d=2")
            frozen_result = analyze_recording_motion(frozen)
            moving_result = analyze_recording_motion(moving)
            self.assertEqual(frozen_result["result"], "FAIL")
            self.assertEqual(moving_result["result"], "PASS")


if __name__ == "__main__":
    unittest.main()
