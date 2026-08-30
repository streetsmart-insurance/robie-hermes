"""807f8920 follow-tab: recording must analyze the Playwright-driven second tab.

Capture's Playwright connection often only sees the first listing tab.
Chrome /json/list already has Edit/FormEntry/documents. A regression must
FAIL if the recording stays on that listing. No live EZLynx. No SSH. No bind.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.browser_capture import capture
from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.models import JobStatus
from robie_job_engine.playwright_write_guard import install_playwright_write_guards
from robie_job_engine.post_job_audit import (
    frames_show_motion,
    rgb_frame,
    run_post_job_audit,
)
from robie_job_engine.recording import RecordingStore
from robie_job_engine.recording_tab import (
    TabCandidate,
    first_ezlynx_wins,
    follow_capture_ticks,
    follow_screencast_frames,
    list_cdp_page_candidates,
    merge_capture_tabs,
    publish_live_playwright_hint,
    recorder_tab_mismatch,
    select_recording_tab,
    should_refresh_cdp_connection,
    tab_candidates_from_cdp_payload,
    write_attach_log,
)
from robie_job_engine.regression_scenarios import (
    FOLLOW_TAB_CHAT,
    run_follow_live_playwright_tab_scenario,
)
from robie_job_engine.store import JobStore


POLICIES = "https://app.ezlynx.com/applicantportal/Policies"
DOCUMENTS = "https://app.ezlynx.com/applicantportal/Documents"
EDIT = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83184565"
FORMENTRY = "https://app.ezlynx.com/applicantportal/FormEntry/220250093"
PLAYWRIGHT_TEXT = (
    f"playwright_exec page.goto({EDIT!r}) then FormEntry {FORMENTRY} "
    "and Save and Continue on account 220250093"
)
FRAME_W = 24
FRAME_H = 16
STALE_RED = (200, 10, 10)
EDIT_BLUE = (10, 10, 220)


class _Driven:
    def __init__(self, identity: str, url: str, color: tuple[int, int, int]) -> None:
        self.identity = identity
        self.url = url
        self.color = color
        self.last_navigated_at = 0.0

    def candidate(self) -> TabCandidate:
        return TabCandidate(self.identity, self.url, self.last_navigated_at)

    def frame(self) -> bytes:
        return rgb_frame(FRAME_W, FRAME_H, self.color)


class FollowLivePlaywrightTabTests(unittest.TestCase):
    def test_named_scenario_id_is_recording_follow_live_playwright_tab(self):
        with durable_temporary_directory() as tmp:
            result = run_follow_live_playwright_tab_scenario(work_dir=Path(tmp))
        self.assertEqual(result["id"], "recording:follow-live-playwright-tab")
        self.assertTrue(result["ok"], result.get("evidence"))
        self.assertIn("807f8920", FOLLOW_TAB_CHAT)

    def test_pages_only_and_first_ezlynx_stay_on_listing_while_cdp_follows_edit(self):
        listing = _Driven("cdp-listing", POLICIES, STALE_RED)
        driven = _Driven("cdp-driven", "about:blank", (0, 0, 0))
        by_id = {listing.identity: listing, driven.identity: driven}

        def snapshot(tab: TabCandidate) -> bytes:
            return by_id[tab.identity].frame()

        playwright_ticks: list[list[TabCandidate]] = []
        cdp_ticks: list[list[TabCandidate]] = []

        def snap() -> None:
            playwright_ticks.append([listing.candidate()])
            cdp_ticks.append([listing.candidate(), driven.candidate()])

        snap()
        driven.url = DOCUMENTS
        driven.color = (10, 200, 10)
        driven.last_navigated_at = 2.0
        snap()
        driven.url = EDIT
        driven.color = EDIT_BLUE
        driven.last_navigated_at = 3.0
        snap()
        driven.url = FORMENTRY
        driven.color = (220, 220, 10)
        driven.last_navigated_at = 4.0
        snap()

        old = follow_screencast_frames(
            playwright_ticks, snapshot, selector=first_ezlynx_wins
        )
        pages_only = follow_screencast_frames(
            playwright_ticks, snapshot, selector=select_recording_tab, hint_url=EDIT
        )
        followed = follow_capture_ticks(
            playwright_ticks, cdp_ticks, snapshot, hint_url=EDIT
        )

        self.assertEqual(old["final_url"], POLICIES)
        self.assertEqual(pages_only["final_url"], POLICIES)
        self.assertEqual(frames_show_motion(old["frames"])["result"], "FAIL")
        self.assertEqual(frames_show_motion(pages_only["frames"])["result"], "FAIL")
        self.assertEqual(
            recorder_tab_mismatch(
                {
                    "initial_url": old["initial_url"],
                    "final_url": old["final_url"],
                    "attached_urls": old["attached_urls"],
                },
                PLAYWRIGHT_TEXT,
            )["result"],
            "MISMATCH",
        )

        self.assertEqual(followed["final_url"], FORMENTRY)
        self.assertIn(EDIT, followed["attached_urls"])
        self.assertGreaterEqual(followed["rebinds"], 1)
        self.assertEqual(frames_show_motion(followed["frames"])["result"], "PASS")
        self.assertNotEqual(followed["frames"][-1], listing.frame())
        self.assertEqual(
            recorder_tab_mismatch(
                {
                    "initial_url": followed["initial_url"],
                    "final_url": followed["final_url"],
                    "attached_urls": followed["attached_urls"],
                },
                PLAYWRIGHT_TEXT,
            )["result"],
            "MATCH",
        )

    def test_json_list_payload_is_source_of_truth_over_playwright_pages(self):
        listing = TabCandidate("pw-only", POLICIES)
        payload = [
            {"id": "t1", "type": "page", "url": POLICIES, "title": "Policies"},
            {"id": "t2", "type": "page", "url": EDIT, "title": "Edit"},
            {"id": "svc", "type": "service_worker", "url": "https://app.ezlynx.com/sw"},
        ]
        cdp = tab_candidates_from_cdp_payload(payload)
        self.assertEqual([tab.url for tab in cdp], [POLICIES, EDIT])
        merged = merge_capture_tabs([listing], cdp)
        self.assertEqual(select_recording_tab(merged, hint_url=EDIT).url, EDIT)
        self.assertEqual(select_recording_tab([listing], hint_url=EDIT).url, POLICIES)
        self.assertTrue(should_refresh_cdp_connection([listing], cdp, hint_url=EDIT))
        self.assertFalse(should_refresh_cdp_connection(cdp, cdp, hint_url=EDIT))

        def http_get(url: str) -> tuple[int, bytes]:
            self.assertTrue(url.endswith("/json/list") or url.endswith("/json"))
            if url.endswith("/json/list"):
                return 200, json.dumps(payload).encode("utf-8")
            return 500, b"no"

        listed = list_cdp_page_candidates(
            cdp_url="http://127.0.0.1:9222", http_get=http_get
        )
        self.assertEqual([tab.identity for tab in listed], ["t1", "t2"])

    def test_audit_fails_when_recording_stays_on_listing(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "message-807f8920", "edit policy 220250093")
            store = JobStore(db)
            store.checkpoint(
                job_id, "worker_response", {"response_text": PLAYWRIGHT_TEXT}
            )
            store.transition(
                job_id,
                JobStatus.UNVERIFIED,
                expected={JobStatus.RUNNING},
                error="fixture",
                release_lease=True,
            )
            video = Path(tmp) / "listing.webm"
            video.write_bytes(b"stale-listing")
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
            recordings = RecordingStore(db)
            recording = recordings.create(job_id, video, video.with_suffix(".stop"))
            recordings.update(
                recording["id"],
                status="READY",
                drive_url="https://drive.google.com/file/d/listing/view",
                drive_file_id="listing",
            )
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
                extract_frames=lambda _path: moving,
            )
            self.assertEqual(audit["recording_motion"]["result"], "FAIL")
            self.assertIn("wrong-tab", audit["recording_motion"]["reason"])
            self.assertEqual(audit["tool_vs_recording"]["result"], "MISMATCH")

    def test_publish_live_hint_skips_listing_and_writes_edit(self):
        with durable_temporary_directory() as tmp:
            hint = Path(tmp) / "job.hint.json"
            os.environ["ROBIE_RECORDING_HINT_FILE"] = str(hint)

            class _Page:
                def __init__(self, url: str) -> None:
                    self.url = url

            try:
                self.assertIsNone(
                    publish_live_playwright_hint([_Page(POLICIES)], page=_Page(POLICIES))
                )
                self.assertFalse(hint.exists())
                written = publish_live_playwright_hint(
                    [_Page(POLICIES), _Page(EDIT)], page=_Page(POLICIES)
                )
                self.assertEqual(written, EDIT)
                self.assertEqual(json.loads(hint.read_text(encoding="utf-8"))["url"], EDIT)
            finally:
                os.environ.pop("ROBIE_RECORDING_HINT_FILE", None)

    def test_write_guard_does_not_hint_listing_page(self):
        with durable_temporary_directory() as tmp:
            hint = Path(tmp) / "job.hint.json"
            os.environ["ROBIE_RECORDING_HINT_FILE"] = str(hint)
            os.environ["ROBIE_EZLYNX_WRITE_APPLICANT_ID"] = "220250093"

            class _Page:
                url = POLICIES

                def count(self):
                    return 1

                def fill(self, selector, value):
                    return None

            try:
                install_playwright_write_guards({"page": _Page(), "Page": _Page})
                listing = _Page()
                listing.fill("#Search", "ROBIE")
                self.assertFalse(hint.exists())
                edit = _Page()
                edit.url = EDIT
                edit.fill("#NamedInsured", "ROBIE Test LLC")
                self.assertTrue(hint.exists())
                self.assertEqual(
                    json.loads(hint.read_text(encoding="utf-8"))["url"], EDIT
                )
            finally:
                os.environ.pop("ROBIE_RECORDING_HINT_FILE", None)
                os.environ.pop("ROBIE_EZLYNX_WRITE_APPLICANT_ID", None)

    def test_capture_source_uses_cdp_list_not_first_ezlynx(self):
        source = Path(capture.__code__.co_filename).read_text(encoding="utf-8")
        self.assertIn("list_cdp_page_candidates", source)
        self.assertIn("should_refresh_cdp_connection", source)
        self.assertIn("pair_playwright_pages_to_tabs", source)
        self.assertIn("Do not browser.close()", source)
        self.assertNotIn("first_ezlynx_wins", source)


if __name__ == "__main__":
    unittest.main()
