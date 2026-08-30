from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.engine import JobEngine
from robie_job_engine.ezlynx import EzlynxDestinationVerifier, HermesCuaEzlynxWorker
from robie_job_engine.je_kill_live import (
    ACTION,
    GREENLET_THREAD_SWITCH,
    LABEL_CONTROL_SETTLE_TIMEOUT_MS,
    PHASES,
    FixtureWorker,
    PersistentChromeEzlynxPort,
    PlaywrightThread,
    load_fixture,
    require_live_test_host,
)
from robie_job_engine.models import JobStatus, WorkerResult
from robie_job_engine.store import JobStore


def fixture_payload() -> dict:
    scenarios = {}
    for index, phase in enumerate(PHASES, start=1):
        scenarios[phase] = {
            "account_id": "220250093",
            "resource_id": f"test-document-{index}",
            "document_name": f"je-kill-{index}.pdf",
            "label_id": "test-label",
            "label": "JE-KILL-01",
            "action_url": f"https://test.ezlynx.com/web/document/{index}",
            "readback_url": f"https://test.ezlynx.com/web/document/{index}",
            "label_control": {"kind": "role", "value": "button", "name": "Labels"},
            "search_input": {"kind": "role", "value": "textbox", "name": "Search Labels"},
            "label_option": {"kind": "text", "value": "JE-KILL-01"},
            "apply_button": {"kind": "role", "value": "button", "name": "Apply"},
            "applied_label": {"kind": "text", "value": "JE-KILL-01"},
        }
    return {
        "test_only": True,
        "disposable": True,
        "approved_by": "Carlo Ferrara",
        "approval_scope": "JE-KILL-01",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": scenarios,
    }


class JeKillLiveGuardTests(unittest.TestCase):
    def _write(self, root: Path, payload: dict) -> Path:
        path = root / "fixture.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_fixture_requires_three_distinct_disposable_approved_test_records(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            fixture = load_fixture(self._write(root, fixture_payload()))
            self.assertEqual(set(fixture.scenarios), set(PHASES))
            self.assertEqual(
                len({item.resource_id for item in fixture.scenarios.values()}), 3
            )

            payload = fixture_payload()
            payload["disposable"] = False
            with self.assertRaisesRegex(ValueError, "disposable"):
                load_fixture(self._write(root, payload))

            payload = fixture_payload()
            payload["approved_by"] = ""
            with self.assertRaisesRegex(ValueError, "Carlo Ferrara"):
                load_fixture(self._write(root, payload))

            payload = fixture_payload()
            payload["scenarios"]["after_action"]["resource_id"] = "test-document-1"
            with self.assertRaisesRegex(ValueError, "distinct"):
                load_fixture(self._write(root, payload))

    def test_fixture_refuses_known_live_account_non_ezlynx_urls_and_positional_selectors(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            payload = fixture_payload()
            payload["scenarios"]["before_action"]["account_id"] = "221398001"
            with self.assertRaisesRegex(ValueError, "forbidden account"):
                load_fixture(self._write(root, payload))

            payload = fixture_payload()
            payload["scenarios"]["before_action"]["action_url"] = "https://example.com/web/x"
            with self.assertRaisesRegex(ValueError, "ezlynx.com"):
                load_fixture(self._write(root, payload))

            payload = fixture_payload()
            payload["scenarios"]["before_action"]["applied_label"] = {
                "kind": "css",
                "value": ".label:nth-child(1)",
            }
            with self.assertRaisesRegex(ValueError, "positional"):
                load_fixture(self._write(root, payload))

    def test_host_guard_requires_test_env_and_exact_test_hostname(self):
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False), patch(
            "robie_job_engine.je_kill_live.socket.gethostname",
            return_value="hermes-test-01.c.internal",
        ):
            require_live_test_host()
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False), patch(
            "robie_job_engine.je_kill_live.socket.gethostname",
            return_value="hermes-test-01",
        ):
            with self.assertRaisesRegex(RuntimeError, "ROBIE_ENV=TEST"):
                require_live_test_host()
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False), patch(
            "robie_job_engine.je_kill_live.socket.gethostname",
            return_value="hermes-poc-01",
        ):
            with self.assertRaisesRegex(RuntimeError, "refuses host"):
                require_live_test_host()

    def test_port_fails_closed_on_expired_session_and_non_unique_readback(self):
        payload = fixture_payload()
        with durable_temporary_directory() as tmp:
            scenario = load_fixture(self._write(Path(tmp), payload)).scenarios["before_action"]

        class Locator:
            def __init__(self, count: int):
                self._count = count

            def count(self):
                return self._count

        class Page:
            url = "https://test.ezlynx.com/login"

            def get_by_role(self, *args, **kwargs):
                return Locator(1)

        port = PersistentChromeEzlynxPort(scenario, cdp_url="http://127.0.0.1:9222")
        self.addCleanup(port.close)
        port._page = Page()
        with self.assertRaisesRegex(RuntimeError, "AUTH_CHALLENGE"):
            port._assert_authenticated()
        with self.assertRaisesRegex(RuntimeError, "matched 2"):
            port._require_one(Locator(2), "applied label")

    def test_workflow_is_main_only_test_only_and_does_not_log_fixture(self):
        workflow = Path(".github/workflows/je-kill-test.yml").read_text(encoding="utf-8")
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("RUN_JE_KILL_01_ON_HERMES_TEST_01", workflow)
        self.assertIn("TEST_VM: hermes-test-01", workflow)
        self.assertIn("ROBIE_ENV=TEST", workflow)
        self.assertNotIn("cat \"${FIXTURE}\"", workflow)
        self.assertNotIn("hermes-poc-01", workflow)

    def test_workflow_transfers_runner_and_fails_closed_without_remote_sentinel(self):
        workflow = Path(".github/workflows/je-kill-test.yml").read_text(encoding="utf-8")
        self.assertIn("gcloud compute scp", workflow)
        self.assertIn("scripts/run-je-kill-test-remote.sh", workflow)
        self.assertIn("JE-KILL REMOTE VERIFIED", workflow)
        self.assertIn('cd \\"${TEST_ROOT}\\"', workflow)
        self.assertNotIn("bash -s", workflow)

        remote = Path("scripts/run-je-kill-test-remote.sh").read_text(encoding="utf-8")
        self.assertIn("JE-KILL Test inventory: 0 blocking Jobs/leases", remote)
        self.assertIn("run-je-kill-live-test.py", remote)
        self.assertIn("grep -Fq '\"result\": \"TEST VERIFIED\"'", remote)
        self.assertIn("JE-KILL REMOTE VERIFIED", remote)

    def test_remote_script_uses_test_user_venv_and_readable_cwd(self):
        remote = Path("scripts/run-je-kill-test-remote.sh").read_text(encoding="utf-8")
        self.assertIn("test_user=streetsmart-hermes-test", remote)
        self.assertIn('python_bin="${TEST_ROOT}/venv/bin/python"', remote)
        self.assertIn("/opt/streetsmart-hermes-test/venv/bin/python", remote)
        self.assertIn('cd "${TEST_ROOT}"', remote)
        self.assertIn('HOME="${TEST_ROOT}"', remote)
        self.assertIn('sudo -u "${test_user}"', remote)
        self.assertIn('install -d -o "${test_user}" -g "${test_user}"', remote)
        self.assertIn('test "$(hostname -s)" = hermes-test-01', remote)
        self.assertIn('test "${ROBIE_ENV}" = TEST', remote)
        self.assertIn("Test release does not match workflow commit", remote)
        self.assertIn("0 blocking Jobs/leases", remote)
        self.assertNotIn("/home/streetsmart-hermes/.hermes", remote)
        self.assertNotRegex(remote, r"sudo -u streetsmart-hermes[^-]")
        self.assertNotRegex(remote, r"-o streetsmart-hermes[^-]")
        self.assertNotRegex(remote, r"-g streetsmart-hermes[^-]")


class ThreadBoundPage:
    """Stands in for Playwright sync: raises the live greenlet error off-thread."""

    def __init__(self) -> None:
        self.owner = threading.get_ident()
        self.url = "https://app.ezlynx.com/web/account/220250093/documents"
        self.ops: list[tuple[str, int]] = []

    def _touch(self, name: str) -> None:
        current = threading.get_ident()
        if current != self.owner:
            raise RuntimeError(
                "Cannot switch to a different thread — Current: <greenlet 0x1> "
                "current active started main, Expected: <greenlet 0x2> "
                "suspended active started main"
            )
        self.ops.append((name, current))

    def goto(self, *args, **kwargs):
        self._touch("goto")
        if args:
            self.url = str(args[0])

    def reload(self, *args, **kwargs):
        self._touch("reload")

    def get_by_role(self, *args, **kwargs):
        self._touch("get_by_role")
        return _CountLocator(0 if kwargs.get("name") == "Password" else 1)

    def get_by_text(self, *args, **kwargs):
        self._touch("get_by_text")
        return _CountLocator(0)

    def get_by_label(self, *args, **kwargs):
        self._touch("get_by_label")
        return _CountLocator(1)

    def get_by_test_id(self, *args, **kwargs):
        self._touch("get_by_test_id")
        return _CountLocator(1)

    def locator(self, *args, **kwargs):
        self._touch("locator")
        return _CountLocator(1)

    def wait_for_load_state(self, *args, **kwargs):
        self._touch("wait_for_load_state")

    def wait_for_timeout(self, *args, **kwargs):
        self._touch("wait_for_timeout")


class _CountLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self):
        return self._count

    def click(self):
        return None

    def fill(self, value):
        return None

    def wait_for(self, **kwargs):
        return None

    def is_enabled(self):
        return True


class _FakeContext:
    def __init__(self, page) -> None:
        self.pages = [page]


class _FakeBrowser:
    def __init__(self, page) -> None:
        self.contexts = [_FakeContext(page)]
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        raise AssertionError("must not close the operator-owned Test Chrome")


class _FakeChromium:
    def __init__(self, browser) -> None:
        self._browser = browser

    def connect_over_cdp(self, *args, **kwargs):
        return self._browser


class _FakePlaywright:
    def __init__(self, browser) -> None:
        self.chromium = _FakeChromium(browser)
        self.stop_calls = 0

    def start(self):
        return self

    def stop(self):
        self.stop_calls += 1


class _ApplyLabelPage:
    """Enough of a page for HermesCuaEzlynxWorker.apply_label without EZLynx."""

    def __init__(self) -> None:
        self.url = "https://app.ezlynx.com/web/account/220250093/documents"
        self.labeled = False
        self.readback_ready = False
        self.thread_ids: list[int] = []

    def _touch(self) -> None:
        self.thread_ids.append(threading.get_ident())

    def goto(self, url, **kwargs):
        self._touch()
        self.url = url
        self.readback_ready = False

    def reload(self, **kwargs):
        self._touch()
        self.readback_ready = True

    def get_by_role(self, role, name="", exact=True):
        self._touch()
        if name == "Password":
            return _ApplyLocator(self, count=0)
        return _ApplyLocator(self, count=1, apply=(name == "Apply"))

    def get_by_text(self, value, exact=True):
        self._touch()
        if value == "JE-KILL-01" and self.readback_ready and not self.labeled:
            return _ApplyLocator(self, count=0)
        return _ApplyLocator(self, count=1, apply=True)

    def get_by_label(self, *args, **kwargs):
        self._touch()
        return _ApplyLocator(self, count=1)

    def get_by_test_id(self, *args, **kwargs):
        self._touch()
        return _ApplyLocator(self, count=1)

    def locator(self, *args, **kwargs):
        self._touch()
        return _ApplyLocator(self, count=1)

    def wait_for_load_state(self, *args, **kwargs):
        self._touch()

    def wait_for_timeout(self, *args, **kwargs):
        self._touch()


class _ApplyLocator:
    def __init__(self, page: _ApplyLabelPage, *, count: int, apply: bool = False) -> None:
        self.page = page
        self._count = count
        self._apply = apply

    def count(self):
        self.page._touch()
        if self._apply and self.page.labeled:
            return 1
        return self._count

    def click(self):
        self.page._touch()
        if self._apply:
            self.page.labeled = True

    def fill(self, value):
        self.page._touch()

    def wait_for(self, **kwargs):
        self.page._touch()

    def is_enabled(self):
        self.page._touch()
        return True


class JeKillPlaywrightAffinityTests(unittest.TestCase):
    def tearDown(self) -> None:
        for port in getattr(self, "_ports", []):
            try:
                port.close()
            except Exception:
                pass

    def _scenario(self):
        with durable_temporary_directory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(json.dumps(fixture_payload()), encoding="utf-8")
            return load_fixture(path).scenarios["before_action"]

    def _port(self, scenario, page) -> PersistentChromeEzlynxPort:
        browser = _FakeBrowser(page)
        playwright = _FakePlaywright(browser)
        port = PersistentChromeEzlynxPort(scenario, cdp_url="http://127.0.0.1:9222")
        self._ports = getattr(self, "_ports", [])
        self._ports.append(port)
        port._start_playwright = lambda: playwright  # type: ignore[method-assign]
        port._fake_browser = browser
        port._fake_pw = playwright
        return port

    def test_old_shared_page_across_threads_raises_greenlet_error(self):
        page = ThreadBoundPage()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaisesRegex(RuntimeError, re.escape(GREENLET_THREAD_SWITCH)):
                pool.submit(page.goto, "https://app.ezlynx.com/web/account/1").result()
        self.assertEqual(page.ops, [])

    def test_port_playwright_ops_stay_on_owner_thread_from_jobengine_pool(self):
        scenario = self._scenario()
        page = ThreadBoundPage()
        # Bind the fake page to the port's Playwright thread, matching a
        # real connect_over_cdp that happens on that thread.
        port = PersistentChromeEzlynxPort(scenario, cdp_url="http://127.0.0.1:9222")
        self._ports = [port]

        def start_on_pw_thread():
            page.owner = threading.get_ident()
            return _FakePlaywright(_FakeBrowser(page))

        port._start_playwright = start_on_pw_thread  # type: ignore[method-assign]

        port.fresh_page_state(ACTION, scenario.payload())
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(port.begin_action).result(timeout=5)
        self.assertTrue(page.ops)
        self.assertEqual(len({thread for _name, thread in page.ops}), 1)
        self.assertEqual(page.ops[0][1], port._pw_thread.thread_id)
        self.assertNotEqual(page.ops[0][1], threading.get_ident())

    def test_call_worker_on_calling_thread_runs_perform_here(self):
        seen: dict[str, int] = {}

        class Worker:
            def perform(self, job, *, idempotency_key):
                seen["thread"] = threading.get_ident()
                return WorkerResult(True, ACTION, {"resource_id": "x"})

        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            engine = JobEngine(
                store,
                {"w": Worker()},
                {},
                call_worker_on_calling_thread=True,
            )
            result = engine._call_worker(
                Worker(),
                {"payload": {}, "idempotency_key": "k"},
            )
        self.assertEqual(seen["thread"], threading.get_ident())
        self.assertTrue(result.succeeded)

    def test_default_call_worker_still_uses_pool_for_timeout(self):
        seen: dict[str, int] = {}

        class Worker:
            def perform(self, job, *, idempotency_key):
                seen["thread"] = threading.get_ident()
                return WorkerResult(True, ACTION, {"resource_id": "x"})

        with durable_temporary_directory() as tmp:
            store = JobStore(str(Path(tmp) / "jobs.db"))
            engine = JobEngine(store, {"w": Worker()}, {})
            engine._call_worker(Worker(), {"payload": {}, "idempotency_key": "k"})
        self.assertNotEqual(seen["thread"], threading.get_ident())

    def test_close_disconnects_cdp_and_does_not_close_chrome(self):
        scenario = self._scenario()
        page = ThreadBoundPage()
        port = self._port(scenario, page)
        port._connect()
        port.close()
        self._ports.remove(port)
        self.assertEqual(port._fake_pw.stop_calls, 1)
        self.assertEqual(port._fake_browser.close_calls, 0)
        self.assertIsNone(port._pw)
        self.assertIsNone(port._page)

        parent = self._port(scenario, ThreadBoundPage())
        parent._connect()
        self.assertIsNotNone(parent._page)

    def test_child_and_parent_close_are_cdp_disconnect_only(self):
        source = Path("robie_job_engine/je_kill_live.py").read_text(encoding="utf-8")
        self.assertIn("Disconnect this CDP client only", source)
        self.assertIn("pw.stop()", source)
        self.assertNotIn("self._browser.close()", source)
        self.assertNotIn("self._page.close()", source)
        self.assertNotIn("Target.closeTarget", source)
        self.assertIn("call_worker_on_calling_thread=True", source)

    def test_reconcile_then_perform_completes_without_thread_switch(self):
        scenario = self._scenario()
        page = _ApplyLabelPage()
        port = PersistentChromeEzlynxPort(scenario, cdp_url="http://127.0.0.1:9222")
        self._ports = [port]

        def start_on_pw_thread():
            return _FakePlaywright(_FakeBrowser(page))

        port._start_playwright = start_on_pw_thread  # type: ignore[method-assign]
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            attempts = Path(tmp) / "action-attempts.jsonl"
            worker = FixtureWorker(
                HermesCuaEzlynxWorker(port),
                port,
                phase="before_action",
                attempts=attempts,
            )
            verifier = EzlynxDestinationVerifier(port)
            store = JobStore(db)
            job = store.create_job(
                ACTION,
                scenario.payload(),
                idempotency_key="je-kill-affinity",
                max_attempts=3,
            )
            store.checkpoint(
                job["id"],
                "action_intent",
                {"action": ACTION, "state": "PREPARED", "run_id": "dead-child"},
            )
            final = JobEngine(
                store,
                {"hermes-cua": worker},
                {ACTION: verifier},
                reconcilers={ACTION: verifier},
                lease_seconds=2,
                enforce_recording_policy=False,
                call_worker_on_calling_thread=True,
            ).run(job["id"])
            exemption = store.get_checkpoint(job["id"], "recording_exemption")
        self.assertEqual(final["status"], JobStatus.COMPLETE)
        self.assertTrue(page.thread_ids)
        self.assertEqual(len(set(page.thread_ids)), 1)
        self.assertEqual(page.thread_ids[0], port._pw_thread.thread_id)
        self.assertIn("recording not enforced", exemption["reason"])
        self.assertIn("ezlynx.apply_label", exemption["reason"])
        self.assertNotIn("not registered as an executable Skill", exemption["reason"])

    def test_playwright_thread_serializes_calls(self):
        owner = PlaywrightThread()
        seen: list[int] = []

        def mark():
            seen.append(threading.get_ident())
            return owner.thread_id

        try:
            first = owner.call(mark)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                second = pool.submit(owner.call, mark).result(timeout=5)
            self.assertEqual(first, owner.thread_id)
            self.assertEqual(second, owner.thread_id)
            self.assertEqual(seen, [owner.thread_id, owner.thread_id])
        finally:
            owner.shutdown()


class _DelayedLabelControlPage:
    """Fake documents table: label_control is 0 until appear_after_s after goto/reload."""

    def __init__(
        self,
        *,
        appear_after_s: float | None,
        count_when_ready: int = 1,
    ) -> None:
        self.url = "https://app.ezlynx.com/web/account/test-account/documents"
        self.appear_after_s = appear_after_s
        self.count_when_ready = count_when_ready
        self.nav_at: float | None = None
        self.clicks = 0
        self.locator_queries: list[tuple[str, str]] = []

    def _mark_nav(self) -> None:
        self.nav_at = time.monotonic()

    def goto(self, url, **kwargs):
        self.url = url
        self._mark_nav()

    def reload(self, **kwargs):
        self._mark_nav()

    def _control_locator(self):
        return _DelayedCountLocator(self)

    def get_by_role(self, role, name="", exact=True):
        self.locator_queries.append(("role", str(name)))
        if name == "Password":
            return _CountLocator(0)
        if name == "Labels":
            return self._control_locator()
        return _CountLocator(1)

    def get_by_text(self, value, exact=True):
        self.locator_queries.append(("text", str(value)))
        return _CountLocator(0)

    def get_by_label(self, *args, **kwargs):
        return _CountLocator(1)

    def get_by_test_id(self, *args, **kwargs):
        return _CountLocator(1)

    def locator(self, selector):
        self.locator_queries.append(("css", str(selector)))
        return self._control_locator()

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, *args, **kwargs):
        return None


class _DelayedCountLocator:
    def __init__(self, page: _DelayedLabelControlPage) -> None:
        self.page = page

    def count(self):
        if self.page.nav_at is None or self.page.appear_after_s is None:
            return 0
        if time.monotonic() - self.page.nav_at < self.page.appear_after_s:
            return 0
        return self.page.count_when_ready

    def click(self):
        if self.count() != 1:
            raise AssertionError("clicked before label_control was uniquely present")
        self.page.clicks += 1

    def fill(self, value):
        return None

    def wait_for(self, **kwargs):
        return None

    def is_enabled(self):
        return True


class JeKillLabelControlSettleTests(unittest.TestCase):
    """Post-navigation race: wait for unique fixture label_control, never guess."""

    def tearDown(self) -> None:
        for port in getattr(self, "_ports", []):
            try:
                port.close()
            except Exception:
                pass

    def _scenario(self, payload: dict | None = None):
        with durable_temporary_directory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(json.dumps(payload or fixture_payload()), encoding="utf-8")
            return load_fixture(path).scenarios["before_action"]

    def _port(self, scenario, page) -> PersistentChromeEzlynxPort:
        port = PersistentChromeEzlynxPort(scenario, cdp_url="http://127.0.0.1:9222")
        self._ports = getattr(self, "_ports", [])
        self._ports.append(port)
        port._start_playwright = lambda: _FakePlaywright(_FakeBrowser(page))  # type: ignore[method-assign]
        return port

    def test_begin_action_waits_until_label_control_count_is_one_then_click_succeeds(self):
        scenario = self._scenario()
        page = _DelayedLabelControlPage(appear_after_s=0.15)
        port = self._port(scenario, page)
        port.begin_action()
        port.click(scenario.label_control)
        self.assertEqual(page.clicks, 1)
        self.assertIn(("role", "Labels"), page.locator_queries)
        self.assertNotIn(("role", "Add label"), page.locator_queries)

    def test_begin_action_stays_blocked_when_label_control_stays_zero(self):
        scenario = self._scenario()
        page = _DelayedLabelControlPage(appear_after_s=None)
        port = self._port(scenario, page)
        with patch(
            "robie_job_engine.je_kill_live.LABEL_CONTROL_SETTLE_TIMEOUT_MS",
            250,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"PLAYWRIGHT_BLOCKED: click target matched 0 elements; refuse to guess",
            ):
                port.begin_action()
        self.assertEqual(page.clicks, 0)

    def test_begin_action_refuses_non_unique_label_control(self):
        scenario = self._scenario()
        page = _DelayedLabelControlPage(appear_after_s=0.0, count_when_ready=8)
        port = self._port(scenario, page)
        started = time.monotonic()
        with self.assertRaisesRegex(
            RuntimeError,
            r"PLAYWRIGHT_BLOCKED: click target matched 8 elements; refuse to guess",
        ):
            port.begin_action()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(page.clicks, 0)

    def test_fresh_page_state_waits_for_unique_label_control_after_reload(self):
        scenario = self._scenario()
        page = _DelayedLabelControlPage(appear_after_s=0.15)
        port = self._port(scenario, page)
        observed = port.fresh_page_state(ACTION, scenario.payload())
        self.assertEqual(observed, {})
        self.assertIn(("role", "Labels"), page.locator_queries)
        self.assertNotIn(("role", "Add label"), page.locator_queries)

    def test_fresh_page_state_stays_blocked_when_label_control_stays_zero(self):
        scenario = self._scenario()
        page = _DelayedLabelControlPage(appear_after_s=None)
        port = self._port(scenario, page)
        with patch(
            "robie_job_engine.je_kill_live.LABEL_CONTROL_SETTLE_TIMEOUT_MS",
            250,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"PLAYWRIGHT_BLOCKED: click target matched 0 elements; refuse to guess",
            ):
                port.fresh_page_state(ACTION, scenario.payload())

    def test_css_fixture_label_control_is_used_not_page_wide_add_label_role(self):
        row_css = (
            'tr:has(#document-checkbox-test-1-input) button:has-text("Add label")'
        )
        payload = fixture_payload()
        payload["scenarios"]["before_action"]["label_control"] = {
            "kind": "css",
            "value": row_css,
        }
        scenario = self._scenario(payload)
        page = _DelayedLabelControlPage(appear_after_s=0.15)
        port = self._port(scenario, page)
        port.begin_action()
        port.click(scenario.label_control)
        self.assertEqual(page.clicks, 1)
        self.assertIn(("css", row_css), page.locator_queries)
        self.assertNotIn(("role", "Add label"), page.locator_queries)

    def test_runner_does_not_invent_page_wide_add_label_role_click(self):
        source = Path("robie_job_engine/je_kill_live.py").read_text(encoding="utf-8")
        self.assertIn("LABEL_CONTROL_SETTLE_TIMEOUT_MS", source)
        self.assertIn("_wait_unique", source)
        self.assertIn("_wait_label_control", source)
        self.assertEqual(LABEL_CONTROL_SETTLE_TIMEOUT_MS, 15_000)
        self.assertNotIn('get_by_role("button", name="Add label"', source)
        self.assertNotIn("get_by_role('button', name='Add label'", source)
        self.assertNotIn('name="Add label"', source)
        self.assertNotIn("document-checkbox-", source)
        self.assertNotIn("except Exception:\n            pass", source)


if __name__ == "__main__":
    unittest.main()
