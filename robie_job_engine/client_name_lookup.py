"""Read-only client lookup by name.

Typing into EZLynx global search is not a write. One matching client is
bound onto the job. More than one match asks a plain question. The write
allowlist is unchanged for every other control.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable
from urllib.parse import urlparse

_LOOKUP = re.compile(
    r"\b(?:policy\s+numbers?|carriers?)\b",
    re.IGNORECASE,
)
_WRITE = re.compile(
    r"\b(?:change|update|add|delete|remove|set|file|bind|cancel|endorse)\b",
    re.IGNORECASE,
)
_NAME = re.compile(
    r"\bfor\s+([A-Za-z][A-Za-z' .-]{1,80}?)\s*[.?!]?$",
    re.IGNORECASE,
)


_SEARCH_BOX = re.compile(
    r"applicantsearch"
    r"|(?:^|[#.\[\]\s\"'(=])search(?:$|[^a-z0-9])"
    r"|searchbox",
    re.IGNORECASE,
)
_POLICY_FACT = re.compile(r"\bpolicy\s+numbers?\b", re.IGNORECASE)
_AUTH_MARKERS = ("/login", "/signin", "/sign-in")

LOOKUP_MISS = "I couldn't look that up; a CSR should take a look."
APPLICANT_SEARCH_SELECTOR = "input#applicantSearch"
SEARCH_KIND = "client_name_search"
_SEARCHER_OVERRIDE: Callable[[str], dict[str, Any]] | None = None


def is_readonly_client_search(method_name: str, selector: object) -> bool:
    """True for an applicant search box on any page, not a form field.

    The dashboard is not a client account page. Typing a name into its
    search box is still a read.
    """
    method = str(method_name or "").strip().casefold()
    if method not in {"fill", "type", "press_sequentially", "click"}:
        return False
    text = " ".join(str(selector or "").split())
    if not text:
        return False
    compact = text.casefold().replace(" ", "")
    if "applicantsearch" in compact:
        return True
    return _SEARCH_BOX.search(text) is not None


def job_is_named_lookup(job: dict[str, Any] | None) -> bool:
    """True when the original ask is a policy-fact lookup by client name."""
    payload = dict((job or {}).get("payload") or {})
    for key in ("request_text", "text", "prompt", "original_text"):
        if client_name_from_lookup(str(payload.get(key) or "")):
            return True
    return False


def job_is_client_policy_lookup(job: dict[str, Any] | None) -> bool:
    """True when the ask wants one named client's policy number.

    A general question such as which carriers we quote is not this lookup.
    """
    payload = dict((job or {}).get("payload") or {})
    for key in ("request_text", "text", "prompt", "original_text"):
        text = str(payload.get(key) or "")
        if _POLICY_FACT.search(text) and client_name_from_lookup(text):
            return True
    return False


def client_name_from_lookup(text: str) -> str | None:
    """The client named in a policy-fact question, or None when this is a write."""
    raw = " ".join(str(text or "").split())
    if not raw or _WRITE.search(raw) or not _LOOKUP.search(raw):
        return None
    match = _NAME.search(raw)
    if not match:
        return None
    name = " ".join(match.group(1).split()).strip(" .?")
    return name or None


def bind_named_client(
    store: Any,
    job_id: str,
    name: str,
    matches: list[dict[str, Any]],
) -> str:
    """Bind one client and answer. Several matches ask which one."""
    who = " ".join(str(name or "").split())
    titled = who.title() if who else "That client"
    ids: list[str] = []
    for row in matches:
        applicant = str((row or {}).get("applicant_id") or "").strip()
        if applicant and applicant not in ids:
            ids.append(applicant)
    if len(ids) > 1:
        return f"I found more than one {titled}. Which one should I use?"
    if len(ids) != 1:
        return f"I couldn't find a client named {titled}."
    job = store.get_job(job_id)
    payload = dict(job.get("payload") or {})
    payload["applicant_id"] = ids[0]
    if who:
        payload["client_name"] = who
    store.update_payload(job_id, payload)
    chosen = next(
        row for row in matches if str(row.get("applicant_id") or "").strip() == ids[0]
    )
    number = str(chosen.get("policy_number") or "").strip()
    carrier = str(chosen.get("carrier") or "").strip()
    line = str(chosen.get("line") or "GL").strip() or "GL"
    if number and carrier:
        return f"{titled}'s {line} policy number is {number} with {carrier}."
    return f"I found {titled}."


def set_client_name_searcher(searcher: Callable[[str], dict[str, Any]] | None) -> None:
    """Tests inject the search. Production types into the open page."""
    global _SEARCHER_OVERRIDE
    _SEARCHER_OVERRIDE = searcher


def _original_ask(job: dict[str, Any] | None) -> str:
    payload = dict((job or {}).get("payload") or {})
    for key in ("request_text", "text", "prompt", "original_text"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return ""


def _titled_name(name: str) -> str:
    who = " ".join(str(name or "").split())
    return who.title() if who else "That client"


def user_message_applicant(job: dict[str, Any] | None) -> str | None:
    """An applicant id the user wrote in this ask. Docs and memory do not count."""
    from .ezlynx_write_scope import requested_message_applicant

    text = _original_ask(job)
    if not text:
        return None
    return requested_message_applicant({"request_text": text, "text": text})


def _search_note(store: Any, job_id: str) -> dict[str, Any]:
    note = store.get_checkpoint(job_id, SEARCH_KIND) or {}
    return dict(note) if isinstance(note, dict) else {}


def trusted_applicant_ids(store: Any, job: dict[str, Any] | None) -> list[str]:
    """Ids this job may open: the user's message, or this job's search."""
    if not job:
        return []
    found: list[str] = []
    named = user_message_applicant(job)
    if named:
        found.append(named)
    note = _search_note(store, str(job.get("id") or ""))
    if str(note.get("source") or "") == "search":
        for item in note.get("applicant_ids") or []:
            token = str(item or "").strip()
            if token and token not in found:
                found.append(token)
    return found


def _is_auth_url(url: object) -> bool:
    path = urlparse(str(url or "").strip()).path.casefold()
    return any(marker in path for marker in _AUTH_MARKERS)


def _page_url(page: Any) -> str:
    value = getattr(page, "url", "")
    if callable(value):
        try:
            value = value()
        except Exception:
            return ""
    return str(value or "").strip()


def _matches_on_page(page: Any) -> list[dict[str, str]]:
    locator_fn = getattr(page, "locator", None)
    if not callable(locator_fn):
        return []
    try:
        links = locator_fn("a[href*='/web/account/'], a[href*='/applicantportal/']")
        total = int(links.count())
    except Exception:
        return []
    from .ezlynx_write_scope import applicant_id_from_ezlynx_url

    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for index in range(total):
        item = links.nth(index) if hasattr(links, "nth") else links
        href = ""
        getter = getattr(item, "get_attribute", None)
        if callable(getter):
            try:
                href = str(getter("href") or "")
            except Exception:
                href = ""
        applicant = str(applicant_id_from_ezlynx_url(href) or "").strip()
        if not applicant or applicant in seen:
            continue
        seen.add(applicant)
        text = ""
        inner = getattr(item, "inner_text", None)
        if callable(inner):
            try:
                text = " ".join(str(inner() or "").split())
            except Exception:
                text = ""
        found.append({"applicant_id": applicant, "name": text})
    return found


def read_applicant_search(page: Any, name: str) -> dict[str, Any]:
    """Type the name into the open page's applicant box. Do not guess a URL."""
    if _is_auth_url(_page_url(page)):
        return {"status": "sign_in", "matches": []}
    locator_fn = getattr(page, "locator", None)
    if not callable(locator_fn):
        return {"status": "error", "matches": []}
    try:
        box = locator_fn(APPLICANT_SEARCH_SELECTOR)
        if int(box.count()) < 1:
            return {"status": "error", "matches": []}
        box.fill(name)
        press = getattr(box, "press", None)
        if callable(press):
            press("Enter")
    except Exception:
        return {"status": "error", "matches": []}
    if _is_auth_url(_page_url(page)):
        return {"status": "sign_in", "matches": []}
    return {"status": "ok", "matches": _matches_on_page(page)}


def _connect_current_page() -> tuple[Any, Any] | None:
    """The page already open. This never navigates to a guessed EZLynx URL."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    cdp_url = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
    playwright = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=2000)
    except Exception:
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        return None
    pages = [page for context in browser.contexts for page in context.pages]
    ezlynx_pages = [
        page for page in pages if "ezlynx.com" in _page_url(page).casefold()
    ]
    page = (ezlynx_pages or pages or [None])[0]
    if page is None:
        try:
            playwright.stop()
        except Exception:
            pass
        return None
    return page, playwright


def default_searcher(name: str) -> dict[str, Any]:
    opened = _connect_current_page()
    if opened is None:
        return {"status": "error", "matches": []}
    page, playwright = opened
    try:
        return read_applicant_search(page, name)
    except Exception:
        return {"status": "error", "matches": []}
    finally:
        try:
            playwright.stop()
        except Exception:
            pass


def _drop_untrusted_binding(store: Any, job: dict[str, Any]) -> dict[str, Any]:
    payload = dict(job.get("payload") or {})
    trusted = set(trusted_applicant_ids(store, job))
    changed = False
    for key in ("applicant_id", "account_id"):
        current = str(payload.get(key) or "").strip()
        if current and current not in trusted:
            payload.pop(key, None)
            changed = True
    if changed:
        store.update_payload(str(job.get("id") or ""), payload)
    return payload


def _remember_search(
    store: Any,
    job_id: str,
    *,
    source: str,
    applicant_ids: list[str],
    user_line: str = "",
    name: str = "",
    candidates: list[str] | None = None,
) -> None:
    store.checkpoint(
        job_id,
        SEARCH_KIND,
        {
            "resolved": True,
            "source": source,
            "applicant_ids": list(applicant_ids),
            "user_line": user_line,
            "name": name,
            "candidates": list(candidates or []),
        },
    )


def prepare_named_client_lookup(
    store: Any,
    job_id: str,
    *,
    searcher: Callable[[str], dict[str, Any]] | None = None,
) -> str | None:
    """Search by name before the model runs. Return a line when it must not.

    One match binds that search result. Several matches ask which one.
    None says so. An id from docs, a runbook, or an earlier chat is dropped.
    """
    job = store.get_job(job_id)
    if not job_is_client_policy_lookup(job):
        return None
    note = _search_note(store, job_id)
    if note.get("resolved"):
        return str(note.get("user_line") or "").strip() or None
    name = ""
    payload = dict(job.get("payload") or {})
    for key in ("request_text", "text", "prompt", "original_text"):
        name = client_name_from_lookup(str(payload.get(key) or "")) or ""
        if name:
            break
    user_id = user_message_applicant(job)
    if user_id:
        _drop_untrusted_binding(store, job)
        job = store.get_job(job_id)
        payload = dict(job.get("payload") or {})
        payload["applicant_id"] = user_id
        if name:
            payload["client_name"] = name
        store.update_payload(job_id, payload)
        _remember_search(
            store,
            job_id,
            source="user_message",
            applicant_ids=[user_id],
            name=name,
        )
        return None
    _drop_untrusted_binding(store, job)
    if not name:
        return None
    runner = searcher or _SEARCHER_OVERRIDE or default_searcher
    try:
        outcome = dict(runner(name) or {})
    except Exception:
        outcome = {"status": "error", "matches": []}
    status = str(outcome.get("status") or "error").casefold()
    matches = [row for row in (outcome.get("matches") or []) if isinstance(row, dict)]
    ids: list[str] = []
    for row in matches:
        applicant = str(row.get("applicant_id") or "").strip()
        if applicant and applicant not in ids:
            ids.append(applicant)
    titled = _titled_name(name)
    if status == "sign_in":
        from .user_reply import SIGN_IN_QUESTION

        _remember_search(
            store,
            job_id,
            source="sign_in",
            applicant_ids=[],
            user_line=SIGN_IN_QUESTION,
            name=name,
        )
        return SIGN_IN_QUESTION
    if status != "ok":
        _remember_search(
            store,
            job_id,
            source="error",
            applicant_ids=[],
            user_line=LOOKUP_MISS,
            name=name,
        )
        return LOOKUP_MISS
    if len(ids) > 1:
        line = f"I found more than one {titled}. Which one should I use?"
        _remember_search(
            store,
            job_id,
            source="several",
            applicant_ids=[],
            user_line=line,
            name=name,
            candidates=ids,
        )
        return line
    if len(ids) != 1:
        line = f"I couldn't find a client named {titled}."
        _remember_search(
            store,
            job_id,
            source="none",
            applicant_ids=[],
            user_line=line,
            name=name,
        )
        return line
    job = store.get_job(job_id)
    payload = dict(job.get("payload") or {})
    payload["applicant_id"] = ids[0]
    payload["client_name"] = name
    store.update_payload(job_id, payload)
    _remember_search(
        store,
        job_id,
        source="search",
        applicant_ids=ids,
        name=name,
    )
    return None


def pending_named_lookup_line(db_path: str, job_id: str | None) -> str:
    """The search already decided what to say. The model must not choose an id."""
    if not db_path or not job_id:
        return ""
    from .store import JobStore

    try:
        note = _search_note(JobStore(db_path), job_id)
    except Exception:
        return ""
    return str(note.get("user_line") or "").strip()


def refuse_named_lookup_navigation(store: Any, job: dict[str, Any] | None, code: str) -> str | None:
    """Refuse an account open that this job's search or the user did not name."""
    if not job_is_client_policy_lookup(job):
        return None
    from .chat_destination_binding import extract_ezlynx_urls

    trusted = set(trusted_applicant_ids(store, job))
    urls = extract_ezlynx_urls([{"code_preview": str(code or "")}])
    raw = str(code or "").strip()
    if raw.startswith("http"):
        urls = [raw, *urls]
    for url in urls:
        if _is_auth_url(url):
            continue
        from .ezlynx_write_scope import applicant_id_from_ezlynx_url

        applicant = str(applicant_id_from_ezlynx_url(url) or "").strip()
        path = urlparse(url).path.casefold()
        if "search" in path and applicant not in trusted:
            return (
                "PLAYWRIGHT_BLOCKED: do not guess an EZLynx search URL. "
                "Type the name into input#applicantSearch on the open page."
            )
        if applicant and applicant not in trusted:
            return (
                "PLAYWRIGHT_BLOCKED: applicant "
                f"{applicant} did not come from this job's search or the user's message"
            )
        if not applicant and "ezlynx.com" in url.casefold() and "/web/account/" not in path:
            if "search" in path or "search" in url.casefold():
                return (
                    "PLAYWRIGHT_BLOCKED: do not guess an EZLynx search URL. "
                    "Type the name into input#applicantSearch on the open page."
                )
    return None


def named_lookup_read_state(store: Any, job: dict[str, Any] | None) -> str:
    """'read' when this job loaded the bound applicant's account or policy page."""
    if not job or not job_is_client_policy_lookup(job):
        return "none"
    from .chat_destination_binding import extract_ezlynx_urls
    from .ezlynx_write_scope import applicant_id_from_ezlynx_url

    trusted = set(trusted_applicant_ids(store, job))
    saw_sign_in = False
    try:
        rows = store.list_playwright_exec(str(job.get("id") or ""))
    except Exception:
        rows = []
    for row in rows:
        status = str((row or {}).get("status") or "").casefold()
        for url in extract_ezlynx_urls([row]):
            if _is_auth_url(url):
                saw_sign_in = True
                continue
            if status != "ok":
                continue
            applicant = str(applicant_id_from_ezlynx_url(url) or "").strip()
            if applicant and applicant in trusted:
                return "read"
    if saw_sign_in:
        return "sign_in"
    return "none"
