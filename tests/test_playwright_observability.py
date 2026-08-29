"""Named CI coverage for the 1df9740b silent Chat/Playwright gap.

No live Chrome. No hermes-poc-01 / hermes-test-01. No PAWIVA / 221398001.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import (
    guard_chat_response,
    open_chat_job,
    stop_generic_chat_job_heartbeat,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.playwright_observability import (
    CDP_END_CHECKPOINT,
    CDP_START_CHECKPOINT,
    ZERO_PLAYWRIGHT_TOOL_ROWS,
    format_playwright_job_lookup,
    job_requires_playwright,
    lookup_playwright_job,
    persist_cdp_snapshot,
    persist_playwright_exec_finish,
    persist_playwright_exec_start,
    snapshot_cdp_tabs,
    tabs_from_cdp_list,
)
from robie_job_engine.store import JobStore


SILENT_GAP_JOB = "1df9740b-7389-4021-918a-8e65a04d61da"
EZLYNX_COMMERCIAL_AUTO = (
    "Finish ROBIE Test LLC 220250093 commercial auto in EZLynx"
)
ASCEND_LEFTOVER_LIST = [
    {
        "id": "tab-leftover-ascend",
        "type": "page",
        "title": "New program",
        "url": "https://dashboard.useascend.com/create/new?token=super-secret",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/secret",
        "devtoolsFrontendUrl": "/devtools/inspector.html?ws=secret",
        "cookies": [{"name": "sid", "value": "cookie-secret"}],
    },
    {
        "id": "tab-ezlynx",
        "type": "page",
        "title": "EZLynx",
        "url": "https://app.ezlynx.com/web/",
    },
]


class ZeroToolRowFailClosedTests(unittest.TestCase):
    def test_zero_tool_row_playwright_job_fails_closed(self):
        """1df9740b: EZLynx Chat job with heartbeat and no playwright_exec is FAILED."""
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "spaces/silent-gap/messages/1df9740b",
                EZLYNX_COMMERCIAL_AUTO,
                requested_by="Carlo",
                conversation_id="spaces/silent-gap",
            )
            store = JobStore(db)
            job = store.get_job(job_id)
            self.assertTrue(job_requires_playwright(job))
            self.assertEqual(store.list_playwright_exec(job_id), [])
            self.assertIsNotNone(store.get_checkpoint(job_id, "gateway_progress"))
            response = guard_chat_response(
                db,
                job_id,
                "I entered the vehicles and drivers. The commercial auto is done.",
            )
            final = store.get_job(job_id)
            stop_generic_chat_job_heartbeat(db, job_id)
            self.assertEqual(final["status"], JobStatus.FAILED.value)
            self.assertNotEqual(final["status"], JobStatus.UNVERIFIED.value)
            self.assertNotEqual(final["status"], JobStatus.COMPLETE.value)
            self.assertIn("PLAYWRIGHT_SILENT", final["last_error"])
            self.assertIn(SILENT_GAP_JOB[:8], final["last_error"])
            self.assertIn("FAILED", response)
            self.assertNotIn("— UNVERIFIED", response)
            self.assertNotIn("— COMPLETE", response)
            self.assertIn("zero playwright_exec", response.casefold())
            self.assertEqual(store.list_playwright_exec(job_id), [])
            self.assertEqual(store.list_attempts(job_id), [])
            self.assertEqual(store.list_evidence(job_id), [])
            silent = store.get_checkpoint(job_id, "playwright_silent")
            self.assertIsNotNone(silent)
            self.assertEqual(silent["tool_rows"], 0)

    def test_started_playwright_exec_row_survives_missing_result(self):
        """A started row is committed before the runner; worker death still leaves it."""
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "spaces/persist/messages/start-only",
                "open EZLynx and add the commercial auto vehicles",
                conversation_id="spaces/persist",
            )
            row_id = persist_playwright_exec_start(
                db, job_id, "page.goto('https://app.ezlynx.com/web/')"
            )
            self.assertIsInstance(row_id, int)
            rows = JobStore(db).list_playwright_exec(job_id)
            stop_generic_chat_job_heartbeat(db, job_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "started")
            self.assertIn("page.goto", rows[0]["code_preview"])
            persist_playwright_exec_finish(
                db,
                row_id,
                {"ok": False, "error": "PLAYWRIGHT_BLOCKED: runner died"},
            )
            finished = JobStore(db).list_playwright_exec(job_id)
            self.assertEqual(finished[0]["status"], "error")
            self.assertIn("PLAYWRIGHT_BLOCKED", finished[0]["result"]["error"])

    def test_conversation_only_chat_does_not_require_playwright_rows(self):
        job = {
            "action_type": "hermes.google_chat_task",
            "payload": {"text": "what does this mean"},
        }
        self.assertFalse(job_requires_playwright(job))


class CdpSnapshotFixtureTests(unittest.TestCase):
    def test_cdp_snapshot_written_from_json_list_fixture(self):
        """CDP start/end checkpoints from fixture /json/list — no live browser."""
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "spaces/cdp/messages/fixture",
                EZLYNX_COMMERCIAL_AUTO,
                conversation_id="spaces/cdp-fixture",
            )
            store = JobStore(db)
            fixture_bytes = json.dumps(ASCEND_LEFTOVER_LIST).encode("utf-8")

            def http_get(url: str):
                self.assertIn("/json/list", url)
                return 200, fixture_bytes

            start = persist_cdp_snapshot(
                store, job_id, "start", payload=ASCEND_LEFTOVER_LIST
            )
            end = persist_cdp_snapshot(
                store, job_id, "end", http_get=http_get
            )
            stop_generic_chat_job_heartbeat(db, job_id)
            stored_start = store.get_checkpoint(job_id, CDP_START_CHECKPOINT)
            stored_end = store.get_checkpoint(job_id, CDP_END_CHECKPOINT)
            self.assertIsNotNone(stored_start)
            self.assertIsNotNone(stored_end)
            self.assertEqual(stored_start["phase"], "start")
            self.assertEqual(stored_end["phase"], "end")
            self.assertEqual(len(start["tabs"]), 2)
            self.assertEqual(
                start["tabs"][0]["url"],
                "https://dashboard.useascend.com/create/new?token=[REDACTED]",
            )
            self.assertEqual(start["tabs"][0]["title"], "New program")
            self.assertEqual(start["tabs"][1]["url"], "https://app.ezlynx.com/web/")
            self.assertEqual(start["tabs"][1]["title"], "EZLynx")
            blob = json.dumps(stored_start) + json.dumps(stored_end)
            self.assertNotIn("super-secret", blob)
            self.assertNotIn("cookie-secret", blob)
            self.assertNotIn("webSocketDebuggerUrl", blob)
            self.assertNotIn("ws://127.0.0.1:9222/devtools/page/secret", blob)
            self.assertIn("dashboard.useascend.com/create/new", blob)
            self.assertEqual(len(end["tabs"]), 2)

    def test_snapshot_cdp_tabs_accepts_fixture_payload_without_http(self):
        snapshot = snapshot_cdp_tabs(payload=ASCEND_LEFTOVER_LIST)
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["source"], "fixture")
        urls = [tab["url"] for tab in snapshot["tabs"]]
        self.assertTrue(any("useascend.com/create/new" in url for url in urls))
        self.assertTrue(any("app.ezlynx.com/web" in url for url in urls))
        self.assertEqual(tabs_from_cdp_list(ASCEND_LEFTOVER_LIST)[0]["title"], "New program")


class PlaywrightToolPersistTests(unittest.TestCase):
    def test_playwright_exec_writes_started_row_before_runner(self):
        from tests.test_playwright_artifact_fail_closed import (
            _FakeProc,
            _load_playwright_tool,
            _restore_modules,
        )

        tool, previous = _load_playwright_tool()
        try:
            with durable_temporary_directory() as tmp:
                db = str(Path(tmp) / "jobs.db")
                job_id = open_chat_job(
                    db,
                    "spaces/tool/messages/persist",
                    "Run playwright_exec on EZLynx",
                    conversation_id="spaces/tool-persist",
                )
                with patch.object(
                    tool.subprocess,
                    "Popen",
                    return_value=_FakeProc(0, stdout="ok"),
                ):
                    result = tool.playwright_exec(
                        "print('hello')",
                        job_id=job_id,
                        db_path=db,
                    )
                rows = JobStore(db).list_playwright_exec(job_id)
                stop_generic_chat_job_heartbeat(db, job_id)
                self.assertTrue(result.get("success"))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "ok")
                self.assertEqual(rows[0]["tool"], "playwright_exec")
        finally:
            _restore_modules(previous)


class OperatorLookupTests(unittest.TestCase):
    def test_lookup_prints_exec_rows_cdp_tabs_and_trace_path(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            artifacts = Path(tmp) / "artifacts"
            job_id = open_chat_job(
                db,
                "spaces/lookup/messages/open",
                EZLYNX_COMMERCIAL_AUTO,
                conversation_id="spaces/lookup",
            )
            persist_playwright_exec_start(
                db, job_id, "page.goto('https://app.ezlynx.com/web/')"
            )
            persist_cdp_snapshot(db, job_id, "start", payload=ASCEND_LEFTOVER_LIST)
            persist_cdp_snapshot(db, job_id, "end", payload=ASCEND_LEFTOVER_LIST)
            zip_path = artifacts / job_id / "playwright-trace.zip"
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            zip_path.write_bytes(b"PK\x03\x04trace")
            report = lookup_playwright_job(
                job_id, db_path=db, artifact_root=artifacts
            )
            text = format_playwright_job_lookup(report)
            stop_generic_chat_job_heartbeat(db, job_id)
            self.assertTrue(report["ok"])
            self.assertEqual(report["playwright_exec_count"], 1)
            self.assertEqual(report["playwright_exec"][0]["status"], "started")
            self.assertTrue(
                any("useascend.com/create/new" in tab["url"] for tab in report["cdp_tabs_start"])
            )
            self.assertTrue(
                any("app.ezlynx.com/web" in tab["url"] for tab in report["cdp_tabs_end"])
            )
            self.assertTrue(report["trace"]["present"])
            self.assertEqual(report["trace"]["path"], str(zip_path))
            self.assertIn("playwright_exec rows: 1", text)
            self.assertIn("CDP tabs start:", text)
            self.assertIn("CDP tabs end:", text)
            self.assertIn("trace zip:", text)
            self.assertIn(str(zip_path), text)
            self.assertNotIn("super-secret", text)
            self.assertNotIn("cookie-secret", text)
            self.assertNotIn("webSocketDebuggerUrl", text)
            self.assertNotIn("password", text.casefold())

    def test_lookup_zero_rows_still_shows_fail_closed_reason(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "spaces/lookup/messages/silent",
                EZLYNX_COMMERCIAL_AUTO,
                conversation_id="spaces/lookup-silent",
            )
            report = lookup_playwright_job(job_id, db_path=db, artifact_root=tmp)
            text = format_playwright_job_lookup(report)
            stop_generic_chat_job_heartbeat(db, job_id)
            self.assertTrue(report["ok"])
            self.assertTrue(report["zero_tool_rows"])
            self.assertEqual(report["playwright_exec"], [])
            self.assertFalse(report["trace"]["present"])
            self.assertIn("PLAYWRIGHT_SILENT", text)
            self.assertIn("1df9740b", text)

    def test_handoff_must_call_lookup_is_at_the_top(self):
        root = Path(__file__).resolve().parents[1]
        handoff = (root / "HANDOFF.md").read_text(encoding="utf-8")
        state = (root / "CURRENT_STATE.md").read_text(encoding="utf-8")
        head = " ".join(
            handoff.split("## Non-negotiable safety boundary", 1)[0].split()
        )
        self.assertIn("MUST-CALL", head)
        self.assertIn("playwright_observability", head)
        self.assertIn("lookup-playwright-job.py", head)
        self.assertIn("Do not invent leftover RETRY", head)
        self.assertIn("Production is not the first test", head)
        self.assertLess(
            handoff.find("MUST-CALL"),
            handoff.find("Ascend API implementation"),
        )
        self.assertIn("playwright_observability", state)
        self.assertIn("Do not invent leftover RETRY", state)
