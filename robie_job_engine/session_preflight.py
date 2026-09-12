#!/usr/bin/env python3
"""Is the EZLynx browser session usable? Answer before a job runs, not after.

On 2026-09-11 three jobs in a row failed against a Chrome whose EZLynx session
had expired. The engine already recorded the evidence — the `cdp_tabs_start`
checkpoint captured the one tab sitting on /auth/account/login — and then ran
the worker anyway. The worker, with no session and no credential path it knew
how to reach, hand-wrote a login with a placeholder password. Five hours of
failures produced three reports, none of which said "logged out".

This module turns that already-captured evidence into a decision.

Design rule, and the reason this is short: only a PROVABLE negative stops a
job. A tab parked on the EZLynx login URL is proof of logged out. Nothing
readable from a tab list is proof of logged IN — a session cookie can be dead
while the URL still looks fine. So LOGGED_OUT blocks, and every other state
proceeds and is recorded. A pre-flight that guesses optimistically wastes a
job; one that guesses pessimistically blocks real work. This one only refuses
when it can show its work.

`classify` is pure and takes the parsed CDP /json/list payload, so it is
testable without a browser, a network, or a box.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterable, Mapping

DEFAULT_CDP_URL = "http://127.0.0.1:9222"

EZLYNX_HOST = "app.ezlynx.com"
LOGIN_PATH = "/auth/account/login"

# States. Only LOGGED_OUT is a provable negative.
LOGGED_OUT = "LOGGED_OUT"
SESSION_PRESENT = "SESSION_PRESENT"
NO_EZLYNX_TAB = "NO_EZLYNX_TAB"
NO_TABS = "NO_TABS"
UNREACHABLE = "UNREACHABLE"

# States that must stop a job before the worker is invoked.
BLOCKING_STATES = frozenset({LOGGED_OUT})

BLOCKER_MESSAGE = (
    "SESSION_LOGGED_OUT: the EZLynx browser session on this host is logged out. "
    "No policy work can run. Re-authenticate through the credential path in "
    "Secret Manager. Do not compose a login."
)


def _pages(tabs: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Real web pages only.

    A Chrome tab list also carries `browser_ui` entries (omnibox popups and the
    like). Counting those as tabs is how "the browser has tabs" becomes true
    for a browser showing nothing.
    """
    out = []
    for tab in tabs or ():
        if not isinstance(tab, Mapping):
            continue
        if str(tab.get("type") or "") != "page":
            continue
        out.append(tab)
    return out


def _is_ezlynx(url: str) -> bool:
    return EZLYNX_HOST in url


def _is_login(tab: Mapping[str, Any]) -> bool:
    url = str(tab.get("url") or "")
    if LOGIN_PATH in url:
        return True
    # Title alone is not proof — a page called "Login" on another host is not
    # our login page — so require the host too.
    title = str(tab.get("title") or "").strip().lower()
    return title == "login" and _is_ezlynx(url)


def classify(tabs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Decide session state from a CDP /json/list payload.

    Returns a dict with `state`, `blocking`, `reason`, and the evidence the
    decision was made from, so a report can quote it rather than assert it.
    """
    pages = _pages(tabs)
    evidence = [
        {"id": p.get("id"), "title": p.get("title"), "url": p.get("url")}
        for p in pages
    ]

    if not pages:
        return {
            "state": NO_TABS,
            "blocking": False,
            "reason": "Chrome is running but has no page tabs.",
            "pages": evidence,
        }

    ezlynx_pages = [p for p in pages if _is_ezlynx(str(p.get("url") or ""))]
    if not ezlynx_pages:
        return {
            "state": NO_EZLYNX_TAB,
            "blocking": False,
            "reason": "No EZLynx tab open; session state cannot be read from the tab list.",
            "pages": evidence,
        }

    login_pages = [p for p in ezlynx_pages if _is_login(p)]
    if len(login_pages) == len(ezlynx_pages):
        # Every EZLynx tab is the login page. That is proof.
        return {
            "state": LOGGED_OUT,
            "blocking": True,
            "reason": BLOCKER_MESSAGE,
            "pages": evidence,
            "login_urls": [str(p.get("url") or "") for p in login_pages],
        }

    return {
        "state": SESSION_PRESENT,
        "blocking": False,
        "reason": (
            "An EZLynx tab is not on the login page. This is not proof the session "
            "is valid — only that it is not provably dead."
        ),
        "pages": evidence,
    }


def read_tabs(cdp_url: str = DEFAULT_CDP_URL, timeout: float = 5.0) -> list[Mapping[str, Any]]:
    """Fetch CDP /json/list. Raises OSError/ValueError on failure."""
    url = cdp_url.rstrip("/") + "/json/list"
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed localhost URL
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"CDP /json/list returned {type(payload).__name__}, expected list")
    return payload


def check(cdp_url: str = DEFAULT_CDP_URL, timeout: float = 5.0) -> dict[str, Any]:
    """Read the browser and classify. Never raises.

    An unreachable CDP endpoint is NOT reported as blocking: that is a
    different failure with a different fix, and mislabelling it as a logged-out
    session would send the next person to the wrong place.
    """
    try:
        tabs = read_tabs(cdp_url, timeout=timeout)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {
            "state": UNREACHABLE,
            "blocking": False,
            "reason": f"Could not read CDP at {cdp_url}: {exc.__class__.__name__}: {exc}",
            "pages": [],
            "cdp_url": cdp_url,
        }
    result = classify(tabs)
    result["cdp_url"] = cdp_url
    return result
