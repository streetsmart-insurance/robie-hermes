"""Dead-tab cleanup after COMPLETE/FAILED/UNVERIFIED and orphan sweep.

No live EZLynx. No SSH. No Chrome restart. No client-file navigation.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.chat_queue import DurableChatEventQueue
from robie_job_engine.models import JobStatus
from robie_job_engine.post_job_audit import audit_terminal_job
from robie_job_engine.production_preflight import run_production_preflight
from robie_job_engine.recording_tab import (
    TabCandidate,
    first_ezlynx_wins,
    read_page_hint,
    select_playwright_page,
    select_recording_tab,
    write_page_hint,
)
from robie_job_engine.store import JobStore
from robie_job_engine.tab_cleanup import (
    BrowserTab,
    cleanup_terminal_job_tabs,
    flush_orphaned_tabs,
    plan_tab_cleanup,
    retarget_recorder_hint,
    sweep_orphaned_tabs,
    tabs_from_pages,
)


SESSION = "https://app.ezlynx.com/web/"
POLICIES = "https://app.ezlynx.com/web/policies"
LOGIN = "https://app.ezlynx.com/auth/account/login"
LIVE_ACCOUNT = "31897605"
STALE_ACCOUNT = "40404040"
LIVE_OVERVIEW = f"https://app.ezlynx.com/web/account/{LIVE_ACCOUNT}/overview"
LIVE_DOCUMENTS = f"https://app.ezlynx.com/web/account/{LIVE_ACCOUNT}/documents"
STALE_OVERVIEW = f"https://app.ezlynx.com/web/account/{STALE_ACCOUNT}/overview"
STALE_DOCUMENTS = f"https://app.ezlynx.com/web/account/{STALE_ACCOUNT}/documents"
BLANK = "about:blank"
ASCEND = "https://app.ascend.com/workspace/file"
GOOGLE = "https://www.google.com/"
ASCEND_DASH = "https://dashboard.useascend.com/quotes"


class FakePage:
    def __init__(self, url: str, identity: str) -> None:
        self.url = url
        self._guid = identity
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _pages(*pairs: tuple[str, str]) -> list[FakePage]:
    return [FakePage(url, identity) for identity, url in pairs]


def _cdp(*pairs: tuple[str, str]) -> list[BrowserTab]:
    return [BrowserTab(identity=identity, url=url) for identity, url in pairs]


def _complete_job(db: str, text: str, *, status: JobStatus = JobStatus.FAILED) -> str:
    store = JobStore(db)
    job_id = open_chat_job(db, f"message-{status.value}-{id(text)}-{text[:12]}", text)
    if status != JobStatus.FAILED:
        with store.connect() as conn:
            conn.execute("UPDATE jobs SET status=? WHERE id=?", (status.value, job_id))
        return job_id
    current = JobStatus(store.get_job(job_id)["status"])
    store.transition(
        job_id,
        JobStatus.FAILED,
        expected={current},
        error="fixture terminal",
        release_lease=True,
    )
    return job_id


def _running_job(db: str, text: str) -> str:
    return open_chat_job(db, f"message-live-{id(text)}-{text[:12]}", text)


class JobTerminalCleanupTests(unittest.TestCase):
    def test_terminal_closes_job_owned_pages_and_leaves_one_web_session(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = _complete_job(
                db, f"open EZLynx account {LIVE_ACCOUNT} documents"
            )
            pages = _pages(
                ("session", SESSION),
                ("overview", LIVE_OVERVIEW),
                ("docs", LIVE_DOCUMENTS),
                ("login", LOGIN),
                ("blank", BLANK),
                ("ascend", ASCEND),
            )
            result = cleanup_terminal_job_tabs(db, job_id, pages=pages)
            self.assertTrue(result["ok"])
            self.assertFalse(pages[0].closed)
            self.assertTrue(pages[1].closed)
            self.assertTrue(pages[2].closed)
            self.assertTrue(pages[3].closed)
            self.assertTrue(pages[4].closed)
            self.assertTrue(pages[5].closed)
            self.assertEqual(result["session_url"], SESSION)
            self.assertIn(SESSION, result["kept_urls"])
            self.assertNotIn(LIVE_OVERVIEW, result["kept_urls"])
            self.assertEqual(
                [page.url for page in pages if not page.closed],
                [SESSION],
            )

    def test_terminal_closes_leftover_pages_of_any_host_the_job_opened(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = _complete_job(
                db,
                (
                    f"open EZLynx account {LIVE_ACCOUNT} then "
                    f"{GOOGLE} and {ASCEND_DASH}"
                ),
            )
            pages = _pages(
                ("session", SESSION),
                ("overview", LIVE_OVERVIEW),
                ("google", GOOGLE),
                ("ascend-dash", ASCEND_DASH),
            )
            result = cleanup_terminal_job_tabs(db, job_id, pages=pages)
            self.assertTrue(result["ok"])
            self.assertFalse(pages[0].closed)
            self.assertTrue(pages[1].closed)
            self.assertTrue(pages[2].closed)
            self.assertTrue(pages[3].closed)
            self.assertEqual(result["session_url"], SESSION)
            self.assertEqual(
                [page.url for page in pages if not page.closed],
                [SESSION],
            )

    def test_unverified_and_complete_also_close_job_owned_pages(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            for status in (JobStatus.UNVERIFIED, JobStatus.COMPLETE):
                with self.subTest(status=status):
                    job_id = _complete_job(
                        db,
                        f"{status.value} account {LIVE_ACCOUNT} overview",
                        status=status,
                    )
                    pages = _pages(
                        (f"session-{status.value}", SESSION),
                        (f"owned-{status.value}", LIVE_OVERVIEW),
                    )
                    result = cleanup_terminal_job_tabs(db, job_id, pages=pages)
                    self.assertTrue(result["ok"])
                    self.assertFalse(pages[0].closed)
                    self.assertTrue(pages[1].closed)


class SweepCleanupTests(unittest.TestCase):
    def test_sweep_does_not_close_a_live_job_tab(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            live_id = _running_job(
                db, f"work EZLynx account {LIVE_ACCOUNT} documents"
            )
            pages = _pages(
                ("session", SESSION),
                ("live-docs", LIVE_DOCUMENTS),
                ("stale", STALE_OVERVIEW),
                ("login", LOGIN),
                ("blank", BLANK),
            )
            result = sweep_orphaned_tabs(db, pages=pages)
            self.assertTrue(result["ok"])
            self.assertIn(live_id, result["live_job_ids"])
            self.assertFalse(pages[0].closed)
            self.assertFalse(pages[1].closed)
            self.assertTrue(pages[2].closed)
            self.assertTrue(pages[3].closed)
            self.assertTrue(pages[4].closed)
            self.assertIn(LIVE_DOCUMENTS, result["kept_urls"])
            self.assertIn(SESSION, result["kept_urls"])

    def test_sweep_closes_extra_login_and_stale_account_tabs(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            JobStore(db)
            pages = _pages(
                ("session", SESSION),
                ("extra-home", POLICIES),
                ("stale-overview", STALE_OVERVIEW),
                ("stale-docs", STALE_DOCUMENTS),
                ("login", LOGIN),
                ("blank", BLANK),
            )
            result = sweep_orphaned_tabs(db, pages=pages)
            self.assertTrue(result["ok"])
            self.assertFalse(pages[0].closed)
            self.assertTrue(pages[1].closed)
            self.assertTrue(pages[2].closed)
            self.assertTrue(pages[3].closed)
            self.assertTrue(pages[4].closed)
            self.assertTrue(pages[5].closed)
            self.assertEqual(result["session_url"], SESSION)
            self.assertEqual(
                [page.url for page in pages if not page.closed],
                [SESSION],
            )

    def test_sweep_leaves_unclaimed_non_ezlynx_leftover(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            JobStore(db)
            pages = _pages(
                ("session", SESSION),
                ("google", GOOGLE),
                ("ascend-dash", ASCEND_DASH),
            )
            result = sweep_orphaned_tabs(db, pages=pages)
            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "sweep")
            self.assertFalse(pages[0].closed)
            self.assertFalse(pages[1].closed)
            self.assertFalse(pages[2].closed)
            self.assertIn(GOOGLE, result["kept_urls"])
            self.assertIn(ASCEND_DASH, result["kept_urls"])


class FlushCleanupTests(unittest.TestCase):
    def test_flush_closes_random_leftover_keeps_session_and_live_job(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            live_id = _running_job(
                db, f"work EZLynx account {LIVE_ACCOUNT} documents"
            )
            pages = _pages(
                ("session", SESSION),
                ("live-docs", LIVE_DOCUMENTS),
                ("google", GOOGLE),
                ("ascend-dash", ASCEND_DASH),
            )
            result = flush_orphaned_tabs(db, pages=pages)
            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "flush")
            self.assertIn(live_id, result["live_job_ids"])
            self.assertFalse(pages[0].closed)
            self.assertFalse(pages[1].closed)
            self.assertTrue(pages[2].closed)
            self.assertTrue(pages[3].closed)
            self.assertIn(SESSION, result["kept_urls"])
            self.assertIn(LIVE_DOCUMENTS, result["kept_urls"])
            self.assertIn(GOOGLE, result["closed_urls"])
            self.assertIn(ASCEND_DASH, result["closed_urls"])
            self.assertEqual(
                [page.url for page in pages if not page.closed],
                [SESSION, LIVE_DOCUMENTS],
            )


class RecorderHintAfterCleanupTests(unittest.TestCase):
    def test_recorder_hint_is_not_first_ezlynx_wins_after_cleanup(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            live_id = _running_job(
                db, f"edit documents on account {LIVE_ACCOUNT}"
            )
            hint = Path(tmp) / "job.hint.json"
            os.environ["ROBIE_RECORDING_HINT_FILE"] = str(hint)
            write_page_hint(hint, url=LIVE_DOCUMENTS, job_id=live_id)
            try:
                listed = _cdp(
                    ("first-ezlynx", SESSION),
                    ("login", LOGIN),
                    ("stale", STALE_OVERVIEW),
                    ("job", LIVE_DOCUMENTS),
                )
                result = sweep_orphaned_tabs(db, tabs=listed, closer=lambda tab: None)
                remaining = [
                    tab
                    for tab in listed
                    if tab.url in {SESSION, LIVE_DOCUMENTS}
                ]
                self.assertEqual(remaining[0].url, SESSION)
                self.assertEqual(first_ezlynx_wins(remaining).url, SESSION)
                chosen = select_recording_tab(
                    [
                        TabCandidate(tab.identity, tab.url)
                        for tab in remaining
                    ],
                    hint_url=LIVE_DOCUMENTS,
                )
                self.assertIsNotNone(chosen)
                self.assertEqual(chosen.url, LIVE_DOCUMENTS)
                self.assertNotEqual(chosen.identity, remaining[0].identity)
                self.assertNotEqual(
                    first_ezlynx_wins(remaining).identity,
                    chosen.identity,
                )
                retargeted = retarget_recorder_hint(
                    remaining,
                    hint_url=LIVE_DOCUMENTS,
                    job_id=live_id,
                    hint_path=hint,
                )
                self.assertEqual(retargeted["url"], LIVE_DOCUMENTS)
                self.assertEqual(read_page_hint(hint)["url"], LIVE_DOCUMENTS)
                self.assertEqual(result["hint"]["url"], LIVE_DOCUMENTS)
                self.assertEqual(result["selection_mode"], "recent_navigation")
                self.assertNotEqual(result["selection_mode"], "first_ezlynx")

                pages = _pages(
                    ("first-ezlynx", SESSION),
                    ("job", LIVE_DOCUMENTS),
                )
                picked = select_playwright_page(pages, hint_url=LIVE_DOCUMENTS)
                self.assertIs(picked, pages[1])
                self.assertIsNot(picked, pages[0])
            finally:
                os.environ.pop("ROBIE_RECORDING_HINT_FILE", None)


class PlanAndAuditHookTests(unittest.TestCase):
    def test_plan_never_closes_a_live_claimed_account_tab(self):
        from robie_job_engine.tab_cleanup import TabClaims

        tabs = _cdp(
            ("session", SESSION),
            ("live", LIVE_DOCUMENTS),
            ("stale", STALE_OVERVIEW),
            ("login", LOGIN),
        )
        plan = plan_tab_cleanup(
            tabs,
            live=TabClaims(account_ids={LIVE_ACCOUNT}, urls={LIVE_DOCUMENTS}),
            mode="sweep",
        )
        closed = {tab.url for tab in plan.close}
        kept = {tab.url for tab in plan.keep}
        self.assertIn(LIVE_DOCUMENTS, kept)
        self.assertNotIn(LIVE_DOCUMENTS, closed)
        self.assertIn(LOGIN, closed)
        self.assertIn(STALE_OVERVIEW, closed)
        self.assertIn(SESSION, kept)

    def test_audit_terminal_job_records_tab_cleanup(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = _complete_job(db, f"account {LIVE_ACCOUNT} overview")
            pages = _pages(("session", SESSION), ("owned", LIVE_OVERVIEW))
            audit = audit_terminal_job(db, job_id)
            self.assertIn("tab_cleanup", audit)
            self.assertFalse(audit["authorizes_complete"])
            result = cleanup_terminal_job_tabs(db, job_id, pages=pages)
            self.assertTrue(pages[1].closed)
            self.assertFalse(pages[0].closed)
            self.assertEqual(result["session_url"], SESSION)

    def test_preflight_sweep_uses_injected_tabs_without_goto(self):
        from robie_job_engine.production_preflight import (
            CANONICAL_JOB_ENGINE_ROOT,
        )

        source = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "production_preflight.py"
        ).read_text(encoding="utf-8")
        self.assertIn("maybe_flush_orphaned_tabs", source)
        self.assertNotIn("page.goto", source)
        self.assertNotIn("systemctl restart", source)
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            JobStore(db)
            DurableChatEventQueue(db)
            report = run_production_preflight(
                gateway_probe={
                    "active": True,
                    "active_state": "active",
                    "pythonpath": f"{CANONICAL_JOB_ENGINE_ROOT}:/srv/robie/current",
                    "canonical_root": CANONICAL_JOB_ENGINE_ROOT,
                    "job_engine_present": True,
                },
                cdp_http_get=lambda url: (
                    (200, b"{}")
                    if url.endswith("/json/version")
                    else (200, json.dumps([{"url": SESSION}]).encode())
                ),
                ezlynx_tabs=[SESSION, LOGIN, STALE_OVERVIEW, GOOGLE, ASCEND_DASH],
                secret_inspector=lambda: {
                    "result": "OK",
                    "secrets": [
                        {
                            "secret_id": "ezlynx-username",
                            "newest_enabled_version": "versions/3",
                            "missing_enabled": False,
                        },
                        {
                            "secret_id": "ezlynx-password",
                            "newest_enabled_version": "versions/1",
                            "missing_enabled": False,
                        },
                    ],
                },
                db_path=db,
                journal="[GoogleChat] Connected; inbound=pubsub\n",
                poster=lambda *_args, **_kwargs: None,
            )
        self.assertTrue(report["ok"])
        flush = report.get("tab_flush") or report.get("tab_sweep") or {}
        self.assertEqual(flush.get("mode"), "flush")
        self.assertIn("closed_urls", flush)
        self.assertIn(LOGIN, flush["closed_urls"])
        self.assertIn(STALE_OVERVIEW, flush["closed_urls"])
        self.assertIn(GOOGLE, flush["closed_urls"])
        self.assertIn(ASCEND_DASH, flush["closed_urls"])
        self.assertEqual(flush.get("session_url"), SESSION)
        self.assertNotIn(SESSION, flush.get("closed_urls") or [])


class PlaywrightPageSelectionTests(unittest.TestCase):
    def test_playwright_tool_refuses_pages0_bind(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "hermes"
            / "tools"
            / "playwright_tool.py"
        ).read_text(encoding="utf-8")
        self.assertIn("select_playwright_page", source)
        self.assertIn("refusing pages[0] / first-ezlynx-wins", source)
        self.assertNotIn("page = pages[0] if pages else context.new_page()", source)
        self.assertIn("install_playwright_write_guards", source)
        self.assertIn("PLAYWRIGHT_BLOCKED", source)

    def test_pages_as_candidates_round_trip(self):
        pages = _pages(("stale", SESSION), ("job", LIVE_DOCUMENTS))
        self.assertEqual(tabs_from_pages(pages)[0].url, SESSION)
        picked = select_playwright_page(pages, hint_url=LIVE_DOCUMENTS)
        self.assertEqual(picked.url, LIVE_DOCUMENTS)


if __name__ == "__main__":
    unittest.main()
