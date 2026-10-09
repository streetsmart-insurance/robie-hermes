"""Phase 1 browser port: Playwright-backed BrowserPort for carrier portal pulls.

Two runtimes (chosen per carrier by the adapter's declared ``runtime``):

- ``box``: Chromium on hermes-poc-01 behind the residential proxy
  (http://9.142.10.166:5822) with the proven anti-detection flag
  ``--disable-blink-features=AutomationControlled`` (see repo ~/AGENTS.md:
  the proxy exits as a Comcast residential IP; the flag is the box's
  working anti-detection, not the unreferenced playwright-stealth lib).
- ``sandbox``: local Chromium, direct egress. For carriers that block
  the box (proven: Hartford EBC, Selective agent portal). Neither is in
  the Phase 1 pilot; the runtime exists so the port is honest about it.

Read-only by shape: the port exposes navigation, form fill, click,
explicit waits, download capture, and screenshots. There is no
form-submit-to-bind, no send, no upload. Adapter actions are still gated
by READ_ONLY_ACTIONS in phase1_doc_pull before any adapter code runs.

Reliability pieces:

- ``TransientBrowserError`` / ``SessionExpiredError``: the error
  taxonomy. Transient failures (navigation/action timeout, dropped
  connection) are retried with backoff via ``run_with_retries``.
  ``SessionExpiredError`` (the portal's login screen reappeared
  mid-flow) is NOT blind-retried — the runner gives it exactly one
  fresh-context re-login via ``CarrierSession.reset()``.
- ``CarrierSession``: one reused browser context per carrier ("sign in
  once per carrier"). The adapter's login steps run against the same
  context for every policy of that carrier, so cookies persist.
- Every port action is timestamped into ``action_log`` (selectors only —
  NEVER filled values, which may be credentials). The runner attaches
  the log to the per-policy evidence and saves a screenshot on failure.

No live portal is touched by importing this module. Playwright is
imported lazily inside ``new_browser()`` so unit tests and CI (which
inject a fake page) never need a real browser.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .phase1_adapters.base import (
    BrowserPort,
    SessionExpiredError,
    TransientBrowserError,
)

# --- runtime configuration ---------------------------------------------

BOX_PROXY = "http://9.142.10.166:5822"
# Proven on hermes-poc-01 (~/AGENTS.md 2026-09-18): the working
# anti-detection is this Chromium flag, not playwright-stealth.
ANTI_DETECTION_ARGS = ["--disable-blink-features=AutomationControlled"]

NAV_TIMEOUT_MS = 60_000
ACTION_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 60_000

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 2.0


@dataclass(frozen=True)
class RuntimeConfig:
    name: str
    proxy: str | None  # proxy server URL, None = direct egress
    headless: bool = True
    extra_args: tuple = ()
    # Playwright browser channel (e.g. "chrome" for the system Google
    # Chrome). Required on hermes-poc-01: the box has no
    # ~/.cache/ms-playwright binaries, so a bare chromium.launch() fails
    # with "Executable doesn't exist". Proven on the box 2026-09-28.
    channel: str | None = None


def _box_args() -> tuple:
    # --no-sandbox: the box runs Chromium as a service user without a
    # desktop sandbox; required for launch there. Not needed on a dev
    # machine (sandbox runtime).
    return tuple(ANTI_DETECTION_ARGS) + ("--no-sandbox",)


RUNTIMES: dict[str, RuntimeConfig] = {
    "box": RuntimeConfig(
        name="box",
        proxy=BOX_PROXY,
        headless=True,
        extra_args=_box_args(),
        channel="chrome",
    ),
    "sandbox": RuntimeConfig(
        name="sandbox",
        proxy=None,
        headless=True,
        extra_args=tuple(ANTI_DETECTION_ARGS),
    ),
}


# --- error taxonomy -----------------------------------------------------

# Message fragments that mark a failure as transient (worth retrying).
# Heuristic by necessity: Playwright surfaces these as plain Errors.
_TRANSIENT_MARKERS = (
    "timed out",
    "Timeout",
    "net::",
    "ERR_",
    "Target crashed",
    "Connection closed",
    "Connection refused",
    "NS_ERROR",
)


def is_transient(exc: BaseException) -> bool:
    """True when ``exc`` is worth retrying with backoff.

    SessionExpiredError is deliberately NOT transient here: retrying on
    a dead session without re-login is pointless — the runner handles it
    with exactly one fresh-context re-login instead.
    """
    if isinstance(exc, SessionExpiredError):
        return False
    if isinstance(exc, TransientBrowserError):
        return True
    if type(exc).__name__ == "TimeoutError":
        return True
    msg = str(exc)
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def run_with_retries(
    fn: Callable[[], Any],
    *,
    attempts: int = RETRY_ATTEMPTS,
    base_delay_s: float = RETRY_BASE_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> Any:
    """Run ``fn``; retry transient failures with exponential backoff.

    Permanent failures (and SessionExpiredError) propagate immediately.
    After ``attempts`` transient failures the last one is raised.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not is_transient(exc):
                raise
            last_exc = exc
            if attempt < attempts:
                delay = base_delay_s * (2 ** (attempt - 1))
                if on_retry is not None:
                    on_retry(attempt, exc, delay)
                sleep(delay)
    assert last_exc is not None  # attempts >= 1
    raise last_exc


# --- the port ------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PlaywrightBrowserPort:
    """Playwright implementation of the BrowserPort protocol.

    ``page`` is injected (production: a real Playwright page from
    ``new_browser``; tests: a fake with the same surface), so this class
    never imports playwright itself.
    """

    def __init__(
        self,
        *,
        runtime: str,
        page: Any,
        context: Any = None,
        playwright_handle: Any = None,
    ) -> None:
        self._runtime = runtime
        self._page = page
        self._context = context
        self._pw_handle = playwright_handle
        self._action_log: list[dict] = []
        self._closed = False

    # -- action log (selectors only — never filled values) --
    @property
    def action_log(self) -> list[dict]:
        return list(self._action_log)

    def reset_log(self) -> None:
        self._action_log = []

    def _record(self, action: str, detail: str = "") -> None:
        self._action_log.append({"t": _utcnow(), "action": action, "detail": detail})

    def _wrap(self, action: str, detail: str, fn: Callable[[], Any]) -> Any:
        self._record(action, detail)
        try:
            return fn()
        except (TransientBrowserError, SessionExpiredError):
            raise  # already classified — never re-wrap
        except Exception as exc:
            if is_transient(exc):
                raise TransientBrowserError(
                    f"{action} failed transiently: {type(exc).__name__}: {exc}"
                ) from exc
            raise

    # -- BrowserPort protocol --
    def goto(self, url: str) -> None:
        def _do() -> str:
            self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            # post-action read-back: where did we actually land?
            return str(getattr(self._page, "url", ""))

        final_url = self._wrap("goto", url, _do)
        self._record("landed", final_url)

    def fill(self, selector: str, value: str) -> None:
        # The VALUE is never recorded: it may be a credential.
        def _do() -> None:
            self._page.fill(selector, value, timeout=ACTION_TIMEOUT_MS)

        self._wrap("fill", selector, _do)

    def click(self, selector: str) -> None:
        def _do() -> None:
            self._page.click(selector, timeout=ACTION_TIMEOUT_MS)

        self._wrap("click", selector, _do)

    def wait_for_selector(self, selector: str, timeout_ms: int = 30000) -> None:
        def _do() -> None:
            self._page.wait_for_selector(
                selector, state="visible", timeout=timeout_ms
            )

        self._wrap("wait", f"{selector} (timeout_ms={timeout_ms})", _do)

    def has_selector(self, selector: str, timeout_ms: int = 5000) -> bool:
        """Quiet presence probe for the session-expiry check. Never raises.

        Used by the runner before starting work on each policy on a
        reused session: the adapter's ``logged_in_indicator`` must be
        present, or the session is treated as silently expired. A short
        timeout keeps a dead page from stalling the run; ``attached``
        (not ``visible``) is enough to prove the authenticated DOM is
        there.
        """
        try:
            self._page.wait_for_selector(
                selector, state="attached", timeout=timeout_ms
            )
            present = True
        except Exception:
            present = False
        self._record("probe", f"{selector} -> {'present' if present else 'absent'}")
        return present

    def download(self, click_selector: str, dest_path: Path) -> Path:
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _do() -> None:
            with self._page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                self._page.click(click_selector, timeout=ACTION_TIMEOUT_MS)
            download = dl_info.value
            download.save_as(str(dest))

        self._wrap("download", f"{click_selector} -> {dest.name}", _do)
        # post-action read-back: the file must actually be there
        if not dest.is_file() or dest.stat().st_size == 0:
            raise TransientBrowserError(f"download produced no file: {dest}")
        return dest

    def screenshot(self, dest_path: str | Path) -> Path:
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._record("screenshot", dest.name)
        self._page.screenshot(path=str(dest))
        return dest

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._record("close", self._runtime)
        for _closer in (self._close_context, self._close_playwright):
            try:
                _closer()
            except Exception:
                pass  # close is best-effort; never mask the run's outcome

    def _close_context(self) -> None:
        if self._context is not None:
            self._context.close()

    def _close_playwright(self) -> None:
        # handle is (playwright, browser) in production, None in tests
        if self._pw_handle is not None:
            pw, browser = self._pw_handle
            try:
                browser.close()
            finally:
                pw.stop()


# --- construction --------------------------------------------------------


def new_browser(runtime: str) -> BrowserPort:
    """Build a live Playwright port for ``runtime`` ("box" | "sandbox").

    The runtime is validated BEFORE playwright is imported so a bad
    runtime name fails fast without needing a browser installed.
    """
    cfg = RUNTIMES.get(runtime)
    if cfg is None:
        raise ValueError(
            f"unknown browser runtime {runtime!r}; expected one of {sorted(RUNTIMES)}"
        )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed in this environment; the pilot's "
            "live browser runs on hermes-poc-01 (box venv). "
            "See pyproject.toml."
        ) from exc

    pw = sync_playwright().start()
    try:
        launch_kwargs: dict[str, Any] = {
            "headless": cfg.headless,
            "args": list(cfg.extra_args),
        }
        if cfg.channel:
            launch_kwargs["channel"] = cfg.channel
        if cfg.proxy:
            launch_kwargs["proxy"] = {"server": cfg.proxy}
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
    except Exception:
        pw.stop()
        raise
    port = PlaywrightBrowserPort(
        runtime=runtime, page=page, context=context, playwright_handle=(pw, browser)
    )
    port._record(
        "new_browser",
        f"runtime={runtime} headless={cfg.headless} "
        f"proxy={'yes' if cfg.proxy else 'no'}",
    )
    return port


def default_browser_factory() -> Callable[[str], BrowserPort]:
    """``browser_factory`` for run_pilot: runtime string -> live port."""

    def factory(runtime: str) -> BrowserPort:
        return new_browser(runtime)

    return factory


class CarrierSession:
    """One reused browser context per carrier ("sign in once per carrier").

    The port is created lazily and reused across that carrier's policies
    so the adapter's login steps run once and cookies persist.
    ``reset()`` closes the context and builds a fresh one — used exactly
    once per carrier when the runner detects session expiry.
    """

    def __init__(
        self,
        carrier_id: str,
        runtime: str,
        port_factory: Callable[[str], BrowserPort],
    ) -> None:
        self.carrier_id = carrier_id
        self.runtime = runtime
        self._port_factory = port_factory
        self._port: BrowserPort | None = None

    def port(self) -> BrowserPort:
        if self._port is None:
            self._port = self._port_factory(self.runtime)
        return self._port

    def reset(self) -> BrowserPort:
        """Fresh context for the single re-login. The old one is closed."""
        self.close()
        self._port = self._port_factory(self.runtime)
        return self._port

    def close(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            try:
                close = getattr(port, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
