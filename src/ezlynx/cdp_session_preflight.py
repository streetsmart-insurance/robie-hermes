"""Live CDP session preflight — inspect the actual Chrome page, once.

Cookie ``storage_state`` and Classic API can report connected/active while the
hermes SSRobie tab on CDP :9222 is the EZLynx Login / forcedOff wall. That
false positive caused multi-minute attach-retry stalls (Paulette Fagone HO,
applicant 196126698).

This gate looks at the **live page** (URL / title / body). It never treats
storage_state or Classic API as proof of an authed dashboard. Hard cap: one
inspect, then stop — no attach-retry loop. Bots must not password-reset;
a human re-logins SSRobie on hermes CDP :9222.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("cdp_session_preflight")

HITL_RELOGIN_MESSAGE = (
    "HITL: human must re-login SSRobie on hermes CDP :9222. "
    "Bots must not password-reset."
)

# One inspect per attach. Callers must not loop this.
MAX_PREFLIGHT_CHECKS = 1

LOGIN_URL_FRAGMENTS = (
    "auth/account/login",
    "/auth/account/",
    "forcedoff",
    "forced-off",
    "forced_off",
)

AUTHED_URL_FRAGMENTS = (
    "/web/dashboard",
    "/web/account/",
    "/web/home",
    "/applicantportal/",
    "/web/reports",
    "/web/agency",
)

LOGIN_TITLE_MARKERS = (
    "login",
    "sign in",
    "sign-in",
    "signin",
)

LOGIN_BODY_MARKERS = (
    "forgot password",
    "forgot your password",
    "reset password",
    "reset your password",
    "you have been forced off",
    "forced off",
    "session expired",
    "please log in",
    "please sign in",
)

AUTHED_TITLE_MARKERS = (
    "dashboard",
    "applicant",
    "policy",
    "documents",
    "activity",
)

_LOGIN_PATH_RE = re.compile(r"/login(?:[/?#]|$)", re.IGNORECASE)


class CdpSessionBlocked(RuntimeError):
    """Live CDP page is Login / forcedOff / not an authed dashboard. Do not retry."""

    def __init__(self, preflight: "LiveCdpPreflightResult"):
        self.preflight = preflight
        super().__init__(preflight.error_message)


@dataclass
class LiveCdpPreflightResult:
    ok: bool
    status: str
    reason: str
    url: str = ""
    title: str = ""
    body_excerpt: str = ""
    signals: List[str] = field(default_factory=list)
    storage_state_active: Optional[bool] = None
    classic_api_active: Optional[bool] = None
    checks_run: int = 1
    page: Any = field(default=None, repr=False, compare=False)

    @property
    def error_message(self) -> str:
        return (
            f"HITL/BLOCKED: live CDP page is not an authed EZLynx dashboard "
            f"({self.reason}). url={self.url!r} title={self.title!r}. "
            f"{HITL_RELOGIN_MESSAGE}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
            "url": self.url,
            "title": self.title,
            "body_excerpt": (self.body_excerpt or "")[:500],
            "signals": list(self.signals),
            "storage_state_active": self.storage_state_active,
            "classic_api_active": self.classic_api_active,
            "checks_run": self.checks_run,
            "hitl": HITL_RELOGIN_MESSAGE,
            "trusted_storage_state": False,
            "trusted_classic_api": False,
        }


def _norm(value: Optional[str]) -> str:
    return (value or "").strip()


def url_is_login_or_forced_off(url: Optional[str]) -> bool:
    raw = _norm(url).lower()
    if not raw:
        return False
    if any(fragment in raw for fragment in LOGIN_URL_FRAGMENTS):
        return True
    return _LOGIN_PATH_RE.search(raw) is not None


def url_is_authed_dashboard(url: Optional[str]) -> bool:
    raw = _norm(url).lower()
    if not raw or url_is_login_or_forced_off(raw):
        return False
    return any(fragment in raw for fragment in AUTHED_URL_FRAGMENTS)


def _title_looks_like_login(title: Optional[str]) -> bool:
    raw = _norm(title).lower()
    return bool(raw) and any(marker in raw for marker in LOGIN_TITLE_MARKERS)


def _body_looks_like_login(body: Optional[str]) -> bool:
    raw = _norm(body).lower()
    return bool(raw) and any(marker in raw for marker in LOGIN_BODY_MARKERS)


def classify_live_page(
    url: str,
    title: str = "",
    body: str = "",
    *,
    storage_state_active: Optional[bool] = None,
    classic_api_active: Optional[bool] = None,
    has_login_form: bool = False,
    page: Any = None,
) -> LiveCdpPreflightResult:
    """Classify one live page. storage_state / Classic API never grant a pass."""
    signals: List[str] = []
    url_n = _norm(url)
    title_n = _norm(title)
    body_n = _norm(body)

    if url_is_login_or_forced_off(url_n):
        signals.append("login_or_forcedoff_url")
    if _title_looks_like_login(title_n):
        signals.append("login_title")
    if _body_looks_like_login(body_n):
        signals.append("login_body")
    if has_login_form:
        signals.append("login_form")
    if url_is_authed_dashboard(url_n):
        signals.append("authed_url")
    if any(marker in title_n.lower() for marker in AUTHED_TITLE_MARKERS) and not _title_looks_like_login(title_n):
        signals.append("authed_title")
    if storage_state_active:
        signals.append("storage_state_active_ignored")
    if classic_api_active:
        signals.append("classic_api_active_ignored")

    if url_is_login_or_forced_off(url_n):
        reason = "forcedoff_url" if "forcedoff" in url_n.lower().replace("-", "").replace("_", "") else "login_url"
        if "forced" in url_n.lower():
            reason = "forcedoff_url"
        return LiveCdpPreflightResult(
            ok=False,
            status="blocked",
            reason=reason,
            url=url_n,
            title=title_n,
            body_excerpt=body_n[:500],
            signals=signals,
            storage_state_active=storage_state_active,
            classic_api_active=classic_api_active,
            page=page,
        )

    if has_login_form or _title_looks_like_login(title_n) or _body_looks_like_login(body_n):
        if not url_is_authed_dashboard(url_n):
            return LiveCdpPreflightResult(
                ok=False,
                status="blocked",
                reason="login_page_signals",
                url=url_n,
                title=title_n,
                body_excerpt=body_n[:500],
                signals=signals,
                storage_state_active=storage_state_active,
                classic_api_active=classic_api_active,
                page=page,
            )

    if url_is_authed_dashboard(url_n):
        return LiveCdpPreflightResult(
            ok=True,
            status="authed",
            reason="live_dashboard",
            url=url_n,
            title=title_n,
            body_excerpt=body_n[:500],
            signals=signals,
            storage_state_active=storage_state_active,
            classic_api_active=classic_api_active,
            page=page,
        )

    return LiveCdpPreflightResult(
        ok=False,
        status="blocked",
        reason="not_authed_dashboard",
        url=url_n,
        title=title_n,
        body_excerpt=body_n[:500],
        signals=signals,
        storage_state_active=storage_state_active,
        classic_api_active=classic_api_active,
        page=page,
    )


def choose_preflight_result(
    results: Iterable[LiveCdpPreflightResult],
    *,
    storage_state_active: Optional[bool] = None,
    classic_api_active: Optional[bool] = None,
) -> LiveCdpPreflightResult:
    """Prefer an already-open authed tab; otherwise the first Login/forcedOff proof."""
    snapshots = list(results)
    for snap in snapshots:
        if snap.ok:
            return snap
    for snap in snapshots:
        if snap.reason in {"login_url", "forcedoff_url", "login_page_signals"}:
            return snap
    if snapshots:
        return snapshots[0]
    return LiveCdpPreflightResult(
        ok=False,
        status="blocked",
        reason="no_live_ezlynx_page",
        signals=["no_pages"],
        storage_state_active=storage_state_active,
        classic_api_active=classic_api_active,
    )


async def snapshot_live_page(page: Any) -> Dict[str, Any]:
    """Read URL/title/body from the current page. Never navigates. Never retries."""
    url = _norm(getattr(page, "url", "") or "")
    title = ""
    body = ""
    has_login_form = False
    try:
        title_fn = getattr(page, "title", None)
        if callable(title_fn):
            maybe = title_fn()
            title = await maybe if hasattr(maybe, "__await__") else (maybe or "")
            title = _norm(str(title or ""))
    except Exception as exc:
        logger.debug("Live CDP title read failed: %s", exc)
    try:
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            maybe = evaluate(
                """() => ({
                    body: ((document.body && document.body.innerText) || '').slice(0, 2500),
                    hasLoginForm: !!(
                        document.querySelector(
                            '#txtUserName, #txtPassword, #btnLogin, input[name="Username"], input[name="Password"]'
                        )
                    )
                })"""
            )
            info = await maybe if hasattr(maybe, "__await__") else maybe
            if isinstance(info, dict):
                body = _norm(str(info.get("body") or ""))
                has_login_form = bool(info.get("hasLoginForm"))
            elif isinstance(info, str):
                body = _norm(info)
    except Exception as exc:
        logger.debug("Live CDP body read failed: %s", exc)
    return {
        "url": url,
        "title": title,
        "body": body,
        "has_login_form": has_login_form,
        "page": page,
    }


async def preflight_live_cdp_session(
    context: Any,
    *,
    storage_state_active: Optional[bool] = None,
    classic_api_active: Optional[bool] = None,
) -> LiveCdpPreflightResult:
    """Inspect live CDP pages once. No navigation. No attach retry."""
    pages = list(getattr(context, "pages", None) or [])
    classified: List[LiveCdpPreflightResult] = []
    for page in pages:
        snap = await snapshot_live_page(page)
        classified.append(
            classify_live_page(
                snap["url"],
                snap["title"],
                snap["body"],
                storage_state_active=storage_state_active,
                classic_api_active=classic_api_active,
                has_login_form=bool(snap.get("has_login_form")),
                page=page,
            )
        )
    result = choose_preflight_result(
        classified,
        storage_state_active=storage_state_active,
        classic_api_active=classic_api_active,
    )
    result.checks_run = MAX_PREFLIGHT_CHECKS
    if result.ok:
        logger.info(
            "Live CDP preflight PASS url=%s title=%s (storage_state=%s classic_api=%s ignored)",
            result.url,
            result.title,
            storage_state_active,
            classic_api_active,
        )
    else:
        logger.error("%s", result.error_message)
    return result


def assert_live_cdp_authed(result: LiveCdpPreflightResult) -> LiveCdpPreflightResult:
    """Hard stop after the single check. Callers must not retry attach."""
    if result.checks_run > MAX_PREFLIGHT_CHECKS:
        result.ok = False
        result.status = "blocked"
        result.reason = "preflight_retry_refused"
        result.signals = list(result.signals) + ["retry_refused"]
    if not result.ok:
        raise CdpSessionBlocked(result)
    return result
