"""Choose the EZLynx tab the recorder should follow.

Production bug (job 30777947): ``browser_capture`` bound Page.startScreencast
to the first ``ezlynx.com`` tab in CDP enumeration order and never rebound.
That first tab was a stale Policies list while playwright_exec drove documents
then ``/applicantportal/Policy/Actions/Edit/<account>/<policy>`` and FormEntry.
The published webm stayed frozen. This module picks the Playwright-driven tab
or the most recently navigated EZLynx page, not first-ezlynx-wins.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


DEFAULT_RECORDING_ROOT = "/opt/streetsmart-hermes/robie-job-engine/data/recordings"
ACTIVE_HINT_POINTER = "active_hint_path"
ACTIVE_PATH_MARKERS = (
    "/policy/actions/edit/",
    "/applicantportal/",
    "formentry",
    "/documents",
    "/continueedit",
    "/application/",
)
LISTING_PATH_MARKERS = (
    "/policies",
    "/web/policies",
    "/policy/list",
)
PLAYWRIGHT_URL_RE = re.compile(
    r"https?://[^\s'\"\\]+ezlynx[^\s'\"\\]*"
    r"|/applicantportal/[^\s'\"\\]+"
    r"|/Policy/Actions/Edit/\d+/\d+"
    r"|FormEntry",
    re.IGNORECASE,
)
ACCOUNT_ID_RE = re.compile(r"\b(\d{8,})\b")


@dataclass(frozen=True)
class TabCandidate:
    """One Chrome page the recorder can attach to."""

    identity: str
    url: str
    last_navigated_at: float = 0.0
    title: str = ""


def is_ezlynx_url(url: str) -> bool:
    return "ezlynx.com" in str(url or "").casefold()


def normalize_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/").casefold()
    host = (parsed.netloc or "").casefold()
    return f"{host}{path}"


def path_looks_like_listing(url: str) -> bool:
    path = urlparse(str(url or "")).path.casefold()
    if "/policy/actions/edit/" in path or "formentry" in path:
        return False
    return any(marker in path for marker in LISTING_PATH_MARKERS)


def path_looks_like_active_work(url: str) -> bool:
    path = urlparse(str(url or "")).path.casefold()
    return any(marker in path for marker in ACTIVE_PATH_MARKERS)


def work_rank(url: str) -> int:
    """Higher means closer to the live EZLynx edit surface."""
    path = urlparse(str(url or "")).path.casefold()
    if "/policy/actions/edit/" in path or "formentry" in path:
        return 2
    if path_looks_like_active_work(url) and not path_looks_like_listing(url):
        return 1
    return 0


def _hint_matches(url: str, hint_url: str) -> bool:
    left = normalize_url(url)
    right = normalize_url(hint_url)
    if not left or not right:
        return False
    return left == right or left.endswith(right) or right.endswith(left) or right in left or left in right


def select_recording_tab(
    tabs: Iterable[TabCandidate],
    *,
    previous_identity: str | None = None,
    hint_url: str | None = None,
) -> TabCandidate | None:
    """Pick the tab Playwright is driving. Never first-ezlynx-wins."""
    pages = [tab for tab in tabs if tab and str(tab.url or "").strip()]
    if not pages:
        return None
    ezlynx = [tab for tab in pages if is_ezlynx_url(tab.url)]
    pool = ezlynx or [tab for tab in pages if tab.url != "about:blank"] or pages
    if hint_url:
        hinted = [tab for tab in pool if _hint_matches(tab.url, hint_url)]
        hinted_live = [
            tab
            for tab in hinted
            if path_looks_like_active_work(tab.url) and not path_looks_like_listing(tab.url)
        ]
        if hinted_live:
            return max(hinted_live, key=lambda tab: tab.last_navigated_at)
        # A pages[0] / listing hint must not pin the recorder while Edit exists.
        if hinted and not any(path_looks_like_active_work(tab.url) for tab in pool):
            return max(hinted, key=lambda tab: tab.last_navigated_at)
    scored: list[tuple[tuple[Any, ...], TabCandidate]] = []
    for tab in pool:
        score = (
            work_rank(tab.url),
            tab.last_navigated_at,
            0 if path_looks_like_listing(tab.url) else 1,
            1 if previous_identity and tab.identity == previous_identity else 0,
        )
        scored.append((score, tab))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def first_ezlynx_wins(
    tabs: Iterable[TabCandidate],
    *,
    previous_identity: str | None = None,
    hint_url: str | None = None,
) -> TabCandidate | None:
    """The 30777947 attach. First ``ezlynx.com`` URL in enumeration order.

    Capture must not call this. Tests use it to prove the old bind stays on
    the stale Policies tab while Playwright drives another page.
    """
    del previous_identity, hint_url
    for tab in tabs:
        if tab and is_ezlynx_url(tab.url):
            return tab
    return None


def follow_screencast_frames(
    ticks: Iterable[Iterable[TabCandidate]],
    snapshot: Callable[[TabCandidate], bytes],
    *,
    selector: Callable[..., TabCandidate | None] | None = None,
) -> dict[str, Any]:
    """Bind / rebind Page.startScreencast the same way capture does.

    Each tick is the live tab list. ``snapshot`` is one encoded frame of the
    bound page. Selector defaults to ``select_recording_tab``.
    """
    pick = selector or select_recording_tab
    frames: list[bytes] = []
    attached: list[str] = []
    previous: str | None = None
    rebinds = 0
    last_url = ""
    for listed in ticks:
        tabs = [tab for tab in listed if tab]
        chosen = pick(tabs, previous_identity=previous)
        if chosen is None:
            continue
        if previous is not None and chosen.identity != previous:
            rebinds += 1
        if previous != chosen.identity or chosen.url not in attached:
            attached.append(chosen.url)
            previous = chosen.identity
        last_url = chosen.url
        frames.append(snapshot(chosen))
    return {
        "frames": frames,
        "attached_urls": attached,
        "rebinds": rebinds,
        "initial_url": attached[0] if attached else "",
        "final_url": last_url,
    }


def recording_root() -> Path:
    return Path(os.environ.get("ROBIE_RECORDING_ROOT") or DEFAULT_RECORDING_ROOT)


def publish_active_hint_pointer(hint_path: str | Path, *, root: str | Path | None = None) -> Path:
    """Write a filesystem pointer the .hermes Playwright tool can find.

    Job Engine and hermes-gateway do not share os.environ. Capture already
    knows ``--hint-file``; playwright_exec only sees this pointer or
    ``ROBIE_RECORDING_HINT_FILE``.
    """
    base = Path(root) if root is not None else recording_root()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    pointer = base / ACTIVE_HINT_POINTER
    pointer.write_text(str(Path(hint_path)), encoding="utf-8")
    return pointer


def resolve_hint_file(*, root: str | Path | None = None) -> Path | None:
    """Find the live Job's hint file without requiring versions/latest-style env."""
    env_path = os.environ.get("ROBIE_RECORDING_HINT_FILE", "").strip()
    if env_path:
        return Path(env_path)
    base = Path(root) if root is not None else recording_root()
    pointer = base / ACTIVE_HINT_POINTER
    if pointer.is_file():
        text = pointer.read_text(encoding="utf-8").strip()
        if text:
            return Path(text)
    return None


def read_page_hint(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    hint = Path(path)
    if not hint.is_file():
        env_path = os.environ.get("ROBIE_RECORDING_HINT_FILE", "").strip()
        hint = Path(env_path) if env_path else hint
    if not hint.is_file():
        return None
    try:
        data = json.loads(hint.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    url = str(data.get("url") or "").strip()
    if not url:
        return None
    return data


def write_page_hint(path: str | Path, *, url: str, job_id: str | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"url": url, "job_id": job_id or os.environ.get("ROBIE_RECORDING_JOB_ID") or ""}
    target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def attach_log_path(recording_path: str | Path | None) -> Path | None:
    if not recording_path:
        return None
    return Path(recording_path).with_suffix(".attach.json")


def write_attach_log(recording_path: str | Path, payload: dict[str, Any]) -> Path:
    target = attach_log_path(recording_path)
    if target is None:
        raise ValueError("recording path is required")
    target.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
    return target


def load_attach_log(recording_path: str | Path | None) -> dict[str, Any] | None:
    target = attach_log_path(recording_path)
    if target is None or not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def extract_playwright_urls(text: str) -> list[str]:
    found = [match.group(0).rstrip(").,;\"'") for match in PLAYWRIGHT_URL_RE.finditer(str(text or ""))]
    return list(dict.fromkeys(found))


def extract_account_ids(text: str) -> set[str]:
    return set(ACCOUNT_ID_RE.findall(str(text or "")))


def recorder_tab_mismatch(
    attach: dict[str, Any] | None,
    playwright_text: str,
) -> dict[str, Any]:
    """Return FAIL/MISMATCH when the recorder stayed on a different EZLynx tab."""
    urls = extract_playwright_urls(playwright_text)
    ids = extract_account_ids(playwright_text)
    playwright_blob = " ".join(urls) + " " + str(playwright_text or "")
    playwright_active = any(
        path_looks_like_active_work(item) for item in urls
    ) or any(
        marker in playwright_blob.casefold()
        for marker in ("formentry", "policy/actions/edit", "applicantportal", "save and continue")
    )
    if attach is None:
        if playwright_active or urls:
            return {
                "result": "UNKNOWN",
                "reason": "missing recorder attach log; cannot prove the captured tab",
                "playwright_urls": urls,
                "playwright_account_ids": sorted(ids),
            }
        return {
            "result": "UNKNOWN",
            "reason": "missing recorder attach log",
            "playwright_urls": urls,
            "playwright_account_ids": sorted(ids),
        }
    attached = [
        str(item)
        for item in (
            list(attach.get("attached_urls") or [])
            + [attach.get("initial_url") or "", attach.get("final_url") or ""]
        )
        if item
    ]
    if not attached:
        return {
            "result": "UNKNOWN",
            "reason": "recorder attach log has no URL",
            "playwright_urls": urls,
        }
    attached_blob = " ".join(attached)
    attached_listing = all(path_looks_like_listing(url) for url in attached) or (
        any(path_looks_like_listing(url) for url in attached)
        and not any(path_looks_like_active_work(url) for url in attached)
    )
    id_on_playwright = bool(ids) and any(account in playwright_blob for account in ids)
    id_on_recording = bool(ids) and any(account in attached_blob for account in ids)
    playwright_edit = any(path_looks_like_active_work(item) for item in urls) or playwright_active
    if playwright_edit and attached_listing:
        return {
            "result": "MISMATCH",
            "reason": (
                "recorder attached to a different tab than the playwright page "
                "(stale listing vs live Edit/FormEntry/documents)"
            ),
            "attached_urls": attached,
            "playwright_urls": urls,
            "playwright_account_ids": sorted(ids),
        }
    if id_on_playwright and not id_on_recording and attached_listing:
        return {
            "result": "MISMATCH",
            "reason": (
                "recorder attached to a different tab than the playwright page "
                "(account id in tool results, not in captured URL)"
            ),
            "attached_urls": attached,
            "playwright_urls": urls,
            "playwright_account_ids": sorted(ids),
        }
    return {
        "result": "MATCH",
        "reason": "recorder attach URLs are consistent with playwright_exec targets",
        "attached_urls": attached,
        "playwright_urls": urls,
        "playwright_account_ids": sorted(ids),
    }
