"""Regressions for the three production holes on job 468d1575.

1. Worker prose that claims Filling Policy Shell / identified a carrier from
   quote data with 0 destination evidence must not look like success.
2. READY + missing local file is FAIL; a Playwright-driven second tab must
   produce a real recording file the audit can open, with frame-diff motion.
3. DESTROYED latest + ENABLED older is healthy (aligned with login bootstrap).

No live EZLynx. No SSH. No bind. No secret payloads.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import guard_chat_response, open_chat_job
from robie_job_engine.login_secret_health import (
    format_leftover_note,
    inspect_login_secrets,
    summarize_secret_versions,
)
from robie_job_engine.models import JobStatus, VerificationEvidence
from robie_job_engine.playwright_write_guard import install_playwright_write_guards
from robie_job_engine.post_job_audit import (
    analyze_recording_motion,
    frames_show_motion,
    rgb_frame,
    run_post_job_audit,
)
from robie_job_engine.recording import RecordingManager, RecordingStore
from robie_job_engine.recording_tab import (
    TabCandidate,
    first_ezlynx_wins,
    follow_screencast_frames,
    publish_active_hint_pointer,
    read_page_hint,
    resolve_hint_file,
    select_recording_tab,
    write_attach_log,
)
from robie_job_engine.store import JobStore
from robie_job_engine.worker_contract import (
    UNVERIFIED_STUCK_TEXT,
    claims_unverified_destination_progress,
    sanitize_worker_response,
)


JOB_468D1575_PROSE = (
    "I identified DRIVE NJ INS CO on the quote and I am Filling Policy Shell. "
    "Remaining policy shell details for ROBIE Test LLC are being entered."
)

POLICIES = "https://app.ezlynx.com/applicantportal/Policies"
DOCUMENTS = "https://app.ezlynx.com/applicantportal/Documents"
EDIT = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83184565"
FORMENTRY = "https://app.ezlynx.com/applicantportal/FormEntry/220250093"
FRAME_W = 24
FRAME_H = 16
STALE_RED = (200, 10, 10)
DOC_GREEN = (10, 200, 10)
EDIT_BLUE = (10, 10, 220)
FORM_YELLOW = (220, 220, 10)


def _version(name: str, state: str, created: float) -> SimpleNamespace:
    return SimpleNamespace(name=name, state=state, create_time=created)


PASSWORD_DESTROYED_NEWEST = [
    _version(
        "projects/streetsmart-hermes-poc/secrets/ezlynx-password/versions/1",
        "ENABLED",
        1.0,
    ),
    _version(
        "projects/streetsmart-hermes-poc/secrets/ezlynx-password/versions/2",
        "DESTROYED",
        2.0,
    ),
]
USERNAME_ENABLED = [
    _version(
        "projects/streetsmart-hermes-poc/secrets/ezlynx-username/versions/3",
        "ENABLED",
        3.0,
    ),
]


class FakeCapture:
    def start(self, output_path: Path, stop_file: Path) -> int:
        return 4682

    def stop(self, pid: int, stop_file: Path, output_path: Path) -> None:
        stop_file.touch()
        output_path.write_bytes(b"468d1575-local-webm")


class FakeUploader:
    def upload(self, path: Path, file_name: str) -> tuple[str, str]:
        return "drive-468d1575", "https://drive.google.com/file/d/drive-468d1575/view"


class PlaywrightDrivenPage:
    def __init__(self, identity: str, url: str, color: tuple[int, int, int]) -> None:
        self.identity = identity
        self.url = url
        self.color = color
        self.last_navigated_at = 0.0

    def goto(self, url: str, color: tuple[int, int, int]) -> None:
        self.url = url
        self.color = color
        self.last_navigated_at = time.monotonic()

    def candidate(self) -> TabCandidate:
        return TabCandidate(self.identity, self.url, self.last_navigated_at)

    def frame(self) -> bytes:
        return rgb_frame(FRAME_W, FRAME_H, self.color)


def _snapshot_from(pages: list[PlaywrightDrivenPage]):
    by_id = {page.identity: page for page in pages}

    def snapshot(tab: TabCandidate) -> bytes:
        return by_id[tab.identity].frame()

    return snapshot


def _write_webm(path: Path, frames: list[bytes]) -> None:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise unittest.SkipTest("ffmpeg is required to prove the audit can open a real file")
    raw = b"".join(frames)
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{FRAME_W}x{FRAME_H}",
        "-r",
        "4",
        "-i",
        "pipe:0",
        "-c:v",
        "libvpx",
        "-b:v",
        "80k",
        str(path),
    ]
    result = subprocess.run(command, input=raw, check=False, capture_output=True)
    if result.returncode != 0:
        raise unittest.SkipTest(
            f"ffmpeg could not encode TEST webm: {result.stderr[-200:]!r}"
        )


class Job468d1575WorkerContractTests(unittest.TestCase):
    def test_468d1575_prose_plus_zero_evidence_does_not_look_like_success(self):
        self.assertTrue(claims_unverified_destination_progress(JOB_468D1575_PROSE))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-468d1575", "finish the commercial auto")
            store = JobStore(db)
            response = guard_chat_response(db, job_id, JOB_468D1575_PROSE)
            job = store.get_job(job_id)
            stored = store.get_checkpoint(job_id, "worker_response") or {}
            evidence = store.list_evidence(job_id)
            action = store.get_checkpoint(job_id, "action")

            self.assertIsNone(action)
            self.assertEqual(evidence, [])
            self.assertEqual(job["status"], JobStatus.FAILED.value)
            self.assertNotEqual(job["status"], JobStatus.UNVERIFIED.value)
            self.assertNotEqual(job["status"], JobStatus.COMPLETE.value)
            self.assertIn("FAILED", response)
            self.assertIn("PLAYWRIGHT_SILENT", job["last_error"])
            self.assertIn("zero playwright_exec", response.casefold())
            self.assertNotIn("Filling Policy Shell", response)
            self.assertNotIn("DRIVE NJ INS CO", response)
            self.assertNotIn("Filling Policy Shell", stored.get("response_text") or "")
            self.assertNotIn("DRIVE NJ INS CO", stored.get("response_text") or "")
            self.assertEqual(stored.get("response_text"), UNVERIFIED_STUCK_TEXT)
            self.assertTrue(stored.get("rewritten"))
            self.assertIn("stuck", stored["response_text"].casefold())
            self.assertIn("no verified", stored["response_text"].casefold())

    def test_blocked_prose_is_not_rewritten_as_stuck_success(self):
        blocked = "PLAYWRIGHT_BLOCKED: unique-write could not name the garaging field"
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-blocked", "finish the form")
            payload = sanitize_worker_response(JobStore(db), job_id, blocked)
            self.assertFalse(payload["rewritten"])
            self.assertEqual(payload["response_text"], blocked)

    def test_fill_claim_kept_when_destination_verified_evidence_exists(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-verified", "finish the form")
            store = JobStore(db)
            store.checkpoint(
                job_id,
                "action",
                {"action": "ezlynx.edit", "destination": {"locator": "#NamedInsured"}},
            )
            store.add_evidence(
                job_id,
                True,
                VerificationEvidence(
                    method="FRESH_READBACK",
                    source="destination",
                    expected={"locator": "#NamedInsured"},
                    observed={"locator": "#NamedInsured"},
                    authoritative=True,
                    captured_at="2026-08-27T17:24:29+00:00",
                    locator="#NamedInsured",
                ),
            )
            payload = sanitize_worker_response(store, job_id, JOB_468D1575_PROSE)
            self.assertFalse(payload["rewritten"])
            self.assertIn("Filling Policy Shell", payload["response_text"])

    def test_complete_stays_blocked_without_destination_verified_row(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-no-complete", "finish the form")
            guard_chat_response(db, job_id, JOB_468D1575_PROSE)
            job = JobStore(db).get_job(job_id)
            self.assertNotEqual(job["status"], JobStatus.COMPLETE.value)
            self.assertEqual(len(JobStore(db).list_evidence(job_id)), 0)


class Job468d1575RecordingTests(unittest.TestCase):
    def test_ready_missing_local_file_is_fail_not_pass(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-missing-rec", "edit policy")
            store = JobStore(db)
            store.checkpoint(
                job_id,
                "worker_response",
                {
                    "response_text": (
                        "playwright_exec page.goto("
                        "'/applicantportal/Policy/Actions/Edit/1/2')"
                    )
                },
            )
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="no structured destination action checkpoint",
                release_lease=True,
            )
            missing = Path(tmp) / "gone.webm"
            recordings = RecordingStore(db)
            recording = recordings.create(job_id, missing, missing.with_suffix(".stop"))
            recordings.update(
                recording["id"],
                status="READY",
                drive_url="https://drive.google.com/file/d/a8a0c1cf/view",
                drive_file_id="a8a0c1cf",
            )
            session = Path(tmp) / "sessions"
            session.mkdir()
            (session / "job.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "tool": "playwright_exec",
                        "code": "page.goto('https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/1/2')",
                    }
                ),
                encoding="utf-8",
            )
            audit = run_post_job_audit(db, job_id, session_root=session)
            self.assertEqual(audit["recording_motion"]["result"], "FAIL")
            self.assertIn("missing recording", audit["recording_motion"]["reason"])
            self.assertEqual(audit["tool_vs_recording"]["result"], "MISMATCH")
            self.assertIn("missing", audit["tool_vs_recording"]["reason"])
            self.assertNotIn("recording is frozen", audit["tool_vs_recording"]["reason"])
            self.assertFalse(audit["authorizes_complete"])

    def test_stop_and_upload_keeps_local_file_for_audit(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job = JobStore(db).create_job("hermes.google_chat_task", {"text": "edit"})
            root = Path(tmp) / "recordings"
            manager = RecordingManager(
                db,
                root=root,
                capture=FakeCapture(),
                uploader=FakeUploader(),
                enabled=True,
                keep_local=False,
            )
            manager.start(job["id"])
            result = manager.stop_and_upload(job["id"], "UNVERIFIED")
            self.assertEqual(result["status"], "READY")
            local = Path(result["local_path"])
            self.assertTrue(local.is_file(), "audit must be able to open the local webm")
            self.assertGreater(local.stat().st_size, 0)
            motion = analyze_recording_motion(
                local,
                extract_frames=lambda _path: [
                    rgb_frame(8, 6, (10, 10, 10)),
                    rgb_frame(8, 6, (240, 10, 10)),
                ],
            )
            self.assertNotEqual(motion.get("reason"), "missing recording")
            self.assertEqual(motion["result"], "PASS")
            manager.release_local_after_audit(job["id"])
            self.assertFalse(local.is_file())

    def test_playwright_second_tab_writes_real_file_with_motion(self):
        """Second-tab Playwright navigation must produce a file + frame-diff motion.

        Old first-ezlynx-wins stays frozen on the first listing tab. The
        selector must follow the driven tab, write a webm the audit can open,
        and see motion. Stays red until both the file and the motion exist.
        """
        stale = PlaywrightDrivenPage("tab-stale-first", POLICIES, STALE_RED)
        driven = PlaywrightDrivenPage("tab-playwright", "about:blank", (0, 0, 0))
        pages = [stale, driven]
        snapshot = _snapshot_from(pages)
        ticks: list[list[TabCandidate]] = []

        def snap() -> None:
            ticks.append([page.candidate() for page in pages])

        snap()
        driven.goto(DOCUMENTS, DOC_GREEN)
        snap()
        driven.goto(EDIT, EDIT_BLUE)
        snap()
        driven.goto(FORMENTRY, FORM_YELLOW)
        snap()

        old = follow_screencast_frames(ticks, snapshot, selector=first_ezlynx_wins)
        new = follow_screencast_frames(ticks, snapshot, selector=select_recording_tab)
        old_motion = frames_show_motion(old["frames"])
        new_motion = frames_show_motion(new["frames"])
        self.assertEqual(old_motion["result"], "FAIL")
        self.assertIn("frozen", old_motion["reason"])
        self.assertEqual(new_motion["result"], "PASS")
        self.assertGreater(new_motion["max_mean_abs"], 2.5)
        self.assertIn(EDIT, new["attached_urls"])
        self.assertEqual(new["final_url"], FORMENTRY)

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-second-tab", "edit policy 220250093")
            store = JobStore(db)
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
            video = Path(tmp) / "follow-playwright.webm"
            _write_webm(video, new["frames"])
            self.assertTrue(video.is_file())
            self.assertGreater(video.stat().st_size, 0)
            write_attach_log(
                video,
                {
                    "initial_url": new["initial_url"],
                    "final_url": new["final_url"],
                    "attached_urls": new["attached_urls"],
                    "rebinds": new["rebinds"],
                    "selection_mode": "recent_navigation",
                },
            )
            recordings = RecordingStore(db)
            recording = recordings.create(job_id, video, video.with_suffix(".stop"))
            recordings.update(
                recording["id"],
                status="READY",
                drive_url="https://drive.google.com/file/d/468d1575-follow/view",
                drive_file_id="468d1575-follow",
            )
            opened = analyze_recording_motion(video)
            self.assertEqual(opened["result"], "PASS")
            self.assertIn("motion", opened["reason"])
            audit = run_post_job_audit(db, job_id, session_root=Path(tmp) / "sessions")
            self.assertEqual(audit["recording_motion"]["result"], "PASS")
            self.assertNotIn("missing recording", audit["recording_motion"].get("reason") or "")
            self.assertTrue(Path(audit["recording_motion"]["path"]).is_file())

    def test_write_guard_discovers_hint_from_active_pointer_without_env(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp) / "recordings"
            hint = root / "job.hint.json"
            os.environ["ROBIE_RECORDING_ROOT"] = str(root)
            os.environ["ROBIE_EZLYNX_WRITE_APPLICANT_ID"] = "220250093"
            os.environ.pop("ROBIE_RECORDING_HINT_FILE", None)
            try:
                publish_active_hint_pointer(hint, root=root)
                self.assertEqual(resolve_hint_file(root=root), hint)

                class _Page:
                    url = POLICIES

                    def count(self):
                        return 1

                    def fill(self, selector, value):
                        return None

                install_playwright_write_guards({"page": _Page(), "Page": _Page})
                self.assertFalse(hint.exists())
                edit = _Page()
                edit.url = EDIT
                edit.fill("#NamedInsured", "ROBIE Test LLC")
                self.assertTrue(hint.exists())
                self.assertEqual(read_page_hint(hint)["url"], EDIT)
            finally:
                os.environ.pop("ROBIE_RECORDING_ROOT", None)
                os.environ.pop("ROBIE_RECORDING_HINT_FILE", None)
                os.environ.pop("ROBIE_EZLYNX_WRITE_APPLICANT_ID", None)


class Job468d1575SecretHealthTests(unittest.TestCase):
    def test_destroyed_latest_with_enabled_older_is_healthy(self):
        summary = summarize_secret_versions(
            PASSWORD_DESTROYED_NEWEST, secret_id="ezlynx-password"
        )
        self.assertFalse(summary["alert"])
        self.assertFalse(summary["missing_enabled"])
        self.assertTrue(summary["leftover_destroyed"])
        self.assertEqual(summary["newest_enabled_version"], "versions/1")

        client = Mock()

        def list_versions(request):
            parent = request["parent"]
            if parent.endswith("ezlynx-password"):
                return PASSWORD_DESTROYED_NEWEST
            return USERNAME_ENABLED

        client.list_secret_versions.side_effect = list_versions
        report = inspect_login_secrets(client=client, project="streetsmart-hermes-poc")
        self.assertEqual(report["result"], "OK")
        self.assertFalse(report["should_hold"])
        leftover = format_leftover_note(report)
        self.assertIn("using ENABLED versions/1", leftover)
        self.assertIn("versions/2 is DESTROYED leftover", leftover)
        self.assertNotIn("password is destroyed", leftover.casefold())
        client.access_secret_version.assert_not_called()

    def test_no_enabled_version_still_alerts_and_holds(self):
        summary = summarize_secret_versions(
            [_version("secrets/ezlynx-password/versions/2", "DESTROYED", 2.0)],
            secret_id="ezlynx-password",
        )
        self.assertTrue(summary["missing_enabled"])
        self.assertTrue(summary["alert"])

    def test_chat_surfaces_leftover_not_password_destroyed(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            report = {
                "result": "OK",
                "reason": "ezlynx-password using ENABLED versions/1; versions/2 is DESTROYED leftover",
                "leftover_note": (
                    "ezlynx-password using ENABLED versions/1; "
                    "versions/2 is DESTROYED leftover"
                ),
                "project": "streetsmart-hermes-poc",
                "should_hold": False,
                "secrets": [
                    {
                        "secret_id": "ezlynx-password",
                        "enabled_versions": ["versions/1"],
                        "newest_enabled_version": "versions/1",
                        "newest_version": "versions/2",
                        "newest_state": "DESTROYED",
                        "missing_enabled": False,
                        "newest_destroyed": True,
                        "leftover_destroyed": True,
                        "alert": False,
                    }
                ],
            }
            with patch(
                "robie_job_engine.login_secret_health.inspect_login_secrets",
                return_value=report,
            ):
                job_id = open_chat_job(
                    db,
                    "spaces/s/messages/leftover",
                    "Perform the destination workflow",
                    conversation_id="spaces/s",
                )
            response = guard_chat_response(db, job_id, JOB_468D1575_PROSE)
            self.assertIn("UNVERIFIED", response)
            self.assertIn("using ENABLED versions/1", response)
            self.assertIn("DESTROYED leftover", response)
            self.assertNotIn("password is destroyed", response.casefold())
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["status"], JobStatus.UNVERIFIED.value)


if __name__ == "__main__":
    unittest.main()
