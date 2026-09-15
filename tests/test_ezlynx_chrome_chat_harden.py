"""Harden the always-on EZLynx Chrome + Chat Playwright path.

Covers the Gold Eagle Bond afternoon class (d5fd2307 / e51a2f2f):
flush must not kill the last CDP target; playwright_exec waits for
``/json/version``; stealth is applied after connect_over_cdp; infra
failures are not closed as UNVERIFIED "no structured destination
action checkpoint". COMPLETE stays blocked without destination evidence.

No live Chrome. No hermes-poc-01 / hermes-test-01. No bind. No client email.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_guard import guard_chat_response, open_chat_job
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore
from robie_job_engine.tab_cleanup import (
    BrowserTab,
    apply_cleanup,
    flush_tabs_at_job_start,
    plan_tab_cleanup,
)
from robie_job_engine.worker_contract import (
    classify_chat_close_without_checkpoint,
    claims_unverified_destination_progress,
)


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy" / "hermes" / "tools" / "playwright_tool.py"
BROWSER_UNIT = ROOT / "deploy" / "systemd" / "robie-ezlynx-browser.service"
BROWSER_TEST_UNIT = ROOT / "deploy" / "systemd" / "robie-ezlynx-browser-test.service"
CHROME_REFRESH_TIMER = ROOT / "deploy" / "systemd" / "robie-chrome-refresh.timer"
CHROME_REFRESH_SERVICE = ROOT / "deploy" / "systemd" / "robie-chrome-refresh.service"
TAB_CLEANUP = ROOT / "robie_job_engine" / "tab_cleanup.py"

SESSION = "https://app.ezlynx.com/web/"
LEFTOVER = "https://dashboard.useascend.com/create/new"
CDP_REFUSED = (
    "Could not attach to persistent Chrome: "
    "[Errno 111] ECONNREFUSED Connection refused (127.0.0.1:9222)"
)
HTTP_429 = "Vertex quota exceeded: HTTP 429 Too Many Requests"
I_DID_IT = "I did it. The job is complete. Completed successfully — COMPLETE."


class FakePage:
    def __init__(self, url: str, identity: str) -> None:
        self.url = url
        self._guid = identity
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _cdp(*pairs: tuple[str, str]) -> list[BrowserTab]:
    return [BrowserTab(identity=identity, url=url) for identity, url in pairs]


def _install_hermes_registry_stub() -> dict[str, ModuleType | None]:
    tools_pkg = ModuleType("tools")
    tools_pkg.__path__ = []
    registry_mod = ModuleType("tools.registry")

    class DummyRegistry:
        def register(self, **_kwargs):
            return None

    registry_mod.registry = DummyRegistry()
    registry_mod.tool_error = lambda message: {"ok": False, "error": message}
    registry_mod.tool_result = lambda payload: payload
    previous = {name: sys.modules.get(name) for name in ("tools", "tools.registry")}
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = registry_mod
    return previous


def _restore_modules(previous: dict[str, ModuleType | None]) -> None:
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _load_playwright_tool():
    previous = _install_hermes_registry_stub()
    spec = importlib.util.spec_from_file_location(
        "robie_playwright_tool_chrome_harden", TOOL
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    try:
        spec.loader.exec_module(module)
    except Exception:
        _restore_modules(previous)
        raise
    return module, previous


class FlushNeverKillsBrowserTests(unittest.TestCase):
    def test_flush_plan_keeps_the_last_browser_target(self):
        leftover = _cdp(("only", LEFTOVER))
        plan = plan_tab_cleanup(leftover, mode="flush")
        self.assertEqual([tab.identity for tab in plan.close], [])
        self.assertEqual([tab.identity for tab in plan.keep], ["only"])
        self.assertIsNotNone(plan.session_tab)
        self.assertEqual(plan.session_tab.identity, "only")

    def test_flush_plan_keeps_session_and_still_never_empties_chrome(self):
        tabs = _cdp(("session", SESSION), ("leftover", LEFTOVER))
        plan = plan_tab_cleanup(tabs, mode="flush")
        self.assertEqual([tab.url for tab in plan.close], [LEFTOVER])
        self.assertEqual([tab.url for tab in plan.keep], [SESSION])

    def test_apply_cleanup_refuses_to_close_the_last_page(self):
        tab = BrowserTab(identity="last", url=LEFTOVER)
        from robie_job_engine.tab_cleanup import CleanupPlan

        plan = CleanupPlan(close=[tab], keep=[], session_tab=None, reason="hostile")
        closed: list[str] = []
        result = apply_cleanup(plan, closer=lambda item: closed.append(item.identity))
        self.assertEqual(closed, [])
        self.assertIn(LEFTOVER, result["kept_urls"])
        self.assertNotIn(LEFTOVER, result["closed_urls"])
        self.assertTrue(result.get("preserved_last_target"))

    def test_job_start_flush_does_not_close_sole_page_or_restart_chrome(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            JobStore(db)
            page = FakePage(LEFTOVER, "only")
            flushed = flush_tabs_at_job_start(db_path=db, pages=[page])
        self.assertIsNotNone(flushed)
        self.assertFalse(page.closed)
        self.assertIn(LEFTOVER, flushed.get("kept_urls") or [])
        source = TAB_CLEANUP.read_text(encoding="utf-8")
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn("systemctl restart", source)
        self.assertIn("never close the last browser target", source)
        self.assertIn("systemctl against", source)


class CdpWaitAndStealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool, cls._registry_modules = _load_playwright_tool()

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._registry_modules)

    def test_wait_for_cdp_json_version_retries_then_blocks(self):
        calls: list[str] = []
        sleeps: list[float] = []

        def http_get(url: str):
            calls.append(url)
            if len(calls) < 3:
                raise ConnectionRefusedError("ECONNREFUSED 127.0.0.1:9222")
            body = json.dumps(
                {
                    "Browser": "Chrome/128.0.0.0",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x",
                }
            ).encode("utf-8")
            return 200, body

        result = self.tool.wait_for_cdp_json_version(
            "http://127.0.0.1:9222",
            http_get=http_get,
            attempts=5,
            delay_s=0.01,
            sleeper=sleeps.append,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.01, 0.01])
        self.assertTrue(all(item.endswith("/json/version") for item in calls))

        def always_down(_url: str):
            raise ConnectionRefusedError("ECONNREFUSED 127.0.0.1:9222")

        with self.assertRaisesRegex(RuntimeError, "PLAYWRIGHT_BLOCKED") as raised:
            self.tool.wait_for_cdp_json_version(
                "http://127.0.0.1:9222",
                http_get=always_down,
                attempts=3,
                delay_s=0.01,
                sleeper=lambda _delay: None,
            )
        self.assertIn("json/version", str(raised.exception))
        self.assertIn("ECONNREFUSED", str(raised.exception))

    def test_wrapper_waits_for_cdp_then_applies_stealth_before_user_code(self):
        wrapper = self.tool._playwright_exec_wrapper()
        source = TOOL.read_text(encoding="utf-8")
        self.assertIn("wait_for_cdp_json_version(cdp_url)", wrapper)
        self.assertIn("/json/version", wrapper)
        self.assertLess(
            wrapper.index("wait_for_cdp_json_version(cdp_url)"),
            wrapper.index("connect_over_cdp"),
        )
        self.assertIn("apply_playwright_stealth(browser)", wrapper)
        self.assertLess(
            wrapper.index("connect_over_cdp"),
            wrapper.index("apply_playwright_stealth(browser)"),
        )
        self.assertLess(
            wrapper.index("apply_playwright_stealth(browser)"),
            wrapper.index("exec(compile(source"),
        )
        self.assertNotIn("browserbase", wrapper.casefold())
        self.assertNotIn("advancedStealth", wrapper)
        self.assertIn("playwright-stealth", source)
        self.assertIn("wait_for_cdp_json_version", source)

    def test_stealth_is_applied_to_contexts_and_pages_and_fails_soft(self):
        applied: list[object] = []

        def stealth_sync(target):
            applied.append(target)

        page = SimpleNamespace(url=SESSION)
        context = SimpleNamespace(pages=[page], on=lambda *_a, **_k: None)
        browser = SimpleNamespace(contexts=[context])
        with patch.dict(sys.modules, {"playwright_stealth": SimpleNamespace(stealth_sync=stealth_sync)}):
            result = self.tool.apply_playwright_stealth(browser)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["applied"], 2)
        self.assertIn(context, applied)
        self.assertIn(page, applied)

        logs: list[str] = []
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "playwright_stealth" or str(name).startswith("playwright_stealth"):
                raise ImportError("No module named 'playwright_stealth'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=blocked):
            missing = self.tool.apply_playwright_stealth(browser, log=logs.append)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["applied"], 0)
        self.assertTrue(logs)
        self.assertIn("playwright-stealth", logs[0].casefold())
        self.assertNotIn("browserbase", "".join(logs).casefold())


class UnverifiedUnmaskTests(unittest.TestCase):
    def test_classifier_unmasks_infra_and_keeps_claimed_progress_unverified(self):
        infra = classify_chat_close_without_checkpoint(CDP_REFUSED, action=None)
        self.assertEqual(infra.status, "FAILED")
        self.assertIn("ECONNREFUSED", infra.error)
        self.assertNotIn("no structured destination action checkpoint", infra.error)

        quota = classify_chat_close_without_checkpoint(HTTP_429, action=None)
        self.assertEqual(quota.status, "FAILED")
        self.assertIn("429", quota.error)

        blocked = classify_chat_close_without_checkpoint(
            "PLAYWRIGHT_BLOCKED: unique-write could not name the garaging field",
            action=None,
        )
        self.assertEqual(blocked.status, "AWAITING_HUMAN_INPUT")
        self.assertIn("PLAYWRIGHT_BLOCKED", blocked.error)

        # A wait expiring is an infra failure (FAILED), not a security guard refusal (AWAITING_HUMAN_INPUT)
        timed_out = classify_chat_close_without_checkpoint(
            "PLAYWRIGHT_TIMEOUT: Timeout 30000ms exceeded.\nwaiting for load state \"networkidle\"",
            action=None,
        )
        self.assertEqual(timed_out.status, "FAILED")
        self.assertIn("PLAYWRIGHT_TIMEOUT", timed_out.error)
        self.assertEqual(timed_out.reason, "infra fail without destination claim")

        claimed = classify_chat_close_without_checkpoint(I_DID_IT, action=None)
        self.assertTrue(claims_unverified_destination_progress(I_DID_IT))
        self.assertEqual(claimed.status, "UNVERIFIED")
        self.assertIn("no structured destination action checkpoint", claimed.error)

        # A timeout alongside a progress claim must classify as UNVERIFIED, not FAILED,
        # so destination verifier can check whether the write landed.
        timeout_with_save = classify_chat_close_without_checkpoint(
            "I saved the policy in EZLynx.\nPLAYWRIGHT_TIMEOUT: Timeout 30000ms exceeded waiting for networkidle",
            action=None,
        )
        self.assertTrue(
            claims_unverified_destination_progress(
                "I saved the policy in EZLynx.\nPLAYWRIGHT_TIMEOUT: Timeout 30000ms exceeded waiting for networkidle"
            )
        )
        self.assertEqual(timeout_with_save.status, "UNVERIFIED")
        self.assertEqual(timeout_with_save.reason, "claimed destination progress without evidence")

        timeout_with_create = classify_chat_close_without_checkpoint(
            "I created the policy.\nPLAYWRIGHT_TIMEOUT: execution exceeded 90 seconds",
            action=None,
        )
        self.assertTrue(
            claims_unverified_destination_progress(
                "I created the policy.\nPLAYWRIGHT_TIMEOUT: execution exceeded 90 seconds"
            )
        )
        self.assertEqual(timeout_with_create.status, "UNVERIFIED")
        self.assertEqual(timeout_with_create.reason, "claimed destination progress without evidence")

        done = classify_chat_close_without_checkpoint("Done", action=None)
        self.assertEqual(done.status, "UNVERIFIED")

        action_only = classify_chat_close_without_checkpoint(
            "Done",
            action={"action": "ezlynx.edit"},
            verifier_missing=True,
            action_type="hermes.google_chat_task",
        )
        self.assertEqual(action_only.status, "UNVERIFIED")
        self.assertIn("no independent verifier registered", action_only.error)

    def test_never_claimed_success_is_not_unverified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "spaces/bond/messages/d5fd2307",
                "Gold Eagle Bond Chat quote",
                conversation_id="spaces/bond",
            )
            response = guard_chat_response(db, job_id, CDP_REFUSED)
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["status"], JobStatus.FAILED.value)
            self.assertNotEqual(job["status"], JobStatus.UNVERIFIED.value)
            self.assertNotEqual(job["status"], JobStatus.COMPLETE.value)
            self.assertIn("FAILED", response)
            self.assertIn("ECONNREFUSED", job["last_error"])
            self.assertNotIn("no structured destination action checkpoint", job["last_error"])
            self.assertIsNone(JobStore(db).get_checkpoint(job_id, "action"))
            self.assertEqual(JobStore(db).list_evidence(job_id), [])

    def test_claimed_progress_without_evidence_stays_unverified(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(
                db,
                "spaces/bond/messages/claimed",
                "Gold Eagle Bond Chat quote",
                conversation_id="spaces/bond-claimed",
            )
            response = guard_chat_response(db, job_id, I_DID_IT)
            job = JobStore(db).get_job(job_id)
            self.assertEqual(job["status"], JobStatus.UNVERIFIED.value)
            self.assertNotEqual(job["status"], JobStatus.COMPLETE.value)
            self.assertIn("UNVERIFIED", response)
            self.assertIn("no structured destination action checkpoint", job["last_error"])
            self.assertNotIn("I did it", response)

    def test_complete_stays_blocked_without_destination_evidence(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            job_id = open_chat_job(db, "spaces/bond/messages/no-complete", "Gold Eagle Bond")
            guard_chat_response(db, job_id, I_DID_IT)
            job = JobStore(db).get_job(job_id)
            self.assertNotEqual(job["status"], JobStatus.COMPLETE.value)
            self.assertEqual(JobStore(db).list_evidence(job_id), [])
            self.assertIsNone(JobStore(db).get_checkpoint(job_id, "action"))


class SystemdRestartLimitTests(unittest.TestCase):
    def test_ezlynx_browser_unit_rate_limits_restarts_and_chrome_refresh_is_retired_in_tree(self):
        for path in (BROWSER_UNIT, BROWSER_TEST_UNIT):
            text = path.read_text(encoding="utf-8")
            self.assertIn("StartLimitBurst=", text)
            self.assertIn("StartLimitIntervalSec=", text)
            self.assertIn("Restart=always", text)
            self.assertIn("3:30", text)
            self.assertIn("robie-chrome-refresh", text)
            self.assertIn("intentional restart", text.casefold())
        refresh = CHROME_REFRESH_TIMER.read_text(encoding="utf-8")
        self.assertIn("03:30:00 America/New_York", refresh)
        self.assertIn("RETIRED. Do not enable.", refresh)
        self.assertIn("RETIRED. Do not enable.", CHROME_REFRESH_SERVICE.read_text(encoding="utf-8"))
        root_unit = (ROOT / "robie-ezlynx-browser.service").read_text(encoding="utf-8")
        self.assertIn("StartLimitBurst=", root_unit)


if __name__ == "__main__":
    unittest.main()
