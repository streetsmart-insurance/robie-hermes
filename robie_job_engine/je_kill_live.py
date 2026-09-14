"""Guarded Test-only JE-KILL-01 runner for a disposable EZLynx fixture.

This module deliberately does not discover or guess a customer record.  A
human-approved fixture on ``hermes-test-01`` supplies three disposable
documents and stable Playwright locators.  The runner uses an isolated Job
database, kills a real child process, and performs authoritative fresh-page
readback before any interrupted action may be repeated.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import multiprocessing
import os
import queue
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from .engine import JobEngine
from .ezlynx import EzlynxDestinationVerifier, HermesCuaEzlynxWorker
from .idempotency import DurableWorkLedger
from .models import JobStatus, VerificationResult, WorkerResult
from .runs import IsolatedRunStore
from .store import JobStore

T = TypeVar("T")


EXPECTED_HOST = "hermes-test-01"
EXPECTED_ENV = "TEST"
ACTION = "ezlynx.apply_label"
PHASES = ("before_action", "after_action", "during_verification")
FORBIDDEN_ACCOUNT_IDS = frozenset({"221398001"})
POSITIONAL_MARKERS = (".first", ".last", ".nth", ":nth", "nth=")
GREENLET_THREAD_SWITCH = (
    "Cannot switch to a different thread — Current: <greenlet"
)
# Angular documents table is empty at domcontentloaded. Wait for the
# Carlo-approved fixture label_control (row-scoped CSS, not page-wide
# "Add label") to be uniquely present before the opening click.
LABEL_CONTROL_SETTLE_TIMEOUT_MS = 15_000
UNIQUE_LOCATOR_POLL_INTERVAL_S = 0.1


class PlaywrightThread:
    """Own every Playwright sync call on one greenlet-bound thread.

    ``JobEngine._call_worker`` uses a ``ThreadPoolExecutor``. Playwright's
    sync API is bound to the thread that opened the CDP connection. The
    JE-KILL live path therefore cannot call ``page.goto`` / locators from
    the Job Engine pool after reconcile opened CDP on the main thread.
    """

    def __init__(self) -> None:
        self._jobs: queue.Queue[
            tuple[Callable[[], Any], concurrent.futures.Future[Any]] | None
        ] = queue.Queue()
        self._ready = threading.Event()
        self.thread_id = 0
        self._thread = threading.Thread(
            target=self._loop,
            name="je-kill-playwright",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("JE-KILL Playwright thread failed to start")

    def _loop(self) -> None:
        self.thread_id = threading.get_ident()
        self._ready.set()
        while True:
            item = self._jobs.get()
            if item is None:
                return
            fn, done = item
            try:
                done.set_result(fn())
            except BaseException as exc:
                done.set_exception(exc)

    def call(self, fn: Callable[[], T]) -> T:
        if threading.get_ident() == self.thread_id:
            return fn()
        done: concurrent.futures.Future[T] = concurrent.futures.Future()
        self._jobs.put((fn, done))
        return done.result()

    def shutdown(self) -> None:
        if not self._thread.is_alive():
            return
        self._jobs.put(None)
        self._thread.join(timeout=5)


def _on_playwright_thread(method: Callable[..., T]) -> Callable[..., T]:
    def wrapper(self: PersistentChromeEzlynxPort, *args: Any, **kwargs: Any) -> T:
        return self._pw_thread.call(lambda: method(self, *args, **kwargs))

    return wrapper


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_live_test_host() -> None:
    env = str(os.environ.get("ROBIE_ENV") or "").strip().upper()
    host = socket.gethostname().split(".", 1)[0]
    if env != EXPECTED_ENV:
        raise RuntimeError(f"JE-KILL live runner requires ROBIE_ENV={EXPECTED_ENV}")
    if host != EXPECTED_HOST:
        raise RuntimeError(
            f"JE-KILL live runner refuses host {host!r}; expected {EXPECTED_HOST!r}"
        )


def _require_ezlynx_url(value: object, field: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (host == "ezlynx.com" or host.endswith(".ezlynx.com")):
        raise ValueError(f"{field} must be an HTTPS ezlynx.com URL")
    if "/web/" not in parsed.path.casefold():
        raise ValueError(f"{field} must target an authenticated EZLynx /web/ page")
    return url


def _validate_locator(raw: object, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{field} must be a locator object")
    kind = str(raw.get("kind") or "").strip()
    if kind not in {"role", "text", "label", "test_id", "css"}:
        raise ValueError(f"{field}.kind is not supported")
    value = str(raw.get("value") or "").strip()
    if not value:
        raise ValueError(f"{field}.value is required")
    folded = value.casefold()
    if kind == "css" and any(marker in folded for marker in POSITIONAL_MARKERS):
        raise ValueError(f"{field} uses a prohibited positional selector")
    result: dict[str, Any] = {"kind": kind, "value": value, "exact": True}
    if kind == "role":
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"{field}.name is required for role locators")
        result["name"] = name
    return result


@dataclass(frozen=True)
class ScenarioFixture:
    phase: str
    account_id: str
    resource_id: str
    document_name: str
    label_id: str
    label: str
    action_url: str
    readback_url: str
    label_control: dict[str, Any]
    search_input: dict[str, Any]
    label_option: dict[str, Any]
    apply_button: dict[str, Any]
    applied_label: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        return {
            "worker": "hermes-cua",
            "account_id": self.account_id,
            "resource_id": self.resource_id,
            "document_name": self.document_name,
            "label_id": self.label_id,
            "label": self.label,
            "label_control": self.label_control,
        }


@dataclass(frozen=True)
class LiveFixture:
    approved_by: str
    approved_at: str
    scenarios: dict[str, ScenarioFixture]


def load_fixture(path: str | Path) -> LiveFixture:
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("test_only") is not True or data.get("disposable") is not True:
        raise ValueError("fixture must declare test_only=true and disposable=true")
    approved_by = str(data.get("approved_by") or "").strip()
    approved_at = str(data.get("approved_at") or "").strip()
    if approved_by != "Carlo Ferrara" or data.get("approval_scope") != "JE-KILL-01":
        raise ValueError("fixture requires Carlo Ferrara approval scoped to JE-KILL-01")
    try:
        approval_time = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("fixture approved_at must be an ISO-8601 timestamp") from exc
    if approval_time.tzinfo is None:
        raise ValueError("fixture approved_at must include a timezone")
    age_seconds = (datetime.now(timezone.utc) - approval_time.astimezone(timezone.utc)).total_seconds()
    if age_seconds < -300 or age_seconds > 7 * 24 * 60 * 60:
        raise ValueError("fixture approval must be current within seven days")
    rows = data.get("scenarios")
    if not isinstance(rows, dict) or set(rows) != set(PHASES):
        raise ValueError(f"fixture scenarios must be exactly {', '.join(PHASES)}")
    scenarios: dict[str, ScenarioFixture] = {}
    resource_ids: set[str] = set()
    for phase in PHASES:
        raw = rows[phase]
        if not isinstance(raw, dict):
            raise ValueError(f"scenario {phase} must be an object")
        account_id = str(raw.get("account_id") or "").strip()
        resource_id = str(raw.get("resource_id") or "").strip()
        document_name = str(raw.get("document_name") or "").strip()
        label_id = str(raw.get("label_id") or "").strip()
        label = str(raw.get("label") or "").strip()
        if not all((account_id, resource_id, document_name, label_id, label)):
            raise ValueError(f"scenario {phase} is missing destination identity")
        if account_id in FORBIDDEN_ACCOUNT_IDS:
            raise ValueError(f"scenario {phase} uses a forbidden account")
        if label != "JE-KILL-01" or not document_name.casefold().startswith("je-kill-"):
            raise ValueError(
                f"scenario {phase} must use the JE-KILL-01 label and je-kill-* document"
            )
        if resource_id in resource_ids:
            raise ValueError("each kill phase requires a distinct disposable document")
        resource_ids.add(resource_id)
        scenarios[phase] = ScenarioFixture(
            phase=phase,
            account_id=account_id,
            resource_id=resource_id,
            document_name=document_name,
            label_id=label_id,
            label=label,
            action_url=_require_ezlynx_url(raw.get("action_url"), f"{phase}.action_url"),
            readback_url=_require_ezlynx_url(raw.get("readback_url"), f"{phase}.readback_url"),
            label_control=_validate_locator(raw.get("label_control"), f"{phase}.label_control"),
            search_input=_validate_locator(raw.get("search_input"), f"{phase}.search_input"),
            label_option=_validate_locator(raw.get("label_option"), f"{phase}.label_option"),
            apply_button=_validate_locator(raw.get("apply_button"), f"{phase}.apply_button"),
            applied_label=_validate_locator(raw.get("applied_label"), f"{phase}.applied_label"),
        )
    return LiveFixture(approved_by=approved_by, approved_at=approved_at, scenarios=scenarios)


class PersistentChromeEzlynxPort:
    """Playwright port limited to one approved disposable Test fixture."""

    def __init__(self, scenario: ScenarioFixture, *, cdp_url: str) -> None:
        self.scenario = scenario
        self.cdp_url = cdp_url
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None
        self._pw_thread = PlaywrightThread()

    def close(self) -> None:
        """Disconnect this CDP client only. Never close operator-owned Chrome."""

        def _disconnect() -> None:
            pw = self._pw
            self._pw = self._browser = self._page = None
            if pw is not None:
                # playwright.stop() drops the CDP websocket. Do not call
                # browser.close() / page.close() — a killed JE-KILL child
                # plus those APIs can take down the Test Chrome the parent
                # still needs for resume.
                pw.stop()

        try:
            self._pw_thread.call(_disconnect)
        finally:
            self._pw_thread.shutdown()

    def _start_playwright(self) -> Any:
        from playwright.sync_api import sync_playwright

        return sync_playwright().start()

    @_on_playwright_thread
    def _connect(self) -> Any:
        if self._page is not None:
            return self._page
        self._pw = self._start_playwright()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=15_000)
        pages = [page for context in self._browser.contexts for page in context.pages]
        if not pages:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: persistent Test Chrome has zero tabs")
        eligible = [page for page in pages if "ezlynx.com" in (page.url or "").casefold()]
        if len(eligible) != 1:
            raise RuntimeError(
                "PLAYWRIGHT_BLOCKED: expected exactly one EZLynx Test tab; "
                f"observed {len(eligible)}"
            )
        self._page = eligible[0]
        return self._page

    @_on_playwright_thread
    def _assert_authenticated(self) -> None:
        page = self._connect()
        url = str(page.url or "").casefold()
        password_count = page.get_by_role("textbox", name="Password", exact=True).count()
        if "login" in url or "signin" in url or password_count:
            raise RuntimeError("AUTH_CHALLENGE: EZLynx Test session is expired")

    @_on_playwright_thread
    def _locator(self, spec: dict[str, Any], *, scope: Any | None = None) -> Any:
        target = scope or self._connect()
        kind = spec["kind"]
        value = spec["value"]
        if kind == "role":
            return target.get_by_role(value, name=spec["name"], exact=True)
        if kind == "text":
            return target.get_by_text(value, exact=True)
        if kind == "label":
            return target.get_by_label(value, exact=True)
        if kind == "test_id":
            return target.get_by_test_id(value)
        return target.locator(value)

    @staticmethod
    def _blocked_count(name: str, count: int) -> RuntimeError:
        return RuntimeError(
            f"PLAYWRIGHT_BLOCKED: {name} matched {count} elements; refuse to guess"
        )

    @staticmethod
    def _require_one(locator: Any, name: str) -> Any:
        count = locator.count()
        if count != 1:
            raise PersistentChromeEzlynxPort._blocked_count(name, count)
        return locator

    @staticmethod
    def _wait_unique(locator: Any, name: str, timeout_ms: int) -> Any:
        """Poll until count==1. 0 at timeout and count>1 stay PLAYWRIGHT_BLOCKED."""
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        last_count = 0
        while True:
            last_count = locator.count()
            if last_count == 1:
                return locator
            if last_count > 1:
                raise PersistentChromeEzlynxPort._blocked_count(name, last_count)
            now = time.monotonic()
            if now >= deadline:
                raise PersistentChromeEzlynxPort._blocked_count(name, last_count)
            time.sleep(min(UNIQUE_LOCATOR_POLL_INTERVAL_S, deadline - now))

    def _wait_label_control(self) -> Any:
        return self._wait_unique(
            self._locator(self.scenario.label_control),
            "click target",
            LABEL_CONTROL_SETTLE_TIMEOUT_MS,
        )

    def preflight(self) -> None:
        """Fail closed on tab/auth before a kill cycle. Disconnect after."""
        self._connect()
        self._assert_authenticated()

    @_on_playwright_thread
    def _dismiss_blocking_overlays(self) -> None:
        """EZLynx release-notes / dark backdrops steal clicks on documents."""
        page = self._connect()
        for _ in range(4):
            closed = False
            for sel in (
                '.cdk-overlay-container button[aria-label*="lose" i]',
                '.cdk-overlay-container button:has-text("Close")',
                '.cdk-overlay-container button:has-text("Got it")',
                '.cdk-overlay-container button:has-text("OK")',
            ):
                loc = page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    try:
                        loc.first.click(timeout=1_500)
                        closed = True
                        page.wait_for_timeout(200)
                    except Exception:
                        pass
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(150)
            backdrop = page.locator(".cdk-overlay-backdrop-showing")
            if backdrop.count() == 0:
                return
            if not closed:
                try:
                    page.evaluate(
                        "() => { document.querySelectorAll('.cdk-overlay-container')"
                        ".forEach((n) => { n.innerHTML = ''; }); }"
                    )
                except Exception:
                    pass
                return

    @_on_playwright_thread
    def ensure_clean_destination(self) -> None:
        """Strip leftover fixture label so kill-before-action still attempts work.

        A prior successful JE-KILL leaves ``JE-KILL-01`` on the disposable docs.
        Restart reconcile then returns APPLIED without calling ``perform`` →
        ``action_attempts=0`` and Stage 2 fails closed. Clear via the row Edit
        control + fixture label option + Apply before spawning the kill child.
        """
        page = self._connect()
        page.goto(self.scenario.action_url, wait_until="domcontentloaded", timeout=30_000)
        self._assert_authenticated()
        self._dismiss_blocking_overlays()
        applied = self._locator(self.scenario.applied_label)
        control = self._locator(self.scenario.label_control)
        if applied.count() == 0 and control.count() == 1:
            return
        edit = self._edit_labels_locator(page)
        self._require_one(edit, "edit labels").click()
        page.wait_for_timeout(400)
        option = self._locator(self.scenario.label_option)
        self._require_one(option, "label option").click()
        apply = self._locator(self.scenario.apply_button)
        self._require_one(apply, "Apply").click()
        page.wait_for_timeout(500)
        page.reload(wait_until="domcontentloaded", timeout=30_000)
        self._assert_authenticated()
        self._dismiss_blocking_overlays()
        applied = self._locator(self.scenario.applied_label)
        if applied.count() != 0:
            raise RuntimeError(
                "JE-KILL REFUSED: disposable destination still labeled after clear; "
                f"applied_label count={applied.count()}"
            )
        self._wait_unique(
            self._locator(self.scenario.label_control),
            "click target",
            LABEL_CONTROL_SETTLE_TIMEOUT_MS,
        )

    def _edit_labels_locator(self, page: Any) -> Any:
        """Row edit control derived from fixture label_control, never invented."""
        ctrl = self.scenario.label_control
        if ctrl.get("kind") == "css":
            value = str(ctrl.get("value") or "")
            if 'button:has-text("Add label")' in value:
                return page.locator(
                    value.replace(
                        'button:has-text("Add label")',
                        'button:has-text("edit")',
                    )
                )
        return (
            page.locator("tr")
            .filter(has_text=self.scenario.document_name)
            .locator("button")
            .filter(has_text="edit")
        )

    @_on_playwright_thread
    def begin_action(self) -> None:
        page = self._connect()
        page.goto(self.scenario.action_url, wait_until="domcontentloaded", timeout=30_000)
        self._assert_authenticated()
        self._wait_label_control()

    @_on_playwright_thread
    def exact_option(self, *, stable_id: str, exact_text: str, scope: Any | None = None) -> Any:
        if stable_id != self.scenario.label_id or exact_text != self.scenario.label:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: option is outside approved Test fixture")
        locator = self._locator(self.scenario.label_option, scope=scope)
        locator.wait_for(state="visible", timeout=10_000)
        return self._require_one(locator, "label option")

    @_on_playwright_thread
    def click(self, target: Any) -> None:
        locator = self._locator(target) if isinstance(target, dict) else target
        if isinstance(target, dict) and target == self.scenario.label_control:
            self._wait_unique(
                locator, "click target", LABEL_CONTROL_SETTLE_TIMEOUT_MS
            ).click()
            return
        locator.wait_for(state="visible", timeout=15_000)
        self._require_one(locator, "click target").click()

    @_on_playwright_thread
    def wait_interactable(
        self, *, role: str, name: str, timeout_ms: int, scope: Any | None = None
    ) -> Any:
        if role == "textbox" and name == "Search Labels":
            spec = self.scenario.search_input
        elif role == "button" and name == "Apply":
            spec = self.scenario.apply_button
        else:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: control is outside approved Test fixture")
        locator = self._locator(spec, scope=scope)
        self._wait_unique(locator, name, timeout_ms)
        locator.wait_for(state="visible", timeout=timeout_ms)
        self._require_one(locator, name)
        if not locator.is_enabled():
            raise RuntimeError(f"PLAYWRIGHT_BLOCKED: {name} is disabled")
        return locator

    def wait_frame_interactable(self, **_: Any) -> Any:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: frame actions are outside this apply-label prototype")

    @_on_playwright_thread
    def fill_like_user(self, target: Any, value: str) -> None:
        if value != self.scenario.label:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: label differs from approved Test fixture")
        self._require_one(target, "label search").fill(value)

    @_on_playwright_thread
    def submit(self, *, idempotency_key: str) -> dict[str, Any]:
        page = self._connect()
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            page.wait_for_timeout(500)
        return {
            "idempotency_key": idempotency_key,
            "submitted_at": _utc_now(),
            "url": page.url,
        }

    def api_state(self, action_type: str, expected: dict[str, Any]) -> None:
        return None

    @_on_playwright_thread
    def fresh_page_state(self, action_type: str, expected: dict[str, Any]) -> dict[str, Any]:
        if action_type != ACTION or expected.get("resource_id") != self.scenario.resource_id:
            raise RuntimeError("readback destination is outside approved Test fixture")
        page = self._connect()
        page.goto(self.scenario.readback_url, wait_until="domcontentloaded", timeout=30_000)
        self._assert_authenticated()
        page.reload(wait_until="domcontentloaded", timeout=30_000)
        self._assert_authenticated()
        loc = self._locator(self.scenario.applied_label)
        deadline = time.monotonic() + max(0.0, LABEL_CONTROL_SETTLE_TIMEOUT_MS / 1000.0)
        while time.monotonic() < deadline:
            if loc.count() > 0:
                break
            label_ctrl = self._locator(self.scenario.label_control)
            if label_ctrl.count() > 0:
                break
            time.sleep(UNIQUE_LOCATOR_POLL_INTERVAL_S)
        count = loc.count()
        if count == 0:
            self._wait_label_control()
            return {}
        if count != 1:
            raise RuntimeError(
                f"authoritative applied-label readback matched {count} elements"
            )
        return dict(expected)


def _append_attempt(path: Path, phase: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"phase": phase, "at": _utc_now()}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _mark_and_block(marker: Path) -> None:
    marker.write_text("ready\n", encoding="utf-8")
    while True:
        time.sleep(0.1)


class KillBoundaryWorker:
    def __init__(self, delegate: Any, *, phase: str, marker: Path) -> None:
        self.delegate = delegate
        self.phase = phase
        self.marker = marker

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        if self.phase == "before_action":
            _mark_and_block(self.marker)
        result = self.delegate.perform(job, idempotency_key=idempotency_key)
        if self.phase == "after_action":
            _mark_and_block(self.marker)
        return result


class FixtureWorker:
    """Navigate explicitly and persist every real browser-action attempt."""

    def __init__(
        self,
        delegate: HermesCuaEzlynxWorker,
        port: PersistentChromeEzlynxPort,
        *,
        phase: str,
        attempts: Path,
    ) -> None:
        self.delegate = delegate
        self.port = port
        self.phase = phase
        self.attempts = attempts

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        _append_attempt(self.attempts, self.phase)
        self.port.begin_action()
        return self.delegate.perform(job, idempotency_key=idempotency_key)


class KillBoundaryVerifier:
    def __init__(self, delegate: EzlynxDestinationVerifier, *, phase: str, marker: Path) -> None:
        self.delegate = delegate
        self.phase = phase
        self.marker = marker

    def reconcile(self, job: dict[str, Any], *, idempotency_key: str):
        return self.delegate.reconcile(job, idempotency_key=idempotency_key)

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        result = self.delegate.verify(job, action)
        if self.phase == "during_verification":
            _mark_and_block(self.marker)
        return result


def _engine(db: Path, scenario: ScenarioFixture, *, phase: str, marker: Path, attempts: Path, cdp_url: str, kill: bool) -> tuple[JobEngine, PersistentChromeEzlynxPort]:
    port = PersistentChromeEzlynxPort(scenario, cdp_url=cdp_url)
    worker: Any = FixtureWorker(
        HermesCuaEzlynxWorker(port),
        port,
        phase=phase,
        attempts=attempts,
    )
    verifier: Any = EzlynxDestinationVerifier(port)
    if kill:
        worker = KillBoundaryWorker(worker, phase=phase, marker=marker)
        verifier = KillBoundaryVerifier(verifier, phase=phase, marker=marker)
    engine = JobEngine(
        JobStore(str(db)),
        {"hermes-cua": worker},
        {ACTION: verifier},
        reconcilers={ACTION: verifier},
        lease_seconds=2 if kill else 30,
        enforce_recording_policy=False,
        call_worker_on_calling_thread=True,
    )
    return engine, port


def _child(db: str, job_id: str, fixture_path: str, phase: str, marker: str, attempts: str, cdp_url: str) -> None:
    fixture = load_fixture(fixture_path)
    engine, port = _engine(Path(db), fixture.scenarios[phase], phase=phase, marker=Path(marker), attempts=Path(attempts), cdp_url=cdp_url, kill=True)
    try:
        engine.run(job_id)
    finally:
        # Disconnect CDP only. SIGKILL of this child skips finally; the
        # parent must still be able to attach to the same Test Chrome.
        port.close()


def _attempt_count(path: Path) -> int:
    if not path.is_file():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])


def run_phase(fixture_path: Path, fixture: LiveFixture, phase: str, run_root: Path, cdp_url: str) -> dict[str, Any]:
    scenario = fixture.scenarios[phase]
    phase_root = run_root / phase
    phase_root.mkdir(parents=True, exist_ok=False)
    db = phase_root / "jobs.db"
    marker = phase_root / "kill-ready"
    attempts = phase_root / "action-attempts.jsonl"
    store = JobStore(str(db))
    job = store.create_job(
        ACTION,
        scenario.payload(),
        idempotency_key=f"je-kill-live:{run_root.name}:{phase}",
        max_attempts=3,
    )
    preflight = PersistentChromeEzlynxPort(scenario, cdp_url=cdp_url)
    try:
        preflight.preflight()
        preflight.ensure_clean_destination()
    finally:
        preflight.close()
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_child,
        args=(str(db), job["id"], str(fixture_path), phase, str(marker), str(attempts), cdp_url),
    )
    process.start()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not marker.exists():
        if not process.is_alive():
            raise RuntimeError(f"child exited before {phase} boundary: {process.exitcode}")
        time.sleep(0.1)
    if not marker.exists():
        process.kill()
        process.join(timeout=5)
        raise RuntimeError(f"child did not reach {phase} boundary")
    process.kill()
    process.join(timeout=10)
    if process.is_alive() or process.exitcode == 0:
        raise RuntimeError(f"child was not killed at {phase} boundary")
    time.sleep(2.4)

    engine, port = _engine(db, scenario, phase=phase, marker=marker, attempts=attempts, cdp_url=cdp_url, kill=False)
    try:
        final = engine.run(job["id"])
    finally:
        port.close()
    ledger = DurableWorkLedger(str(db)).get(ACTION, job["idempotency_key"])
    runs = IsolatedRunStore(str(db)).list_runs(job["id"])
    evidence = store.list_evidence(job["id"])
    result = {
        "phase": phase,
        "status": str(final["status"]),
        "action_attempts": _attempt_count(attempts),
        "ledger_external_actions": int((ledger or {}).get("external_actions", 0)),
        "ledger_verified": int((ledger or {}).get("verified", 0)),
        "authoritative_verified_evidence": sum(
            1 for row in evidence if row.get("verified") and row.get("authoritative")
        ),
        "abandoned_run": any(row.get("terminal_event") == "ABANDONED" for row in runs),
    }
    expected = {
        "status": JobStatus.COMPLETE.value,
        "action_attempts": 1,
        "ledger_external_actions": 1,
        "ledger_verified": 1,
        "authoritative_verified_evidence": 1,
        "abandoned_run": True,
    }
    for key, value in expected.items():
        if result[key] != value:
            raise RuntimeError(f"{phase} failed {key}: expected {value!r}, got {result[key]!r}")
    return result


def run_all(fixture_path: Path, run_root: Path, cdp_url: str) -> dict[str, Any]:
    require_live_test_host()
    fixture = load_fixture(fixture_path)
    run_dir = run_root / f"je-kill-01-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    results = [run_phase(fixture_path, fixture, phase, run_dir, cdp_url) for phase in PHASES]
    evidence = {
        "test": "JE-KILL-01",
        "environment": "Test",
        "host": EXPECTED_HOST,
        "approved_by": fixture.approved_by,
        "approved_at": fixture.approved_at,
        "run_id": run_dir.name,
        "verified_at": _utc_now(),
        "production_touched": False,
        "results": results,
    }
    evidence_path = run_dir / "evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "result": "TEST VERIFIED",
        "run_id": run_dir.name,
        "evidence_path": str(evidence_path),
        "phases": [{"phase": row["phase"], "status": row["status"]} for row in results],
        "production_touched": False,
    }, sort_keys=True))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test-only JE-KILL-01 live browser runner")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument(
        "--cdp-url",
        default=os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222"),
    )
    args = parser.parse_args(argv)
    run_all(Path(args.fixture), Path(args.run_root), args.cdp_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
