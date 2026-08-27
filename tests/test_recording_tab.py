"""Recorder tab selection and job 30777947 wrong-tab audit.

No live EZLynx. No SSH. Selection is first-ezlynx-wins no longer.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.models import JobStatus
from robie_job_engine.playwright_write_guard import install_playwright_write_guards
from robie_job_engine.post_job_audit import (
    format_audit_chat_message,
    rgb_frame,
    run_post_job_audit,
)
from robie_job_engine.recording import RecordingStore, SubprocessTabCapture
from robie_job_engine.recording_tab import (
    TabCandidate,
    read_page_hint,
    recorder_tab_mismatch,
    select_recording_tab,
    write_attach_log,
    write_page_hint,
)
from robie_job_engine.store import JobStore


POLICIES = "https://app.ezlynx.com/applicantportal/Policies"
DOCUMENTS = "https://app.ezlynx.com/applicantportal/Documents"
EDIT = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83184565"
FORMENTRY = "https://app.ezlynx.com/applicantportal/FormEntry/220250093"

JOB_30777947_PLAYWRIGHT = (
    "37 unique playwright_exec calls on documents then "
    f"{EDIT} and FormEntry; 20 naming 220250093; 0 other accounts; "
    "3 save-shaped clicks (Save and Continue)"
)


def _tabs(*urls: str, nav: list[float] | None = None) -> list[TabCandidate]:
    times = nav or [0.0] * len(urls)
    return [
        TabCandidate(identity=f"tab-{index}", url=url, last_navigated_at=times[index])
        for index, url in enumerate(urls)
    ]


def _ready_recording(db: str, job_id: str, path: Path) -> None:
    store = RecordingStore(db)
    recording = store.create(job_id, path, path.with_suffix(".stop"))
    store.update(
        recording["id"],
        status="READY",
        drive_url="https://drive.google.com/file/d/audit-30777947/view",
        drive_file_id="audit-30777947",
    )


def _extract(frames: list[bytes]):
    def _inner(_path):
        return frames

    return _inner


class SelectRecordingTabTests(unittest.TestCase):
    def test_first_ezlynx_policies_loses_to_edit_at_equal_timestamps(self):
        chosen = select_recording_tab(_tabs(POLICIES, DOCUMENTS, EDIT))
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.url, EDIT)
        self.assertNotEqual(chosen.identity, "tab-0")
        self.assertEqual(select_recording_tab(_tabs(POLICIES, DOCUMENTS)).url, DOCUMENTS)

    def test_most_recently_navigated_edit_beats_older_listing(self):
        chosen = select_recording_tab(_tabs(POLICIES, EDIT, nav=[10.0, 40.0]))
        self.assertEqual(chosen.url, EDIT)

    def test_new_edit_tab_beats_stale_policies_even_if_stamp_is_zero(self):
        chosen = select_recording_tab(_tabs(POLICIES, EDIT, nav=[5.0, 0.0]))
        self.assertEqual(chosen.url, EDIT)

    def test_active_work_hint_wins(self):
        chosen = select_recording_tab(
            _tabs(POLICIES, EDIT, nav=[90.0, 1.0]),
            hint_url=EDIT,
        )
        self.assertEqual(chosen.url, EDIT)

    def test_listing_hint_does_not_pin_stale_policies_when_edit_exists(self):
        chosen = select_recording_tab(
            _tabs(POLICIES, EDIT, nav=[90.0, 1.0]),
            hint_url=POLICIES,
        )
        self.assertEqual(chosen.url, EDIT)

    def test_hint_file_round_trip(self):
        with durable_temporary_directory() as tmp:
            path = Path(tmp) / "job.hint.json"
            write_page_hint(path, url=EDIT, job_id="30777947")
            data = read_page_hint(path)
            self.assertEqual(data["url"], EDIT)
            self.assertEqual(data["job_id"], "30777947")


class RecorderTabMismatchTests(unittest.TestCase):
    def test_policies_attach_vs_edit_playwright_is_mismatch(self):
        attach = {
            "initial_url": POLICIES,
            "final_url": POLICIES,
            "attached_urls": [POLICIES],
            "rebinds": 0,
            "selection_mode": "first_ezlynx",
        }
        result = recorder_tab_mismatch(attach, JOB_30777947_PLAYWRIGHT)
        self.assertEqual(result["result"], "MISMATCH")
        self.assertIn("different tab", result["reason"])
        self.assertIn("220250093", result["playwright_account_ids"])

    def test_matching_edit_tabs_are_not_mismatch(self):
        attach = {
            "initial_url": EDIT,
            "final_url": FORMENTRY,
            "attached_urls": [DOCUMENTS, EDIT, FORMENTRY],
            "rebinds": 2,
            "selection_mode": "recent_navigation",
        }
        result = recorder_tab_mismatch(attach, JOB_30777947_PLAYWRIGHT)
        self.assertEqual(result["result"], "MATCH")


class Job30777947AuditTests(unittest.TestCase):
    def test_wrong_tab_fails_motion_and_mismatch_even_if_frames_move(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = open_chat_job(db, "message-30777947", "edit policy 220250093")
            store.checkpoint(
                job_id,
                "worker_response",
                {"response_text": JOB_30777947_PLAYWRIGHT},
            )
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
            video = Path(tmp) / "30777947-policies.webm"
            video.write_bytes(b"frozen-policies-webm")
            write_attach_log(
                video,
                {
                    "initial_url": POLICIES,
                    "final_url": POLICIES,
                    "attached_urls": [POLICIES],
                    "rebinds": 0,
                    "selection_mode": "first_ezlynx",
                },
            )
            _ready_recording(db, job_id, video)
            session = Path(tmp) / "sessions"
            session.mkdir()
            (session / "job.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "tool": "playwright_exec",
                        "code": f"page.goto({EDIT!r}); page.click('text=Save and Continue')",
                    }
                ),
                encoding="utf-8",
            )
            moving = [
                rgb_frame(8, 6, (10, 10, 10)),
                rgb_frame(8, 6, (240, 10, 10)),
                rgb_frame(8, 6, (10, 240, 10)),
            ]
            audit = run_post_job_audit(
                db,
                job_id,
                session_root=session,
                extract_frames=_extract(moving),
            )
            self.assertEqual(audit["recording_motion"]["result"], "FAIL")
            self.assertIn("frozen / wrong-tab", audit["recording_motion"]["reason"])
            self.assertEqual(audit["tool_vs_recording"]["result"], "MISMATCH")
            self.assertIn("different tab", audit["tool_vs_recording"]["reason"])
            text = format_audit_chat_message(audit)
            self.assertIn("frozen / wrong-tab", text)
            self.assertIn("MISMATCH", text)
            self.assertFalse(audit["authorizes_complete"])
            self.assertNotEqual(store.get_job(job_id)["status"], JobStatus.COMPLETE.value)

    def test_matching_edit_attach_does_not_false_mismatch(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            store = JobStore(db)
            job_id = open_chat_job(db, "message-match-tab", "edit policy 220250093")
            store.checkpoint(
                job_id,
                "worker_response",
                {"response_text": JOB_30777947_PLAYWRIGHT},
            )
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
            video = Path(tmp) / "edit-tab.webm"
            video.write_bytes(b"edit-webm")
            write_attach_log(
                video,
                {
                    "initial_url": DOCUMENTS,
                    "final_url": EDIT,
                    "attached_urls": [DOCUMENTS, EDIT],
                    "rebinds": 1,
                    "selection_mode": "recent_navigation",
                },
            )
            _ready_recording(db, job_id, video)
            session = Path(tmp) / "sessions"
            session.mkdir()
            (session / "job.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "tool": "playwright_exec",
                        "code": f"page.goto({EDIT!r})",
                    }
                ),
                encoding="utf-8",
            )
            moving = [
                rgb_frame(8, 6, (10, 10, 10)),
                rgb_frame(8, 6, (240, 10, 10)),
            ]
            audit = run_post_job_audit(
                db,
                job_id,
                session_root=session,
                extract_frames=_extract(moving),
            )
            self.assertEqual(audit["recording_motion"]["result"], "PASS")
            self.assertNotIn("wrong-tab", audit["recording_motion"].get("reason", ""))
            self.assertEqual(audit["tool_vs_recording"]["result"], "MATCH")


class CaptureCommandAndHintPublishTests(unittest.TestCase):
    def test_subprocess_capture_passes_hint_file(self):
        with durable_temporary_directory() as tmp:
            output = Path(tmp) / "ready.webm"
            stop_file = Path(tmp) / "ready.stop"
            captured: list[list[str]] = []

            def launch(command, **_kwargs):
                captured.append(list(command))
                Path(command[command.index("--ready-file") + 1]).touch(mode=0o600)

                class _Proc:
                    pid = 4141
                    returncode = None

                    def poll(self):
                        return None

                return _Proc()

            with patch("robie_job_engine.recording.subprocess.Popen", side_effect=launch):
                pid = SubprocessTabCapture(ready_timeout=0.2).start(output, stop_file)
            self.assertEqual(4141, pid)
            command = captured[0]
            self.assertIn("--hint-file", command)
            self.assertEqual(
                command[command.index("--hint-file") + 1],
                str(output.with_suffix(".hint.json")),
            )

    def test_write_guard_does_not_hint_pages0_on_install(self):
        with durable_temporary_directory() as tmp:
            hint = Path(tmp) / "job.hint.json"
            os.environ["ROBIE_RECORDING_HINT_FILE"] = str(hint)

            class _Page:
                url = POLICIES

                def count(self):
                    return 1

                def fill(self, selector, value):
                    return None

            try:
                install_playwright_write_guards({"page": _Page(), "Page": _Page})
                self.assertFalse(hint.exists())
                edit = _Page()
                edit.url = EDIT
                edit.fill("#NamedInsured", "ROBIE Test LLC")
                self.assertTrue(hint.exists())
                self.assertEqual(read_page_hint(hint)["url"], EDIT)
            finally:
                os.environ.pop("ROBIE_RECORDING_HINT_FILE", None)


if __name__ == "__main__":
    unittest.main()
