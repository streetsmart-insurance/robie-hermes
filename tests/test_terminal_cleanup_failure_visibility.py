"""A failed post-terminal audit/tab_cleanup must be logged, not silent.

Carlo 2026-08-31: leftover tabs kept accumulating on Production with no
visible cause. tab_cleanup.py already closes a job's tabs automatically at
terminal status (engine.py, via maybe_audit_terminal_job), but any failure
in that path was swallowed by a bare `except Exception: pass` - so a broken
cleanup run left zero trace anywhere. This must never flip a job's own
outcome (best-effort side effect), but it must be visible.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.browser_read import BoundedBrowserReadWorker, BrowserReadVerifier
from robie_job_engine.engine import JobEngine
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


class MemoryBrowser:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.reads = 0

    def read_fresh(self, locator):
        self.reads += 1
        key = locator.get("url") or locator.get("title")
        if key not in self.pages:
            raise LookupError(f"page not found: {key}")
        return dict(self.pages[key])


class TerminalCleanupFailureVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root / "jobs.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_broken_terminal_tab_cleanup_is_logged_and_job_still_completes(self):
        pages = {"https://example.test/page": {"url": "https://example.test/page", "title": "Ok"}}
        browser = MemoryBrowser(pages)
        engine = JobEngine(
            self.store,
            {"browser-read": BoundedBrowserReadWorker(browser)},
            {"browser.read": BrowserReadVerifier(browser)},
        )
        job = self.store.create_job(
            "browser.read",
            {
                "worker": "browser-read",
                "locator": {"url": "https://example.test/page"},
                "expected": {"url": "https://example.test/page", "title": "Ok"},
            },
            idempotency_key="terminal-cleanup-broken",
        )
        with patch(
            "robie_job_engine.post_job_audit.maybe_audit_terminal_job",
            side_effect=RuntimeError("CDP unreachable: connection refused"),
        ):
            with self.assertLogs("robie_job_engine.engine", level="ERROR") as captured:
                final = engine.run(job["id"])

        # Fail-open: a broken cleanup must never flip the job's own outcome.
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        # But it must no longer be invisible.
        joined = "\n".join(captured.output)
        self.assertIn(job["id"], joined)
        self.assertIn("post-terminal cleanup failed", joined)
        self.assertIn("CDP unreachable", joined)


if __name__ == "__main__":
    unittest.main()
