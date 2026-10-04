#!/usr/bin/env python3
"""Supervised, read-only inspection of the EZLynx task fields, to establish the field contract.

PURPOSE: record what is ACTUALLY on the page and in the discussion record so a person can
declare each consequential field (and what it means) in
`deploy/ezlynx_task_field_contract.json`. This module names no selector and infers no meaning.

READ-ONLY and NARROW, by construction:
- It never saves, reassigns, posts a note, places a call, creates a job or runs the intake. (A
  test asserts the source contains none of those calls.)
- DOM capture is two-phase. Phase 1 returns METADATA ONLY (label, role, type, visibility
  flags); it never reads a value. Python then excludes every hidden, invisible or credential-like
  control. Phase 2 reads values ONLY for controls whose label matches an approved task-field
  pattern, and the page script re-derives the actual element's identity, visibility and
  credential status BEFORE it touches any value; a changed element is rejected unread. No
  neighbouring or sibling element is ever read.
- Everything else is recorded as names only (dialog) or not at all (account page).
- API discovery records key names and TYPES only by default, never values, and only after the
  discussion is verified to belong to the applicant.
- It refuses unless it is supervised and exclusive: Test VM, ROBIE_ENV=TEST, applicant 220250093,
  exactly one task ID in ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS, reassignment and live calls off, no
  active jobs or leases, no other client's tab open, an operator named, browser ownership and
  exclusivity confirmed by that operator, and Test ALREADY holding a valid shared driver lease
  (the repo's gate is only checked, never changed: it does not obtain or renew the lease). The
  profile lock is held for the run. It STOPS on an unexpected page or a Cancel that cannot be verified.
- The output file is created exclusively (never overwritten), mode 0600.

It is NOT run by CI or by any scheduler. See docs/EZLYNX_TASK_FIELD_DISCOVERY.md.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from typing import Any

from . import ezlynx_task_cdp as cdp
from .task_discussion_route import DiscussionRouteRefused, assert_no_inherited_job_context, safe_failure

TEST_ACCOUNT = "220250093"
TEST_HOST_PREFIX = "hermes-test-01"
ALLOWED_TASK_IDS_ENV = "ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS"
MAX_ELEMENTS = 400
MAX_VALUE_CHARS = 200

# CAPTURE SCOPE, NOT SELECTORS. These patterns only decide which already-visible, non-credential
# controls may have their VALUE recorded. They are never used to read a field and never become a
# contract entry; a person declares the contract after reviewing the observation.
APPROVED_FIELD_PATTERNS = {
    "description": re.compile(r"\b(descri\w*|instruct\w*|task note|details?)\b", re.I),
    "created_by": re.compile(r"\bcreated?\b", re.I),
    "assigned_producer": re.compile(r"\bproducer\b", re.I),
    "csr": re.compile(r"\b(csr|customer service|service rep\w*|account manager)\b", re.I),
    "activity_labels": re.compile(r"\b(labels?|tags?)\b", re.I),
    "assignee": re.compile(r"\bassign\w*\b", re.I),
}
CREDENTIAL_RE = re.compile(
    r"pass(word|code|phrase)?|secret|token|otp|one[- ]?time|mfa|2fa|verification|security (answer|question)|"
    r"\bssn\b|social security|tax ?id|\bein\b|card|cvv|cvc|routing|account number|\bpin\b|api[- ]?key|credential|login",
    re.I)
CREDENTIAL_TYPES = {"password", "file"}

# Both phases are pure reads of the page. Phase 1 returns metadata only: it never touches a value.
_COMMON = """
  const SELECTOR = 'input, textarea, select, [role], [aria-label], label, [contenteditable="true"]';
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\\s+/).map(i => { const n = document.getElementById(i); return n ? clean(n.textContent) : ''; }).join(' ').trim(); if (t) return t; }
    if (el.labels && el.labels.length) return Array.from(el.labels).map(l => clean(l.textContent)).join(' ');
    if (el.tagName.toLowerCase() === 'label') return clean(el.textContent).slice(0, 120);
    return clean(el.getAttribute('placeholder') || el.getAttribute('title') || '');
  };
  const els = Array.from(root.querySelectorAll(SELECTOR)).slice(0, %(cap)d);
""" % {"cap": MAX_ELEMENTS}

_DESCRIBE_BODY = _COMMON + """
  return els.map((el, index) => {
    const style = window.getComputedStyle(el);
    const tag = el.tagName.toLowerCase();
    return {
      index, tag, type: el.getAttribute('type') || '', role: el.getAttribute('role') || '',
      autocomplete: el.getAttribute('autocomplete') || '', name: nameOf(el),
      id_hint: el.id || '', class_hint: (el.className && el.className.baseVal === undefined) ? String(el.className) : '',
      aria_hidden: el.getAttribute('aria-hidden') || '', hidden: !!el.hidden,
      display: style.display, visibility: style.visibility,
      in_view: !!(el.offsetParent !== null || el.getClientRects().length),
      editable: ['input', 'textarea', 'select'].includes(tag) ? !(el.readOnly || el.disabled) : el.isContentEditable === true,
    };
  });
"""

_READ_BODY = _COMMON + """
  const CREDENTIAL = new RegExp(%(cred)s, 'i');
  const CREDENTIAL_TYPES = %(types)s;
  const CREDENTIAL_DESCENDANT = 'input[type="password"], input[type="file"], input[autocomplete="one-time-code"], input[autocomplete*="password"]';
  const wanted = Array.isArray(arg) ? arg : [];
  // The second stage trusts nothing from the first. For the ACTUAL element at this index it
  // re-derives identity, visibility and credential status from attributes and styles ONLY, and
  // touches value / innerText / textContent only after every check has passed.
  return wanted.map((item) => {
    const reject = (why) => ({ index: item.index, name: '', value: '', rejected: why });
    const el = els[item.index];
    if (!el) return reject('changed: element is gone');
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type') || '';
    const role = el.getAttribute('role') || '';
    const name = nameOf(el);
    if (name !== item.name || tag !== item.tag || type !== (item.type || '') || role !== (item.role || '')) {
      return reject('changed: identity differs from the description stage');
    }
    const style = window.getComputedStyle(el);
    const ancestorHidden = typeof el.closest === 'function' && !!el.closest('[aria-hidden="true"]');
    if (type.toLowerCase() === 'hidden' || el.hidden === true || ancestorHidden
        || (el.getAttribute('aria-hidden') || '').toLowerCase() === 'true'
        || style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse'
        || !(el.offsetParent !== null || el.getClientRects().length)) {
      return reject('hidden_or_invisible');
    }
    const haystack = [name, el.id || '', (el.className && el.className.baseVal === undefined) ? String(el.className) : '',
                      el.getAttribute('autocomplete') || '', type].join(' ');
    if (CREDENTIAL_TYPES.includes(type.toLowerCase()) || CREDENTIAL.test(haystack)
        || (tag !== 'input' && el.querySelector(CREDENTIAL_DESCENDANT))) {
      return reject('credential_like');
    }
    const isInput = ['input', 'textarea', 'select'].includes(tag);
    const value = isInput ? String(el.value || '') : clean(el.innerText);
    return { index: item.index, name, value: value.slice(0, %(max)d), rejected: '' };
  });
""" % {"max": MAX_VALUE_CHARS, "cred": json.dumps(CREDENTIAL_RE.pattern),
       "types": json.dumps(sorted(CREDENTIAL_TYPES))}

DESCRIBE_JS_PAGE = "(arg) => { const root = document; " + _DESCRIBE_BODY + " }"
DESCRIBE_JS_ELEMENT = "(root, arg) => { " + _DESCRIBE_BODY + " }"
READ_JS_PAGE = "(arg) => { const root = document; " + _READ_BODY + " }"
READ_JS_ELEMENT = "(root, arg) => { " + _READ_BODY + " }"


class InspectionRefused(Exception):
    """A precondition for a safe, supervised, read-only Test inspection does not hold."""


class InspectionAborted(Exception):
    """The inspection stopped because something unexpected happened. Nothing was changed."""


class CancelFailed(InspectionAborted):
    """The Edit dialog could not be verified closed. Stop; close it by hand WITHOUT saving."""


# ---------------------------------------------------------------- environment seams (patched in tests)

def _hostname() -> str:
    return socket.gethostname()


@contextmanager
def _exclusive_session(**kwargs: Any):
    """The repo's profile lock. exclusive_session also CHECKS the driver lease; it does not obtain or renew it."""
    from .ezlynx_session_lock import exclusive_session

    with exclusive_session(**kwargs):
        yield


def _driver_gate_status() -> dict[str, Any]:
    """The shared driver lease as the repo's own gate reads it. READ ONLY: it never writes metadata."""
    from .ezlynx_driver_gate import check_driver_gate

    decision = check_driver_gate()
    return {"allowed": bool(decision.allowed), "holder": decision.holder, "reason": decision.reason}


def _job_db_path() -> str:
    return os.environ.get("ROBIE_JOB_DB", "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db")


def _job_inventory(db_path: str | None = None) -> list[dict[str, Any]]:
    """Active jobs or held leases, read-only (the same inventory the Test deploy takes)."""
    path = db_path or _job_db_path()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, status, lease_owner FROM jobs "
                "WHERE status IN ('RUNNING','VERIFYING') OR lease_owner IS NOT NULL").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise InspectionRefused(f"cannot verify exclusivity: the job database is unreadable ({type(exc).__name__})") from exc
    return [dict(row) for row in rows]


def _list_browser_tabs() -> list[dict[str, str]]:
    from urllib.parse import urlparse

    from .tab_cleanup import list_cdp_tabs

    host = urlparse(cdp.CDP_URL).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise InspectionRefused(f"the browser is not local to this VM ({host!r}); ownership cannot be confirmed")
    try:
        tabs = list_cdp_tabs(cdp_url=cdp.CDP_URL)
    except Exception as exc:  # noqa: BLE001
        raise InspectionRefused(f"cannot list the browser's tabs ({type(exc).__name__})") from exc
    return [{"url": str(getattr(tab, "url", "")), "title": str(getattr(tab, "title", ""))} for tab in tabs]


# ---------------------------------------------------------------- preconditions

def _listed_task_ids() -> set[str]:
    return {part.strip() for part in os.environ.get(ALLOWED_TASK_IDS_ENV, "").split(",")
            if re.fullmatch(r"[1-9][0-9]*", part.strip())}


def preflight(*, task_id: str, applicant_id: str, operator: str, confirm_browser_owner: bool = False,
              confirm_exclusive: bool = False, jobs_db_path: str | None = None, browser: bool = True) -> dict[str, Any]:
    """Every check that must pass BEFORE anything is opened. Raises InspectionRefused."""
    if os.environ.get("ROBIE_ENV", "").strip().upper() != "TEST":
        raise InspectionRefused("the inspection runs only with ROBIE_ENV=TEST")
    if str(applicant_id).strip() != TEST_ACCOUNT:
        raise InspectionRefused(f"the inspection is limited to the Test account {TEST_ACCOUNT}")
    listed = _listed_task_ids()
    if listed != {str(task_id).strip()}:
        raise InspectionRefused(f"{ALLOWED_TASK_IDS_ENV} must list exactly this one task ({task_id}); found {sorted(listed)}")
    if os.environ.get(cdp.REASSIGN_GATE_ENV, "").strip() == "1":
        raise InspectionRefused("reassignment is enabled; it must be off during discovery")
    if os.environ.get("ROBIE_PHONE_LIVE_CALLS", "").strip() == "1":
        raise InspectionRefused("live calls are enabled; they must be off during discovery")
    host = _hostname()
    if not host.startswith(TEST_HOST_PREFIX):
        raise InspectionRefused(f"this is not the Test VM ({host!r}); run it on {TEST_HOST_PREFIX}")
    if not str(operator or "").strip():
        raise InspectionRefused("a named operator must supervise the inspection")
    record: dict[str, Any] = {"operator": str(operator).strip(), "host": host,
                              "intake_reassignment_calls": "off (verified from the environment)",
                              "allowed_task_ids": sorted(listed)}
    if browser:
        if not confirm_browser_owner:
            raise InspectionRefused("the operator has not confirmed browser ownership")
        if not confirm_exclusive:
            raise InspectionRefused("the operator has not confirmed exclusive use of the browser")
        active = _job_inventory(jobs_db_path)
        if active:
            raise InspectionRefused(f"{len(active)} job(s) or lease(s) are active; the browser is not exclusive")
        lease = _driver_gate_status()
        if not (lease.get("allowed") is True and lease.get("holder") == "TEST" and lease.get("reason") == "driver is IN"):
            raise InspectionRefused(
                f"Test does not already hold a valid shared driver lease ({lease.get('reason')}; holder {lease.get('holder')}). "
                "The inspection only checks the lease; it does not obtain or renew it")
        tabs = _list_browser_tabs()
        foreign = [t["url"] for t in tabs
                   if re.search(r"/web/account/(\d+)", t["url"]) and f"/web/account/{TEST_ACCOUNT}" not in t["url"]]
        if foreign:
            raise InspectionRefused(f"another client's account is open in the browser: {foreign}")
        record.update({"active_jobs_or_leases": 0, "open_tabs": tabs, "driver_lease": lease,
                       "browser_ownership_confirmed_by_operator": True,
                       "exclusive_use_confirmed_by_operator": True})
    return record


# ---------------------------------------------------------------- classification (decided before any value is read)

def exclusion_reason(meta: dict[str, Any]) -> str | None:
    """Why this control must never have its value read; None when it may be considered."""
    haystack = " ".join(str(meta.get(key) or "") for key in ("name", "id_hint", "class_hint", "autocomplete", "type"))
    if str(meta.get("type") or "").lower() in CREDENTIAL_TYPES or CREDENTIAL_RE.search(haystack):
        return "credential_like"
    hidden = (str(meta.get("type") or "").lower() == "hidden" or meta.get("hidden") is True
              or str(meta.get("aria_hidden") or "").lower() == "true"
              or str(meta.get("display") or "").lower() == "none"
              or str(meta.get("visibility") or "").lower() in ("hidden", "collapse")
              or meta.get("in_view") is False)
    return "hidden_or_invisible" if hidden else None


def approved_field_hint(meta: dict[str, Any]) -> str | None:
    name = str(meta.get("name") or "")
    for field, pattern in APPROVED_FIELD_PATTERNS.items():
        if pattern.search(name):
            return field
    return None


def _capture(scope: Any, scope_name: str, *, inventory: bool, excluded: dict[str, int]) -> tuple[list, list]:
    """Metadata first; values only for approved, visible, non-credential controls."""
    is_page = hasattr(scope, "goto") or scope_name == "page"
    describe = DESCRIBE_JS_PAGE if is_page else DESCRIBE_JS_ELEMENT
    read = READ_JS_PAGE if is_page else READ_JS_ELEMENT
    metas = list(scope.evaluate(describe))
    controls: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []
    for meta in metas:
        reason = exclusion_reason(meta)
        if reason:
            excluded[reason] += 1
            continue
        hint = approved_field_hint(meta)
        if inventory:
            controls.append({"tag": meta.get("tag"), "role": meta.get("role"), "type": meta.get("type"),
                             "name": meta.get("name"), "editable": meta.get("editable")})
        if hint:
            approved.append({"meta": meta, "hint": hint})
        elif not inventory:
            excluded["unapproved_page_elements"] += 1
    fields: list[dict[str, Any]] = []
    if approved:
        request = [{"index": item["meta"]["index"], "name": item["meta"].get("name"), "tag": item["meta"].get("tag"),
                    "type": item["meta"].get("type"), "role": item["meta"].get("role")} for item in approved]
        rows = {row.get("index"): row for row in scope.evaluate(read, request)}
        for item in approved:
            meta, row = item["meta"], rows.get(item["meta"]["index"]) or {}
            if row.get("rejected") or row.get("name") != meta.get("name"):
                # the page re-checked the live element and refused it (or it moved): nothing was read
                excluded["revalidation_failed"] += 1
                continue
            fields.append({"scope": scope_name, "field_hint": item["hint"], "name": meta.get("name"),
                           "role": meta.get("role"), "editable": meta.get("editable"),
                           "value": str(row.get("value") or "")[:MAX_VALUE_CHARS]})
    return controls, fields


# ---------------------------------------------------------------- output

def _meaning_review() -> dict[str, Any]:
    return {
        "status": "PENDING HUMAN REVIEW",
        "instructions": ("For each required field, a person identifies which observed element carries it, "
                         "what it MEANS, and which report column it corresponds to. Nothing here is inferred."),
        "fields": {name: None for name in cdp.REQUIRED_FIELDS},
    }


def _write_exclusive(output_path: Any, record: dict[str, Any]) -> None:
    fd = os.open(str(output_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")


# ---------------------------------------------------------------- DOM inspection

def _expected_url(applicant_id: str) -> str:
    return cdp._activity_url(applicant_id).rstrip("/")


def _assert_expected_page(page: Any, applicant_id: str) -> None:
    url = str(getattr(page, "url", "") or "").rstrip("/")
    if url != _expected_url(applicant_id):
        raise InspectionAborted(f"unexpected page: {url or 'unknown'} (expected {_expected_url(applicant_id)})")


def _cancel_and_verify(page: Any, panel: Any) -> None:
    try:
        cdp._cancel_dialog(panel)
    except Exception as exc:  # noqa: BLE001
        raise CancelFailed(f"cancel failed: the Cancel control could not be used ({type(exc).__name__})") from exc
    if page.get_by_role("dialog", name="Edit Task", exact=True).count() != 0:
        raise CancelFailed("cancel failed: the Edit Task dialog is still open")


def run_dom_inspection(*, task_id: str, applicant_id: str, output_path: Any, operator: str,
                       confirm_browser_owner: bool = False, confirm_exclusive: bool = False,
                       jobs_db_path: str | None = None, dialog_inventory: bool = True) -> dict[str, Any]:
    """Open the task's Edit dialog, record approved fields, then Cancel and VERIFY it closed.

    Never saves. Stops on an unexpected page or a Cancel that cannot be verified, recording why.
    `dialog_inventory=False` records the approved fields only (no names-only list of other controls).
    """
    pre = preflight(task_id=task_id, applicant_id=applicant_id, operator=operator,
                    confirm_browser_owner=confirm_browser_owner, confirm_exclusive=confirm_exclusive,
                    jobs_db_path=jobs_db_path, browser=True)
    excluded = {"hidden_or_invisible": 0, "credential_like": 0, "unapproved_page_elements": 0,
                "revalidation_failed": 0}
    record: dict[str, Any] = {
        "kind": "dom", "environment": "TEST", "applicant_id": str(applicant_id), "task_id": str(task_id),
        "observed_at": datetime.now(timezone.utc).isoformat(), "preflight": pre,
        "dialog_controls": [], "approved_fields": [], "excluded": excluded,
        "cancel_verified": None, "stopped": None,
        "note": "Observation only. No selector was guessed and no field was mapped.",
        "meaning_review": _meaning_review(),
    }
    stack = ExitStack()
    try:
        try:
            stack.enter_context(_exclusive_session(timeout_seconds=30))
        except Exception as exc:  # noqa: BLE001 — a held lock or a driver lease we do not own
            raise InspectionRefused(f"the profile lock or the driver-lease check refused ({type(exc).__name__})") from exc
        with stack:
            with cdp._browser_page() as page:
                try:
                    cdp._goto_activity(page, applicant_id)
                except Exception as exc:  # noqa: BLE001 — a login page, an expired session or a load failure
                    raise InspectionAborted(f"unexpected page: could not reach the activity page ({type(exc).__name__})") from exc
                _assert_expected_page(page, applicant_id)
                try:
                    panel = cdp._search_and_open_edit(page, task_id, applicant_id)
                except Exception as exc:  # noqa: BLE001 — wrong page, missing or ambiguous row or dialog
                    raise InspectionAborted(f"unexpected page or dialog ({type(exc).__name__})") from exc
                try:
                    controls, fields = _capture(panel, "dialog", inventory=dialog_inventory, excluded=excluded)
                    record["dialog_controls"], record["approved_fields"] = controls, fields
                finally:
                    _cancel_and_verify(page, panel)
                record["cancel_verified"] = True
                _assert_expected_page(page, applicant_id)
                _, page_fields = _capture(page, "page", inventory=False, excluded=excluded)
                record["approved_fields"] = record["approved_fields"] + page_fields
    except CancelFailed as exc:
        record["cancel_verified"] = False
        record["stopped"] = {"reason": str(exc)}
        _write_exclusive(output_path, record)
        raise
    except InspectionAborted as exc:
        record["stopped"] = {"reason": str(exc)}
        _write_exclusive(output_path, record)
        raise
    _write_exclusive(output_path, record)
    return record


# ---------------------------------------------------------------- API inspection (keys and types only)

def _key_types(value: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    types: dict[str, str] = {}
    if isinstance(value, dict) and depth < 4:
        for key, inner in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(inner, dict):
                types.update(_key_types(inner, path, depth + 1))
            elif isinstance(inner, list):
                types[path] = "list"
                for item in inner[:20]:
                    types.update(_key_types(item, path, depth + 1))
            else:
                types[path] = "null" if inner is None else type(inner).__name__
    return types


def _approved_values(note: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(note, dict):
        for key, inner in note.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(inner, dict):
                out.update(_approved_values(inner, path))
            elif isinstance(inner, (str, int, float)) and not isinstance(inner, bool):
                probe = {"name": str(key)}
                if approved_field_hint(probe) or str(key).lower() in ("body", "text", "description"):
                    if not CREDENTIAL_RE.search(path):
                        out[path] = str(inner)[:MAX_VALUE_CHARS]
    return out


def run_api_inspection(*, client: Any, task_id: str, applicant_id: str, discussion_id: str,
                       output_path: Any, operator: str, expected_route: str,
                       include_approved_values: bool = False) -> dict[str, Any]:
    """Read ONE discussion and record key names and TYPES (values only on explicit opt-in, and then
    only for approved, non-credential keys). Ownership must be VERIFIED first; otherwise nothing is read."""
    pre = preflight(task_id=task_id, applicant_id=applicant_id, operator=operator, browser=False)
    try:
        assert_no_inherited_job_context()
    except DiscussionRouteRefused as exc:
        raise InspectionRefused(f"{exc}; nothing was read") from exc
    route = getattr(client, "route_record", None)
    if not isinstance(route, dict) or route.get("route") != expected_route:
        raise InspectionRefused(
            f"the client's Discussion API route is {route.get('route') if isinstance(route, dict) else 'unknown'!r}, "
            f"not the expected {expected_route!r}; nothing was read")
    if route.get("browser_cookies") is not False:
        raise InspectionRefused("the client can read browser cookies; the read-only inspection requires a cookie-free client; nothing was read")
    lookup = getattr(client, "get_discussion_ids", None)
    if lookup is None:
        raise InspectionRefused("the client cannot verify which discussions belong to the applicant; nothing was read")
    try:
        owned = [str(item).strip() for item in lookup(str(applicant_id))]
    except Exception as exc:  # noqa: BLE001
        raise InspectionRefused(f"ownership could not be verified ({safe_failure(exc)}); nothing was read") from exc
    if str(discussion_id).strip() not in owned:
        raise InspectionRefused(f"discussion {discussion_id} is not one of applicant {applicant_id}'s; nothing was read")
    from .ezlynx_discussions import iter_discussion_notes

    record_in = client.get_discussion(str(discussion_id).strip())
    notes = list(iter_discussion_notes(record_in))
    keys: dict[str, str] = {}
    for note in notes:
        keys.update(_key_types(note))
    record: dict[str, Any] = {
        "kind": "api", "environment": "TEST", "applicant_id": str(applicant_id), "task_id": str(task_id),
        "discussion_id": str(discussion_id), "discussion_ownership": "verified", "discussion_route": dict(route),
        "observed_at": datetime.now(timezone.utc).isoformat(), "preflight": pre,
        "discussion_keys": _key_types(record_in) if isinstance(record_in, dict) else {},
        "note_count": len(notes), "note_keys": dict(sorted(keys.items())),
        "values_included": bool(include_approved_values),
        "note": "Observation only. Key names and types; no key was assumed to mean anything.",
        "meaning_review": _meaning_review(),
    }
    if include_approved_values and notes:
        record["approved_values"] = _approved_values(notes[0])
    _write_exclusive(output_path, record)
    return record
