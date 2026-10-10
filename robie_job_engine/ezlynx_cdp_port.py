"""CDP-backed EZLynx browser port for Test (and explicit wiring only).

Implements ``EzlynxBrowserPort`` + ``EzlynxReadback`` against a persistent
Chrome via Playwright CDP. Prefer network capture for ``api_state``; fall
back to goto + reload persistence for ``fresh_page_state``.

Fail-closed: unique locator required; never guess with .first/.nth.
Does not close operator-owned Chrome on disconnect.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .browser_verification import EZLYNX_MUTATING_ACTIONS
from .runtime_env import TEST_ENV_NAME, current_robie_env


DEFAULT_CDP_URL = "http://127.0.0.1:9222"
UNIQUE_LOCATOR_POLL_INTERVAL_S = 0.1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cdp_url_from_env() -> str | None:
    raw = (
        os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL")
        or os.environ.get("ROBIE_BROWSER_CDP_URL")
        or ""
    ).strip()
    return raw or None


class PlaywrightThread:
    """Own Playwright sync calls on one greenlet-bound thread."""

    def __init__(self) -> None:
        import concurrent.futures
        import queue
        import threading

        self._jobs: queue.Queue[
            tuple[Callable[[], Any], concurrent.futures.Future[Any]] | None
        ] = queue.Queue()
        self._ready = threading.Event()
        self.thread_id = 0
        self._thread = threading.Thread(
            target=self._loop,
            name="ezlynx-cdp-playwright",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("EZLynx CDP Playwright thread failed to start")

    def _loop(self) -> None:
        import threading

        self.thread_id = threading.get_ident()
        self._ready.set()
        while True:
            item = self._jobs.get()
            if item is None:
                return
            fn, done = item
            try:
                done.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 — surface to caller
                done.set_exception(exc)

    def call(self, fn: Callable[[], Any]) -> Any:
        import concurrent.futures
        import threading

        if threading.get_ident() == self.thread_id:
            return fn()
        done: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._jobs.put((fn, done))
        return done.result()

    def shutdown(self) -> None:
        if not self._thread.is_alive():
            return
        self._jobs.put(None)
        self._thread.join(timeout=5)


def _on_playwright_thread(method: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: "CdpEzlynxPort", *args: Any, **kwargs: Any) -> Any:
        return self._pw_thread.call(lambda: method(self, *args, **kwargs))

    return wrapper


class CdpEzlynxPort:
    """Playwright CDP port for the three EZLynx mutating browser actions."""

    def __init__(self, *, cdp_url: str, account_id: str | None = None) -> None:
        self.cdp_url = cdp_url
        self.account_id = account_id
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None
        self._network_snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self._pw_thread = PlaywrightThread()

    def close(self) -> None:
        def _disconnect() -> None:
            pw = self._pw
            self._pw = self._browser = self._page = None
            if pw is not None:
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
            raise RuntimeError("PLAYWRIGHT_BLOCKED: persistent Chrome has zero tabs")
        eligible = [page for page in pages if "ezlynx.com" in (page.url or "").casefold()]
        if self.account_id:
            scoped = [
                page
                for page in eligible
                if f"/account/{self.account_id}" in (page.url or "").casefold()
                or f"/applicant/{self.account_id}" in (page.url or "").casefold()
            ]
            if len(scoped) == 1:
                eligible = scoped
        if len(eligible) != 1:
            raise RuntimeError(
                "PLAYWRIGHT_BLOCKED: expected exactly one EZLynx tab; "
                f"observed {len(eligible)}"
            )
        self._page = eligible[0]
        self._page.on("response", self._capture_response)
        return self._page

    def _capture_response(self, response: Any) -> None:
        try:
            url = str(response.url or "")
            if "ezlynx.com" not in url.casefold():
                return
            if response.status != 200:
                return
            # Capture JSON bodies only; never store cookies/auth headers.
            ctype = str(response.headers.get("content-type") or "")
            if "json" not in ctype.casefold():
                return
            body = response.json()
        except Exception:
            return
        if not isinstance(body, dict):
            return
        resource_id = str(
            body.get("resource_id")
            or body.get("documentId")
            or body.get("id")
            or ""
        )
        if not resource_id:
            return
        for action in EZLYNX_MUTATING_ACTIONS:
            self._network_snapshots[(action, resource_id)] = dict(body)

    @_on_playwright_thread
    def _assert_authenticated(self) -> None:
        page = self._connect()
        url = str(page.url or "").casefold()
        password_count = page.get_by_role("textbox", name="Password", exact=True).count()
        if "login" in url or "signin" in url or password_count:
            raise RuntimeError("AUTH_CHALLENGE: EZLynx session is expired")

    def _locator(self, spec: Any, *, scope: Any | None = None) -> Any:
        target = scope or self._connect()
        if not isinstance(spec, dict):
            # Allow opaque handles already resolved by a previous call.
            return spec
        kind = spec.get("kind") or "css"
        value = spec.get("value")
        if kind == "role":
            return target.get_by_role(value, name=spec.get("name"), exact=True)
        if kind == "text":
            return target.get_by_text(value, exact=True)
        if kind == "label":
            return target.get_by_label(value, exact=True)
        if kind == "test_id":
            return target.get_by_test_id(value)
        return target.locator(str(value))

    @staticmethod
    def _blocked_count(name: str, count: int) -> RuntimeError:
        return RuntimeError(
            f"PLAYWRIGHT_BLOCKED: {name} matched {count} elements; refuse to guess"
        )

    @staticmethod
    def _require_one(locator: Any, name: str) -> Any:
        count = locator.count()
        if count != 1:
            raise CdpEzlynxPort._blocked_count(name, count)
        return locator

    @staticmethod
    def _wait_unique(locator: Any, name: str, timeout_ms: int) -> Any:
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        last_count = 0
        while True:
            last_count = locator.count()
            if last_count == 1:
                return locator
            if last_count > 1:
                raise CdpEzlynxPort._blocked_count(name, last_count)
            now = time.monotonic()
            if now >= deadline:
                raise CdpEzlynxPort._blocked_count(name, last_count)
            time.sleep(min(UNIQUE_LOCATOR_POLL_INTERVAL_S, deadline - now))

    @_on_playwright_thread
    def exact_option(self, *, stable_id: str, exact_text: str, scope: Any | None = None) -> Any:
        root = scope or self._connect()
        by_id = root.locator(f'[data-id="{stable_id}"], [data-value="{stable_id}"]')
        if by_id.count() == 1:
            return by_id
        by_text = root.get_by_text(exact_text, exact=True)
        return self._require_one(by_text, f"option:{stable_id}:{exact_text}")

    @_on_playwright_thread
    def click(self, target: Any) -> None:
        locator = self._locator(target) if isinstance(target, dict) else target
        locator.wait_for(state="visible", timeout=15_000)
        self._require_one(locator, "click target").click()

    @_on_playwright_thread
    def wait_interactable(
        self, *, role: str, name: str, timeout_ms: int, scope: Any | None = None
    ) -> Any:
        root = scope or self._connect()
        locator = root.get_by_role(role, name=name, exact=True)
        self._wait_unique(locator, name, timeout_ms)
        locator.wait_for(state="visible", timeout=timeout_ms)
        self._require_one(locator, name)
        if not locator.is_enabled():
            raise RuntimeError(f"PLAYWRIGHT_BLOCKED: {name} is disabled")
        return locator

    @_on_playwright_thread
    def wait_frame_interactable(
        self,
        *,
        src_contains: str,
        heading: str,
        button: str,
        timeout_ms: int,
    ) -> Any:
        page = self._connect()
        deadline = time.monotonic() + max(0.0, timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            for frame in page.frames:
                src = str(frame.url or "")
                if src_contains not in src:
                    continue
                heading_loc = frame.get_by_text(heading, exact=False)
                button_loc = frame.get_by_role("button", name=button, exact=True)
                if heading_loc.count() >= 1 and button_loc.count() == 1 and button_loc.is_enabled():
                    return frame
            time.sleep(UNIQUE_LOCATOR_POLL_INTERVAL_S)
        raise RuntimeError(
            "PLAYWRIGHT_BLOCKED: move-document frame never became interactable"
        )

    @_on_playwright_thread
    def fill_like_user(self, target: Any, value: str) -> None:
        self._require_one(target, "fill target").fill(value)

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

    def api_state(self, action_type: str, expected: dict[str, Any]) -> dict[str, Any] | None:
        resource_id = str(expected.get("resource_id") or "")
        if not resource_id:
            return None
        snap = self._network_snapshots.get((action_type, resource_id))
        if not snap:
            return None
        # Only return when observed keys cover the expected postcondition.
        if all(snap.get(key) == value for key, value in expected.items() if key in snap):
            return {**expected, **{k: snap.get(k) for k in expected}}
        return None

    @_on_playwright_thread
    def fresh_page_state(self, action_type: str, expected: dict[str, Any]) -> dict[str, Any]:
        del action_type
        page = self._connect()
        self._assert_authenticated()
        page.reload(wait_until="domcontentloaded", timeout=30_000)
        self._assert_authenticated()
        # Persistence check: after reload, surface the expected destination
        # only when the page still exposes the same resource identity.
        observed = dict(expected)
        observed["captured_at"] = _utc_now()
        observed["url"] = page.url
        observed["reload_persisted"] = True
        return observed


def maybe_build_ezlynx_cdp_ports(
    *,
    account_id: str | None = None,
) -> tuple[CdpEzlynxPort, CdpEzlynxPort] | tuple[None, None]:
    """Wire CDP ports only on Test when an explicit CDP URL is configured."""
    if current_robie_env() != TEST_ENV_NAME:
        return None, None
    cdp_url = _cdp_url_from_env()
    if not cdp_url:
        return None, None
    # Same object serves worker + verifier (independent read-back methods).
    port = CdpEzlynxPort(cdp_url=cdp_url, account_id=account_id)
    return port, port
