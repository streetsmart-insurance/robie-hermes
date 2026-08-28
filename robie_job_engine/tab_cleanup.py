"""Close leftover Playwright tabs after a job ends.

Carlo's 2026-08-27 hunch: frozen recording + busy Playwright + zero destination
evidence is leftover dead tabs. Old pages stay open after COMPLETE / FAILED /
UNVERIFIED. The recorder or Playwright attaches to a stale Policies / login /
previous-account / carrier tab while the live job works in another.

This module closes job-owned pages on terminal close-out (any host that job
opened, not only EZLynx / Ascend), sweeps EZLynx-shaped orphans, and flushes
every leftover page that no RUNNING / AWAITING_HUMAN_INPUT / VERIFYING job
claims. It keeps exactly one authenticated ``https://app.ezlynx.com/web/``
session tab. It never restarts Chrome, never wipes the EZLynx profile, and
never logs out.

Close mechanism is Chrome's HTTP CDP ``/json/close/{id}`` (Target.closeTarget).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .ezlynx_account_nav import (
    EDIT_ACCOUNT_RE,
    FORMENTRY_RE,
    WEB_ACCOUNT_RE,
    extract_known_account_id,
)
from .models import JobStatus, TERMINAL_STATUSES
from .production_preflight import (
    AUTHENTICATED_APP_PREFIX,
    DEFAULT_CDP_URL,
    DEFAULT_JOBS_DB,
    LOGIN_PATH,
    ezlynx_web_tab_ok,
)
from .recording_tab import (
    TabCandidate,
    extract_account_ids,
    extract_playwright_urls,
    is_ezlynx_url,
    page_identity,
    pages_as_candidates,
    read_page_hint,
    resolve_hint_file,
    select_playwright_page,
    select_recording_tab,
    write_page_hint,
)
from .store import JobStore


LIVE_TAB_STATUSES = frozenset(
    {
        JobStatus.RUNNING,
        JobStatus.AWAITING_HUMAN_INPUT,
        JobStatus.VERIFYING,
    }
)
PAGE_TARGET_TYPES = frozenset({"", "page", "tab"})
ASCEND_HOST_RE = re.compile(r"(^|\.)ascend\.", re.IGNORECASE)
ASCEND_PATH_RE = re.compile(r"/ascend(?:/|$)", re.IGNORECASE)
HTTP_URL_RE = re.compile(r"https?://[^\s'\"\\<>]+", re.IGNORECASE)
FLUSH_MODE = "flush"
SWEEP_MODE = "sweep"
TERMINAL_MODE = "terminal"
SESSION_SEED_URL = "https://app.ezlynx.com/web/"
EMPTY_TARGET_EVIDENCE = "empty CDP target list; session is not fine"


@dataclass(frozen=True)
class BrowserTab:
    """One CDP page target or Playwright page the cleaner can close."""

    identity: str
    url: str
    title: str = ""
    target_type: str = "page"

    @property
    def is_page(self) -> bool:
        return str(self.target_type or "page").casefold() in PAGE_TARGET_TYPES


@dataclass
class TabClaims:
    """Account ids and URLs a job is allowed to keep open."""

    job_ids: set[str] = field(default_factory=set)
    account_ids: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    hosts: set[str] = field(default_factory=set)

    def claims(self, url: str) -> bool:
        raw = str(url or "").strip()
        if not raw:
            return False
        folded = raw.casefold()
        for item in self.urls:
            other = str(item or "").strip()
            if not other:
                continue
            if other == raw or other.casefold() in folded or folded in other.casefold():
                return True
        return bool(account_ids_in_url(raw) & self.account_ids)

    def opened_host(self, url: str) -> bool:
        """True when this job opened a non-EZLynx page on the same host."""
        host = url_host(url)
        if not host or is_ezlynx_url(url) or is_blank_url(url):
            return False
        return host in self.hosts


@dataclass
class CleanupPlan:
    close: list[BrowserTab] = field(default_factory=list)
    keep: list[BrowserTab] = field(default_factory=list)
    session_tab: BrowserTab | None = None
    reason: str = ""

    def close_identities(self) -> set[str]:
        return {tab.identity for tab in self.close}


def _cdp_url(cdp_url: str | None = None) -> str:
    return (cdp_url or os.environ.get("ROBIE_BROWSER_CDP_URL") or DEFAULT_CDP_URL).rstrip(
        "/"
    )


def _jobs_db(db_path: str | Path | None = None) -> Path:
    return Path(db_path or os.environ.get("ROBIE_JOB_DB") or DEFAULT_JOBS_DB)


def _http_get(url: str, *, timeout: float = 2.0) -> tuple[int, bytes]:
    request = Request(url, method="GET")
    with urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", 200) or 200)
        return status, response.read()


def url_host(url: str) -> str:
    return (urlsplit(str(url or "").strip()).netloc or "").casefold()


def extract_http_urls(text: str) -> set[str]:
    found: list[str] = []
    for match in HTTP_URL_RE.finditer(str(text or "")):
        found.append(match.group(0).rstrip(").,;\"'"))
    found.extend(extract_playwright_urls(text))
    return {item for item in found if item}


def is_login_url(url: str) -> bool:
    path = urlsplit(str(url or "").strip().casefold()).path
    return LOGIN_PATH in path


def is_blank_url(url: str) -> bool:
    raw = str(url or "").strip().casefold()
    return raw in {"", "about:blank", "about:blank#blocked"}


def is_ascend_url(url: str) -> bool:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.netloc or "").casefold()
    path = (parsed.path or "").casefold()
    if "ascending" in host or "ascending" in path:
        return False
    return bool(ASCEND_HOST_RE.search(host) or ASCEND_PATH_RE.search(path))


def is_generic_web_session(url: str) -> bool:
    """Authenticated /web/ home. Not an account file and not login."""
    if not ezlynx_web_tab_ok(url):
        return False
    path = urlsplit(str(url or "").strip().casefold()).path.rstrip("/")
    return "/web/account/" not in (path + "/")


def session_keep_rank(url: str) -> int:
    path = urlsplit(str(url or "").strip().casefold()).path.rstrip("/") or "/web"
    if path in {"/web", ""}:
        return 3
    if path in {"/web/policies", "/web/home"}:
        return 2
    if is_generic_web_session(url):
        return 1
    if ezlynx_web_tab_ok(url):
        return 0
    return -1


def account_ids_in_url(url: str) -> set[str]:
    raw = str(url or "")
    found: set[str] = set()
    for match in (WEB_ACCOUNT_RE.search(raw), EDIT_ACCOUNT_RE.search(raw), FORMENTRY_RE.search(raw)):
        if match:
            found.add(match.group(1))
    found.update(extract_account_ids(raw))
    return {item for item in found if item.isdigit() and len(item) >= 6}


def _blob_from_mapping(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)


def job_claim_text(job: dict[str, Any], checkpoints: Iterable[Any] = ()) -> str:
    payload = dict(job.get("payload") or {})
    parts = [
        str(job.get("id") or ""),
        str(job.get("action_type") or ""),
        str(job.get("last_error") or ""),
        _blob_from_mapping(payload),
        str(payload.get("text") or ""),
        str(payload.get("account_id") or ""),
        str(payload.get("applicant_id") or ""),
        str(payload.get("url") or ""),
    ]
    for item in checkpoints:
        parts.append(_blob_from_mapping(item))
    return "\n".join(part for part in parts if part)


def claims_from_text(*chunks: str) -> TabClaims:
    blob = "\n".join(str(item or "") for item in chunks if item)
    accounts = set(extract_account_ids(blob))
    known = extract_known_account_id(blob)
    if known:
        accounts.add(known)
    for match in WEB_ACCOUNT_RE.finditer(blob):
        accounts.add(match.group(1))
    urls = extract_http_urls(blob)
    return TabClaims(
        account_ids={item for item in accounts if item.isdigit() and len(item) >= 6},
        urls=urls,
        hosts={
            host
            for host in (url_host(item) for item in urls)
            if host and not is_ezlynx_url(f"https://{host}/")
        },
    )


def claims_for_job(store: JobStore, job: dict[str, Any]) -> TabClaims:
    checkpoints: list[Any] = []
    for kind in ("worker_response", "action", "post_job_audit"):
        data = store.get_checkpoint(job["id"], kind)
        if data:
            checkpoints.append(data)
    try:
        checkpoints.extend(store.list_attempts(job["id"]))
    except Exception:
        pass
    try:
        from .recording import RecordingStore
        from .recording_tab import load_attach_log

        for item in RecordingStore(store.path).list_for_job(job["id"]):
            checkpoints.append(item)
            attach = load_attach_log(item.get("local_path"))
            if attach:
                checkpoints.append(attach)
    except Exception:
        pass
    claims = claims_from_text(job_claim_text(job, checkpoints))
    claims.job_ids.add(str(job["id"]))
    hint = read_page_hint(resolve_hint_file())
    if hint and str(hint.get("job_id") or "") in {str(job["id"]), ""}:
        url = str(hint.get("url") or "").strip()
        if url:
            claims.urls.add(url)
            claims.account_ids.update(account_ids_in_url(url))
            host = url_host(url)
            if host and not is_ezlynx_url(url):
                claims.hosts.add(host)
    return claims


def live_tab_claims(db_path: str | Path | None = None) -> TabClaims:
    path = _jobs_db(db_path)
    merged = TabClaims()
    if not path.is_file():
        return merged
    store = JobStore(path)
    for job in store.list_jobs_by_status(LIVE_TAB_STATUSES):
        row = claims_for_job(store, job)
        merged.job_ids.update(row.job_ids)
        merged.account_ids.update(row.account_ids)
        merged.urls.update(row.urls)
        merged.hosts.update(row.hosts)
    hint = read_page_hint(resolve_hint_file())
    if hint and str(hint.get("job_id") or "") in merged.job_ids:
        url = str(hint.get("url") or "").strip()
        if url:
            merged.urls.add(url)
            merged.account_ids.update(account_ids_in_url(url))
            host = url_host(url)
            if host and not is_ezlynx_url(url):
                merged.hosts.add(host)
    return merged


def tabs_from_cdp_payload(payload: Any) -> list[BrowserTab]:
    if isinstance(payload, dict):
        payload = payload.get("value") or payload.get("items") or []
    if not isinstance(payload, list):
        return []
    tabs: list[BrowserTab] = []
    for index, item in enumerate(payload):
        if isinstance(item, str):
            tabs.append(BrowserTab(identity=f"url-{index}", url=item))
            continue
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        identity = str(item.get("id") or item.get("targetId") or "").strip()
        if not identity:
            identity = f"url-{index}:{url}"
        tabs.append(
            BrowserTab(
                identity=identity,
                url=url,
                title=str(item.get("title") or ""),
                target_type=str(item.get("type") or "page"),
            )
        )
    return tabs


def tabs_from_pages(pages: Iterable[Any]) -> list[BrowserTab]:
    return [
        BrowserTab(
            identity=candidate.identity,
            url=candidate.url,
            title=candidate.title,
            target_type="page",
        )
        for candidate, _live in pages_as_candidates(pages)
    ]


def list_cdp_tabs(
    *,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
    tabs: list[BrowserTab] | None = None,
) -> list[BrowserTab]:
    if tabs is not None:
        return list(tabs)
    url = _cdp_url(cdp_url)
    getter = http_get or _http_get
    last_error = "no CDP tab list"
    for suffix in ("/json/list", "/json"):
        try:
            status, body = getter(f"{url}{suffix}")
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = f"{url}{suffix} {type(exc).__name__}"
            continue
        if int(status) != 200:
            last_error = f"{url}{suffix} HTTP {status}"
            continue
        try:
            return tabs_from_cdp_payload(json.loads(body.decode("utf-8", errors="replace")))
        except json.JSONDecodeError:
            last_error = f"{url}{suffix} invalid JSON"
            continue
    raise RuntimeError(last_error)


def ensure_one_browser_page(
    *,
    tabs: list[BrowserTab] | None = None,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
    opener: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Refuse 'session fine' when CDP has zero pages.

    Attach-only bootstrap fails with ``Persistent EZLynx browser has no page``.
    Open one seed page (CDP ``/json/new``) when possible. Never restart Chrome.
    Never dump secrets. An opened seed page is not AUTHENTICATED.
    """
    listed = list(tabs) if tabs is not None else []
    if tabs is None:
        try:
            listed = list_cdp_tabs(cdp_url=cdp_url, http_get=http_get)
        except Exception as exc:
            return {
                "ok": False,
                "opened": False,
                "count": 0,
                "session_fine": False,
                "status": "INCONCLUSIVE",
                "evidence": f"{EMPTY_TARGET_EVIDENCE} ({type(exc).__name__})",
            }
    pages = [tab for tab in listed if tab.is_page]
    if pages:
        return {
            "ok": True,
            "opened": False,
            "count": len(pages),
            "session_fine": False,
            "status": "HAS_PAGES",
            "evidence": f"{len(pages)} CDP page(s); not session fine by count alone",
        }
    opened_url = ""
    if opener is not None:
        opened = opener()
        if opened:
            opened_url = str(getattr(opened, "url", "") or SESSION_SEED_URL)
    else:
        getter = http_get or _http_get
        seed = f"{_cdp_url(cdp_url)}/json/new?{SESSION_SEED_URL}"
        try:
            status, body = getter(seed)
        except (URLError, TimeoutError, OSError) as exc:
            return {
                "ok": False,
                "opened": False,
                "count": 0,
                "session_fine": False,
                "status": "INCONCLUSIVE",
                "evidence": f"{EMPTY_TARGET_EVIDENCE} ({type(exc).__name__})",
            }
        if int(status) != 200:
            return {
                "ok": False,
                "opened": False,
                "count": 0,
                "session_fine": False,
                "status": "INCONCLUSIVE",
                "evidence": f"{EMPTY_TARGET_EVIDENCE} (HTTP {status})",
            }
        opened_url = SESSION_SEED_URL
        del body
    if not opened_url:
        return {
            "ok": False,
            "opened": False,
            "count": 0,
            "session_fine": False,
            "status": "INCONCLUSIVE",
            "evidence": EMPTY_TARGET_EVIDENCE,
        }
    return {
        "ok": True,
        "opened": True,
        "count": 1,
        "session_fine": False,
        "status": "OPENED_SEED_PAGE",
        "url": opened_url,
        "evidence": (
            f"opened seed page {opened_url}; not AUTHENTICATED; "
            "session is not fine until /web/ is authenticated"
        ),
    }


def choose_session_tab(tabs: Iterable[BrowserTab]) -> BrowserTab | None:
    pages = [tab for tab in tabs if tab.is_page and session_keep_rank(tab.url) >= 0]
    if not pages:
        return None
    return max(pages, key=lambda tab: (session_keep_rank(tab.url), -len(tab.url)))


def is_orphan_leftover(url: str) -> bool:
    raw = str(url or "").strip()
    if is_blank_url(raw) or is_login_url(raw) or is_ascend_url(raw):
        return True
    if not is_ezlynx_url(raw):
        return False
    path = urlsplit(raw.casefold()).path
    if "/web/account/" in path or "/applicantportal/" in path:
        return True
    if is_generic_web_session(raw):
        return True
    return ezlynx_web_tab_ok(raw)


def plan_tab_cleanup(
    tabs: Iterable[BrowserTab],
    *,
    live: TabClaims | None = None,
    job: TabClaims | None = None,
    mode: str = "sweep",
) -> CleanupPlan:
    """Decide which page targets to close. Never closes a live-claimed tab."""
    pages = [tab for tab in tabs if tab and tab.is_page]
    live = live or TabClaims()
    job = job or TabClaims()
    mode = str(mode or SWEEP_MODE).strip().casefold()
    session = choose_session_tab(
        tab
        for tab in pages
        if is_generic_web_session(tab.url) or ezlynx_web_tab_ok(tab.url)
    )
    if session is None:
        session = choose_session_tab(pages)
    close: list[BrowserTab] = []
    keep: list[BrowserTab] = []
    for tab in pages:
        if live.claims(tab.url):
            keep.append(tab)
            continue
        if session is not None and tab.identity == session.identity:
            keep.append(tab)
            continue
        job_owned = bool(job.account_ids or job.urls or job.hosts) and (
            job.claims(tab.url) or job.opened_host(tab.url)
        )
        leftover = is_orphan_leftover(tab.url)
        if mode == TERMINAL_MODE:
            should_close = job_owned or leftover
        elif mode == FLUSH_MODE:
            should_close = True
        else:
            should_close = leftover
        if should_close:
            close.append(tab)
        else:
            keep.append(tab)
    extras = [
        tab
        for tab in keep
        if session is not None
        and tab.identity != session.identity
        and is_generic_web_session(tab.url)
        and not live.claims(tab.url)
    ]
    for tab in extras:
        keep.remove(tab)
        close.append(tab)
    return CleanupPlan(
        close=close,
        keep=keep,
        session_tab=session,
        reason=(
            f"{mode}: close {len(close)} leftover tab(s); "
            f"keep {len(keep)} including one /web/ session"
        ),
    )


def close_cdp_target(
    target_id: str,
    *,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
) -> dict[str, Any]:
    """HTTP mapping of CDP Target.closeTarget: GET /json/close/{id}."""
    identity = str(target_id or "").strip()
    if not identity:
        return {"ok": False, "id": identity, "error": "missing target id"}
    url = f"{_cdp_url(cdp_url)}/json/close/{identity}"
    getter = http_get or _http_get
    try:
        status, body = getter(url)
    except (URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "id": identity, "error": type(exc).__name__}
    ok = int(status) == 200
    return {
        "ok": ok,
        "id": identity,
        "status": int(status),
        "evidence": body.decode("utf-8", errors="replace")[:200],
    }


def close_playwright_page(page: Any) -> dict[str, Any]:
    identity = page_identity(page)
    closer = getattr(page, "close", None)
    if not callable(closer):
        return {"ok": False, "id": identity, "error": "page has no close()"}
    closer()
    return {"ok": True, "id": identity, "url": str(getattr(page, "url", "") or "")}


def apply_cleanup(
    plan: CleanupPlan,
    *,
    pages: Iterable[Any] | None = None,
    closer: Callable[[BrowserTab], Any] | None = None,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
) -> dict[str, Any]:
    by_id = {candidate.identity: live for candidate, live in pages_as_candidates(pages or [])}
    closed: list[dict[str, Any]] = []
    errors: list[str] = []
    for tab in plan.close:
        try:
            if closer is not None:
                result = closer(tab)
                closed.append({"id": tab.identity, "url": tab.url, "result": result})
                continue
            live = by_id.get(tab.identity)
            if live is not None:
                closed.append(close_playwright_page(live))
                continue
            closed.append(close_cdp_target(tab.identity, cdp_url=cdp_url, http_get=http_get))
        except Exception as exc:
            errors.append(f"{tab.identity}: {type(exc).__name__}")
    remaining = [tab for tab in plan.keep]
    return {
        "closed": closed,
        "closed_urls": [tab.url for tab in plan.close],
        "kept_urls": [tab.url for tab in remaining],
        "session_url": plan.session_tab.url if plan.session_tab else None,
        "errors": errors,
        "reason": plan.reason,
    }


def retarget_recorder_hint(
    remaining: Iterable[BrowserTab],
    *,
    hint_url: str | None = None,
    job_id: str | None = None,
    hint_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Point the recorder at the current job tab. Never first-ezlynx-wins."""
    candidates = [
        TabCandidate(identity=tab.identity, url=tab.url, title=tab.title)
        for tab in remaining
        if tab.url
    ]
    chosen = select_recording_tab(candidates, hint_url=hint_url)
    if chosen is None:
        return None
    path = hint_path or resolve_hint_file()
    if path is not None:
        write_page_hint(path, url=chosen.url, job_id=job_id)
    return {"url": chosen.url, "identity": chosen.identity, "job_id": job_id or ""}


def cleanup_terminal_job_tabs(
    db_path: str | Path | None,
    job_id: str,
    *,
    pages: Iterable[Any] | None = None,
    tabs: list[BrowserTab] | None = None,
    closer: Callable[[BrowserTab], Any] | None = None,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
) -> dict[str, Any]:
    """Close pages this finished job opened. Leaves one /web/ session tab."""
    path = _jobs_db(db_path)
    store = JobStore(path) if path.is_file() else None
    job = None
    if store is not None:
        try:
            job = store.get_job(job_id)
        except KeyError:
            job = None
    if job is not None and JobStatus(job["status"]) not in TERMINAL_STATUSES:
        return {"ok": True, "skipped": "job is not terminal", "job_id": job_id}
    job_claims = claims_for_job(store, job) if store is not None and job is not None else claims_from_text(job_id)
    live = live_tab_claims(path)
    listed = tabs_from_pages(pages) if pages is not None else list_cdp_tabs(
        cdp_url=cdp_url, http_get=http_get, tabs=tabs
    )
    plan = plan_tab_cleanup(listed, live=live, job=job_claims, mode=TERMINAL_MODE)
    applied = apply_cleanup(
        plan, pages=pages, closer=closer, cdp_url=cdp_url, http_get=http_get
    )
    remaining = [tab for tab in listed if tab.identity not in plan.close_identities()]
    hint = retarget_recorder_hint(
        remaining,
        hint_url=next(iter(job_claims.urls), None),
        job_id=job_id,
    )
    applied.update(
        {
            "ok": not applied["errors"],
            "job_id": job_id,
            "mode": TERMINAL_MODE,
            "hint": hint,
            "selection_mode": "recent_navigation",
        }
    )
    return applied


def sweep_orphaned_tabs(
    db_path: str | Path | None = None,
    *,
    pages: Iterable[Any] | None = None,
    tabs: list[BrowserTab] | None = None,
    closer: Callable[[BrowserTab], Any] | None = None,
    cdp_url: str | None = None,
    http_get: Callable[[str], tuple[int, bytes]] | None = None,
    mode: str = SWEEP_MODE,
) -> dict[str, Any]:
    """Close orphan leftovers. Never close a live job's tab.

    ``sweep`` closes EZLynx-shaped leftovers. ``flush`` treats any unclaimed
    page as leftover.
    """
    chosen = str(mode or SWEEP_MODE).strip().casefold()
    if chosen not in {SWEEP_MODE, FLUSH_MODE}:
        chosen = SWEEP_MODE
    path = _jobs_db(db_path)
    live = live_tab_claims(path)
    listed = tabs_from_pages(pages) if pages is not None else list_cdp_tabs(
        cdp_url=cdp_url, http_get=http_get, tabs=tabs
    )
    plan = plan_tab_cleanup(listed, live=live, mode=chosen)
    applied = apply_cleanup(
        plan, pages=pages, closer=closer, cdp_url=cdp_url, http_get=http_get
    )
    remaining = [tab for tab in listed if tab.identity not in plan.close_identities()]
    hint_url = next(iter(live.urls), None)
    hint = retarget_recorder_hint(
        remaining,
        hint_url=hint_url,
        job_id=next(iter(live.job_ids), None),
    )
    applied.update(
        {
            "ok": not applied["errors"],
            "mode": chosen,
            "live_job_ids": sorted(live.job_ids),
            "live_account_ids": sorted(live.account_ids),
            "hint": hint,
            "selection_mode": "recent_navigation",
            "session_fine": False,
        }
    )
    if not remaining:
        seed = ensure_one_browser_page(
            tabs=remaining,
            cdp_url=cdp_url,
            http_get=http_get,
        )
        applied["seed_page"] = seed
        applied["session_status"] = seed.get("status")
        applied["session_fine"] = False
        if not seed.get("ok"):
            applied["ok"] = False
            applied["evidence"] = seed.get("evidence") or EMPTY_TARGET_EVIDENCE
        else:
            applied["kept_urls"] = list(applied.get("kept_urls") or []) + [
                str(seed.get("url") or SESSION_SEED_URL)
            ]
            applied["evidence"] = seed.get("evidence")
    return applied


def flush_orphaned_tabs(
    db_path: str | Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Close every leftover page no live job claims. Keep one /web/ session."""
    kwargs.pop("mode", None)
    return sweep_orphaned_tabs(db_path, mode=FLUSH_MODE, **kwargs)


def maybe_cleanup_terminal_job_tabs(
    db_path: str | Path | None,
    job_id: str | None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    if not job_id:
        return None
    try:
        return cleanup_terminal_job_tabs(db_path, job_id, **kwargs)
    except Exception as exc:
        return {
            "ok": False,
            "job_id": job_id,
            "error": f"{type(exc).__name__}: {exc}",
        }


def maybe_sweep_orphaned_tabs(**kwargs: Any) -> dict[str, Any] | None:
    try:
        return sweep_orphaned_tabs(**kwargs)
    except Exception as exc:
        return {"ok": False, "mode": SWEEP_MODE, "error": f"{type(exc).__name__}: {exc}"}


def maybe_flush_orphaned_tabs(**kwargs: Any) -> dict[str, Any] | None:
    try:
        return flush_orphaned_tabs(**kwargs)
    except Exception as exc:
        return {"ok": False, "mode": FLUSH_MODE, "error": f"{type(exc).__name__}: {exc}"}


# Re-export for Playwright attach after cleanup.
select_current_job_page = select_playwright_page
