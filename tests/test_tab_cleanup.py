"""Dead-tab cleanup after COMPLETE/FAILED/UNVERIFIED and orphan sweep.

No live EZLynx. No SSH. No Chrome restart. No client-file navigation.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import open_chat_job, stop_generic_chat_job_heartbeat
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
    EMPTY_TARGET_EVIDENCE,
    LIVE_TAB_CLAIM_MAX_AGE,
    WRONG_HOST_REFUSED,
    BrowserTab,
    cleanup_terminal_job_tabs,
    ensure_one_browser_page,
    flush_orphaned_tabs,
    flush_tabs_at_job_start,
    live_tab_claims,
    plan_tab_cleanup,
    refuse_wrong_host_at_job_start,
    retarget_recorder_hint,
    sweep_orphaned_tabs,
    tabs_from_pages,
    wrong_host_refuse_reason,
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
ASCEND_CREATE_NEW = "https://dashboard.useascend.com/create/new"


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


def _park_hitl_job(db: str, text: str, *, age: timedelta) -> str:
    job_id = open_chat_job(db, f"message-hitl-{id(text)}-{age.total_seconds()}", text)
    stop_generic_chat_job_heartbeat(db, job_id)
    stamp = (datetime.now(timezone.utc) - age).isoformat()
    with JobStore(db).connect() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, created_at=?, updated_at=? WHERE id=?",
            (JobStatus.AWAITING_HUMAN_INPUT.value, stamp, stamp, job_id),
        )
    return job_id


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

    def test_flush_empty_targets_is_inconclusive_not_session_fine(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            JobStore(db)
            result = flush_orphaned_tabs(
                db,
                tabs=[],
                http_get=lambda url: (_ for _ in ()).throw(OSError("no cdp")),
            )
            self.assertFalse(result["ok"])
            self.assertFalse(result["session_fine"])
            self.assertEqual(result.get("session_status"), "INCONCLUSIVE")
            self.assertIn(EMPTY_TARGET_EVIDENCE, result.get("evidence") or "")
            cleanup_source = (
                Path(__file__).resolve().parents[1]
                / "robie_job_engine"
                / "tab_cleanup.py"
            ).read_text(encoding="utf-8")
            self.assertNotIn("systemctl restart", cleanup_source)

        opened: list[str] = []

        def opener() -> object:
            opened.append(SESSION)
            return type("Page", (), {"url": SESSION})()

        seed = ensure_one_browser_page(tabs=[], opener=opener)
        self.assertTrue(seed["ok"])
        self.assertTrue(seed["opened"])
        self.assertFalse(seed["session_fine"])
        self.assertEqual(seed["status"], "OPENED_SEED_PAGE")
        self.assertEqual(opened, [SESSION])


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
                chat_runtime_probe={
                    "name": "chat-runtime",
                    "ok": True,
                    "evidence": "Chat load path equals zip",
                },
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


class StaleLiveTabClaimTests(unittest.TestCase):
    """Parked HITL / stuck jobs older than one hour do not keep leftover tabs."""

    def test_sixty_one_minute_hitl_job_does_not_claim_ascend_create_new_so_flush_closes_it(
        self,
    ):
        self.assertEqual(LIVE_TAB_CLAIM_MAX_AGE, timedelta(hours=1))
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = _park_hitl_job(
                db,
                f"create Ascend program at {ASCEND_CREATE_NEW}",
                age=timedelta(minutes=61),
            )
            store = JobStore(db)
            before = store.get_job(job_id)
            self.assertEqual(before["status"], JobStatus.AWAITING_HUMAN_INPUT.value)
            claims = live_tab_claims(db)
            self.assertNotIn(job_id, claims.job_ids)
            self.assertFalse(claims.claims(ASCEND_CREATE_NEW))
            pages = _pages(
                ("session", SESSION),
                ("ascend-create", ASCEND_CREATE_NEW),
            )
            result = flush_orphaned_tabs(db, pages=pages)
            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "flush")
            self.assertNotIn(job_id, result["live_job_ids"])
            self.assertFalse(pages[0].closed)
            self.assertTrue(pages[1].closed)
            self.assertIn(ASCEND_CREATE_NEW, result["closed_urls"])
            self.assertNotIn(ASCEND_CREATE_NEW, result["kept_urls"])
            after = store.get_job(job_id)
            self.assertEqual(after["status"], JobStatus.AWAITING_HUMAN_INPUT.value)
            self.assertEqual(after["id"], job_id)
            self.assertIsNone(after.get("completed_at"))
            self.assertNotEqual(after["status"], JobStatus.FAILED.value)

    def test_thirty_minute_hitl_job_still_claims_ascend_create_new_so_flush_keeps_it(
        self,
    ):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = _park_hitl_job(
                db,
                f"create Ascend program at {ASCEND_CREATE_NEW}",
                age=timedelta(minutes=30),
            )
            store = JobStore(db)
            claims = live_tab_claims(db)
            self.assertIn(job_id, claims.job_ids)
            self.assertTrue(claims.claims(ASCEND_CREATE_NEW))
            pages = _pages(
                ("session", SESSION),
                ("ascend-create", ASCEND_CREATE_NEW),
            )
            result = flush_orphaned_tabs(db, pages=pages)
            self.assertTrue(result["ok"])
            self.assertIn(job_id, result["live_job_ids"])
            self.assertFalse(pages[0].closed)
            self.assertFalse(pages[1].closed)
            self.assertIn(ASCEND_CREATE_NEW, result["kept_urls"])
            self.assertNotIn(ASCEND_CREATE_NEW, result["closed_urls"])
            after = store.get_job(job_id)
            self.assertEqual(after["status"], JobStatus.AWAITING_HUMAN_INPUT.value)

    def test_start_of_job_flush_hook_is_invoked_for_chat_and_playwright(self):
        chat_calls: list[dict] = []
        playwright_calls: list[dict] = []

        def _record(bucket: list[dict], **kwargs):
            bucket.append(dict(kwargs))
            return {"ok": True, "mode": "flush", "closed_urls": [], "kept_urls": [SESSION]}

        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            JobStore(db)
            with patch(
                "robie_job_engine.tab_cleanup.flush_tabs_at_job_start",
                side_effect=lambda **kwargs: _record(chat_calls, **kwargs),
            ):
                job_id = open_chat_job(
                    db,
                    "message-start-flush",
                    "open EZLynx documents",
                    conversation_id="spaces/start-flush",
                )
            self.assertTrue(job_id)
            self.assertGreaterEqual(len(chat_calls), 1)
            self.assertEqual(chat_calls[0].get("db_path"), db)
            stop_generic_chat_job_heartbeat(db, job_id)

        import importlib.util
        import sys
        from types import ModuleType

        tools_pkg = ModuleType("tools")
        tools_pkg.__path__ = []
        registry_mod = ModuleType("tools.registry")

        class DummyRegistry:
            def register(self, **_kwargs):
                return None

        def tool_error(message):
            return {"ok": False, "error": message}

        def tool_result(payload):
            return payload

        registry_mod.registry = DummyRegistry()
        registry_mod.tool_error = tool_error
        registry_mod.tool_result = tool_result
        previous = {name: sys.modules.get(name) for name in ("tools", "tools.registry")}
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.registry"] = registry_mod
        tool_path = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "hermes"
            / "tools"
            / "playwright_tool.py"
        )
        spec = importlib.util.spec_from_file_location(
            "robie_playwright_tool_start_flush", tool_path
        )
        module = importlib.util.module_from_spec(spec)
        try:
            assert spec is not None and spec.loader is not None
            spec.loader.exec_module(module)
            with patch(
                "robie_job_engine.tab_cleanup.flush_tabs_at_job_start",
                side_effect=lambda **kwargs: _record(playwright_calls, **kwargs),
            ):
                result = module.playwright_exec("")
            self.assertGreaterEqual(len(playwright_calls), 1)
            self.assertEqual(result.get("ok"), False)
            self.assertIn("No Playwright code provided", result.get("error") or "")
            source = tool_path.read_text(encoding="utf-8")
            self.assertIn("flush_tabs_at_job_start", source)
            self.assertIn(
                "flush_tabs_at_job_start",
                (
                    Path(__file__).resolve().parents[1]
                    / "robie_job_engine"
                    / "chat_guard.py"
                ).read_text(encoding="utf-8"),
            )
            with durable_temporary_directory() as hook_tmp:
                hook_db = str(Path(hook_tmp) / "jobs.db")
                JobStore(hook_db)
                pages = _pages(("session", SESSION), ("leftover", GOOGLE))
                flushed = flush_tabs_at_job_start(db_path=hook_db, pages=pages)
            self.assertIsNotNone(flushed)
            self.assertEqual(flushed.get("mode"), "flush")
            self.assertTrue(pages[1].closed)
            self.assertFalse(pages[0].closed)
        finally:
            for name, item in previous.items():
                if item is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = item


class WrongHostRefuseTests(unittest.TestCase):
    def test_ezlynx_job_plus_ascend_tab_is_refused(self):
        job = {
            "action_type": "hermes.google_chat_task",
            "payload": {"text": f"open EZLynx account {LIVE_ACCOUNT} documents"},
        }
        tabs = _cdp(
            ("session", SESSION),
            ("ascend-create", ASCEND_CREATE_NEW),
        )
        reason = wrong_host_refuse_reason(job=job, tabs=tabs)
        self.assertIsNotNone(reason)
        self.assertIn(WRONG_HOST_REFUSED, reason or "")
        self.assertIn("ezlynx", (reason or "").casefold())
        self.assertIn("do not attach", (reason or "").casefold())
        self.assertIn("playwright_exec", (reason or "").casefold())
        verdict = refuse_wrong_host_at_job_start(job=job, tabs=tabs)
        self.assertTrue(verdict["refused"])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["job_host"], "ezlynx")
        self.assertTrue(verdict["kept_session"])

    def test_ezlynx_job_plus_ezlynx_account_tab_is_allowed(self):
        job = {
            "action_type": "hermes.google_chat_task",
            "payload": {"text": f"open EZLynx account {LIVE_ACCOUNT} documents"},
        }
        tabs = _cdp(
            ("session", SESSION),
            ("account", LIVE_OVERVIEW),
        )
        reason = wrong_host_refuse_reason(job=job, tabs=tabs)
        self.assertIsNone(reason)
        verdict = refuse_wrong_host_at_job_start(job=job, tabs=tabs)
        self.assertFalse(verdict["refused"])
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["job_host"], "ezlynx")

    def test_chat_and_playwright_invoke_wrong_host_refuse(self):
        job = {
            "action_type": "hermes.google_chat_task",
            "payload": {"text": f"open EZLynx account {LIVE_ACCOUNT}"},
        }
        tabs = _cdp(("session", SESSION), ("ascend-create", ASCEND_CREATE_NEW))
        refused = {
            "ok": False,
            "refused": True,
            "reason": f"{WRONG_HOST_REFUSED}: fixture",
            "job_host": "ezlynx",
            "kept_session": True,
        }
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            JobStore(db)
            with patch(
                "robie_job_engine.tab_cleanup.refuse_wrong_host_at_job_start",
                return_value=refused,
            ):
                job_id = open_chat_job(
                    db,
                    "message-wrong-host",
                    f"open EZLynx account {LIVE_ACCOUNT} documents",
                    conversation_id="spaces/wrong-host",
                )
            opened = JobStore(db).get_job(job_id)
            self.assertEqual(opened["status"], JobStatus.FAILED.value)
            self.assertIn(WRONG_HOST_REFUSED, opened["last_error"])
            stop_generic_chat_job_heartbeat(db, job_id)
        self.assertIsNotNone(wrong_host_refuse_reason(job=job, tabs=tabs))
        chat_guard = (
            Path(__file__).resolve().parents[1]
            / "robie_job_engine"
            / "chat_guard.py"
        ).read_text(encoding="utf-8")
        tool = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "hermes"
            / "tools"
            / "playwright_tool.py"
        ).read_text(encoding="utf-8")
        self.assertIn("refuse_wrong_host_at_job_start", chat_guard)
        self.assertIn("refuse_wrong_host_at_job_start", tool)


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
