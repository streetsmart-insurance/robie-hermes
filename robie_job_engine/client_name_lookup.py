"""Read-only client lookup by name.

Typing into EZLynx global search is not a write. One matching client is
bound onto the job. More than one match asks a plain question. The write
allowlist is unchanged for every other control.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger("robie.health")

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
QUICK_SEARCH_SELECTOR = "#quickSearchInput"
APPLICANT_SEARCH_SELECTOR = "input#applicantSearch"
# Dashboard first. The older applicant box is the fallback. A header
# input whose placeholder is Search is the last resort.
SEARCH_BOX_SELECTORS = (
    QUICK_SEARCH_SELECTOR,
    APPLICANT_SEARCH_SELECTOR,
    "header input[placeholder='Search' i]",
    "header input[placeholder='Search']",
    "[role='banner'] input[placeholder='Search' i]",
    "[role='banner'] input[placeholder='Search']",
)
_RESULT_SELECTORS = (
    "[role='listbox'] a[href]",
    "[role='option'] a[href]",
    "a[href*='/web/account/']",
    "a[href*='/applicantportal/']",
)
_EMPTY_RESULTS = ("no result", "no applicant", "0 result", "not found", "no match")
SEARCH_RESULT_TIMEOUT_SECONDS = 15
_FORM_PATHS = ("/policy/actions/edit/", "formentry", "/applicantportal/policy")
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
    if "applicantsearch" in compact or "quicksearchinput" in compact:
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


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def _display_name(name: str) -> str:
    who = " ".join(str(name or "").split())
    return who.title() if who else ""


def _name_tokens(name: str) -> list[str]:
    """Letters only, so punctuation and a middle initial do not change the name."""
    folded = str(name or "").casefold().replace("-", " ")
    folded = re.sub(r"[^a-z0-9\s]", "", folded)
    tokens = [token for token in folded.split() if token and token not in _NAME_SUFFIXES]
    if len(tokens) >= 3:
        middle = [token for token in tokens[1:-1] if len(token) != 1]
        tokens = [tokens[0], *middle, tokens[-1]]
    return tokens


def account_name_matches(searched: str, account_name: str) -> bool:
    """True when this account's own name is the person who was searched."""
    left = _name_tokens(searched)
    right = _name_tokens(account_name)
    if not left or not right:
        return False
    return left == right or sorted(left) == sorted(right)


def _split_name_matches(
    searched: str, matches: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Account-name hits first. The rest are linked contacts on other accounts."""
    named: list[dict[str, Any]] = []
    linked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in matches:
        if not isinstance(row, dict):
            continue
        applicant = str(row.get("applicant_id") or "").strip()
        if not applicant or applicant in seen:
            continue
        seen.add(applicant)
        if account_name_matches(searched, str(row.get("name") or "")):
            named.append(row)
        else:
            linked.append(row)
    return named, linked


def which_client_question(name: str, matches: list[dict[str, Any]]) -> str:
    """A numbered list of accounts whose own name is the person searched."""
    titled = _titled_name(name)
    lines = [f"I found more than one {titled}."]
    for index, row in enumerate(matches[:5], start=1):
        lines.append(f"{index}. {_match_phrase(row)}")
    lines.append("Which one should I use?")
    return "\n".join(lines)


def linked_accounts_question(name: str, matches: list[dict[str, Any]]) -> str:
    """One line. These accounts are not named the person who was searched."""
    titled = _titled_name(name)
    shown = list(matches[:5])
    count = len(shown)
    noun = "account" if count == 1 else "accounts"
    bits: list[str] = []
    for index, row in enumerate(shown, start=1):
        who = _display_name(str(row.get("name") or ""))
        applicant = str(row.get("applicant_id") or "").strip()
        if who:
            bits.append(f"{index}. {who} (account {applicant})")
        elif applicant:
            bits.append(f"{index}. account {applicant}")
    listed = " ".join(bits)
    return (
        f"I didn't find an account named {titled}, but the name shows up on "
        f"{count} other {noun}: {listed} Which one, or none?"
    )


def _match_phrase(row: dict[str, Any]) -> str:
    """Name, place, and account id. No field names and no internal codes."""
    who = _display_name(str(row.get("name") or ""))
    address = " ".join(str(row.get("address") or "").split())
    city = " ".join(str(row.get("city") or "").split())
    place = address
    if city and city.casefold() not in place.casefold():
        place = ", ".join(part for part in (place, city) if part)
    if not place:
        place = " ".join(str(row.get("detail") or "").split())
    role = " ".join(str(row.get("role") or "").split())
    applicant = str(row.get("applicant_id") or "").strip()
    bits: list[str] = []
    if who:
        bits.append(who)
    joined = " ".join(bits).casefold()
    if place and place.casefold() not in joined:
        bits.append(place)
        joined = " ".join(bits).casefold()
    if role and role.casefold() not in joined:
        bits.append(role)
    if applicant:
        bits.append(f"account {applicant}")
    return ", ".join(bits) if bits else f"account {applicant}"


def bind_named_client(
    store: Any,
    job_id: str,
    name: str,
    matches: list[dict[str, Any]],
) -> str:
    """Bind one client and answer. Several account names ask which one."""
    who = " ".join(str(name or "").split())
    titled = who.title() if who else "That client"
    named, linked = _split_name_matches(who, matches)
    if len(named) > 1:
        return which_client_question(name, named)
    if len(named) == 1:
        matches = named
    elif linked:
        return linked_accounts_question(name, linked)
    else:
        return f"I couldn't find a client named {titled}."
    ids: list[str] = []
    for row in matches:
        applicant = str((row or {}).get("applicant_id") or "").strip()
        if applicant and applicant not in ids:
            ids.append(applicant)
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


_PROBE_OVERRIDE: Callable[[], str] | None = None
_LAST_PROBE_URL = ""
AUTHENTICATED_PROBE_URL = "https://app.ezlynx.com/web/"
_SIGN_OUT_CLAIM = re.compile(
    r"signed\s+out|sign[\s-]?out|sign[\s-]?in|log[\s-]?in|/auth/account/login",
    re.IGNORECASE,
)


def set_session_probe(probe: Callable[[], str] | None) -> None:
    """Tests pin the fresh-page probe. None uses the last live probe."""
    global _PROBE_OVERRIDE
    _PROBE_OVERRIDE = probe


def note_session_probe(url: str) -> None:
    global _LAST_PROBE_URL
    _LAST_PROBE_URL = str(url or "").strip()


def session_probe_url() -> str:
    if _PROBE_OVERRIDE is not None:
        try:
            return str(_PROBE_OVERRIDE() or "").strip()
        except Exception:
            return ""
    return _LAST_PROBE_URL


def text_claims_sign_out(text: str) -> bool:
    return _SIGN_OUT_CLAIM.search(str(text or "")) is not None


def sign_out_posture(text: str) -> str:
    """How to treat a sign-out claim after a fresh-page probe.

    ``ignore`` — no probe yet; the existing sign-in path decides.
    ``suppress`` — the fresh page is still authenticated. Do not ask
    the user to sign in. The lookup continues.
    ``post`` — the fresh probe also landed on login. One plain line.
    """
    if not text_claims_sign_out(text):
        return "ignore"
    probe = session_probe_url()
    if not probe:
        return "ignore"
    if _is_auth_url(probe):
        return "post"
    return "suppress"


def _body_text(page: Any) -> str:
    locator = getattr(page, "locator", None)
    if not callable(locator):
        return str(getattr(page, "body_text", "") or "")
    try:
        body = locator("body")
    except Exception:
        return ""
    inner = getattr(body, "inner_text", None)
    if not callable(inner):
        return ""
    try:
        return str(inner() or "")
    except TypeError:
        try:
            return str(inner(timeout=1000) or "")
        except Exception:
            return ""
    except Exception:
        return ""


def _page_html(page: Any) -> str:
    content = getattr(page, "content", None)
    if callable(content):
        try:
            return str(content() or "")
        except Exception:
            return ""
    return str(getattr(page, "html", "") or "")


def angular_shell_is_blank(page: Any) -> bool:
    """True for an empty ``<app-root></app-root>`` with no body text."""
    if _body_text(page).strip():
        return False
    folded = _page_html(page).casefold()
    return "<app-root" in folded and "</app-root>" in folded


def wait_for_angular_content(page: Any, *, seconds: float = 8.0) -> bool:
    """Wait until the Angular shell has real text. A fake page is one shot."""
    waiter = getattr(page, "wait_for_function", None)
    if callable(waiter):
        try:
            waiter(
                """() => {
                  const text = (document.body && document.body.innerText || '').trim();
                  return text.length > 0;
                }""",
                timeout=max(1, int(seconds * 1000)),
            )
        except Exception:
            pass
    return bool(_body_text(page).strip()) and not angular_shell_is_blank(page)


def close_extra_ezlynx_pages(context: Any, keep: Any) -> None:
    """Leave one EZLynx tab. Other EZLynx pages opened for the probe are closed."""
    pages = list(getattr(context, "pages", []) or [])
    for item in pages:
        if item is keep:
            continue
        url = _page_url(item).casefold()
        if url and "ezlynx.com" not in url:
            continue
        closer = getattr(item, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                continue


def account_activity_url(applicant_id: str) -> str:
    ident = str(applicant_id or "").strip()
    return f"https://app.ezlynx.com/web/account/{ident}/activity"


def open_bound_account_in_fresh_page(context: Any, applicant_id: str) -> dict[str, Any]:
    """Open the bound account on a new tab and wait for Angular content.

    A blank ``<app-root>`` is not a sign-out. A root redirect to login is
    not a sign-out either, unless a fresh page of a known authenticated
    route also lands on login. Extra pages are closed.
    """
    if context is None or not str(applicant_id or "").strip():
        return {"opened": False, "signed_out": False, "probe_url": ""}
    url = account_activity_url(applicant_id)
    fresh = context.new_page()
    goto = getattr(fresh, "goto", None)
    if callable(goto):
        goto(url)
    if not wait_for_angular_content(fresh):
        another = context.new_page()
        again = getattr(another, "goto", None)
        if callable(again):
            again(url)
        wait_for_angular_content(another)
        closer = getattr(fresh, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        fresh = another
    probe = context.new_page()
    probe_goto = getattr(probe, "goto", None)
    if callable(probe_goto):
        probe_goto(AUTHENTICATED_PROBE_URL)
    probe_url = _page_url(probe)
    note_session_probe(probe_url)
    signed_out = _is_auth_url(probe_url)
    probe_close = getattr(probe, "close", None)
    if callable(probe_close):
        try:
            probe_close()
        except Exception:
            pass
    close_extra_ezlynx_pages(context, fresh)
    return {
        "opened": True,
        "signed_out": signed_out,
        "probe_url": probe_url,
        "url": _page_url(fresh),
        "page": fresh,
    }


def assess_session_after_stuck_tab(
    page: Any,
    context: Any,
    *,
    account_url: str = "",
) -> dict[str, Any]:
    """A blank shell or a login landing is checked on a fresh page.

    The fresh page is a known authenticated route: the bound account when
    we have one, otherwise ``/web/``. Sign-out is reported only when that
    page is also the login page. The extra page is closed. One EZLynx tab
    stays.
    """
    if context is None:
        url = _page_url(page)
        return {
            "signed_out": _is_auth_url(url),
            "probe_url": url,
            "page": page,
        }
    target = str(account_url or "").strip() or AUTHENTICATED_PROBE_URL
    fresh = context.new_page()
    goto = getattr(fresh, "goto", None)
    if callable(goto):
        goto(target)
    wait_for_angular_content(fresh)
    probe_url = _page_url(fresh)
    note_session_probe(probe_url)
    signed_out = _is_auth_url(probe_url)
    if signed_out:
        closer = getattr(fresh, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        return {"signed_out": True, "probe_url": probe_url, "page": page}
    close_extra_ezlynx_pages(context, fresh)
    return {"signed_out": False, "probe_url": probe_url, "page": fresh}


def install_stuck_tab_recovery(scope: dict[str, Any]) -> None:
    """Wrap ``page.goto`` so a stuck account tab is replaced from the same context."""
    page = scope.get("page")
    context = scope.get("context")
    if page is None or context is None:
        return
    original = getattr(page, "goto", None)
    if not callable(original) or getattr(original, "_robie_stuck_tab", False):
        return

    def goto(url: str, *args: Any, **kwargs: Any) -> Any:
        response = original(url, *args, **kwargs)
        current = scope.get("page") or page
        blank = angular_shell_is_blank(current)
        login = _is_auth_url(_page_url(current))
        if not blank and not login:
            return response
        account = ""
        raw = str(url or "")
        match = re.search(r"/web/account/(\d{6,})", raw, re.IGNORECASE)
        if match:
            account = account_activity_url(match.group(1))
        result = assess_session_after_stuck_tab(
            current, context, account_url=account
        )
        replacement = result.get("page")
        if replacement is not None and not result.get("signed_out"):
            scope["page"] = replacement
        return response

    goto._robie_stuck_tab = True  # type: ignore[attr-defined]
    page.goto = goto


def _page_url(page: Any) -> str:
    value = getattr(page, "url", "")
    if callable(value):
        try:
            value = value()
        except Exception:
            return ""
    return str(value or "").strip()


def _is_dashboard_url(url: object) -> bool:
    parsed = urlparse(str(url or "").strip())
    if "ezlynx.com" not in (parsed.netloc or str(url or "")).casefold():
        return False
    path = (parsed.path or "").casefold().rstrip("/")
    return path in {"", "/web", "/web/home", "/home", "/dashboard", "/web/dashboard"}


def _is_form_url(url: object) -> bool:
    path = urlparse(str(url or "")).path.casefold()
    return any(marker in path for marker in _FORM_PATHS)


def _page_would_lose_unsaved_form(page: Any) -> bool:
    """A form tab with unsaved fields is not a place to type a search."""
    if not _is_form_url(_page_url(page)):
        return False
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return True
    try:
        dirty = evaluate(
            """() => {
              const skip = new Set(["quickSearchInput", "applicantSearch"]);
              const nodes = document.querySelectorAll("input, textarea, select");
              for (const node of nodes) {
                if (skip.has(node.id)) continue;
                if (node.type === "hidden" || node.type === "search") continue;
                const current = "value" in node ? String(node.value || "") : "";
                const initial = "defaultValue" in node ? String(node.defaultValue || "") : "";
                if (current !== initial) return true;
              }
              return false;
            }"""
        )
    except Exception:
        return True
    return bool(dirty)


def choose_client_search_page(pages: list[Any]) -> Any | None:
    """Use the dashboard. Do not type into a tab that would lose an unsaved form."""
    ezlynx = [page for page in pages if page and "ezlynx.com" in _page_url(page).casefold()]
    dashboards = [page for page in ezlynx if _is_dashboard_url(_page_url(page))]
    if dashboards:
        return dashboards[0]
    for page in ezlynx:
        if _page_would_lose_unsaved_form(page):
            continue
        return page
    return None


def _locator_count(node: Any) -> int:
    try:
        return int(node.count())
    except Exception:
        return 0


def _first_search_box(page: Any) -> Any | None:
    locator_fn = getattr(page, "locator", None)
    if not callable(locator_fn):
        return None
    for selector in SEARCH_BOX_SELECTORS:
        try:
            box = locator_fn(selector)
        except Exception:
            continue
        if _locator_count(box) >= 1 and callable(getattr(box, "fill", None)):
            return box
    return None


def _attr(node: Any, name: str) -> str:
    getter = getattr(node, "get_attribute", None)
    if not callable(getter):
        return ""
    try:
        return str(getter(name) or "")
    except Exception:
        return ""


def _dom_hint(page: Any) -> str:
    """Input ids and placeholders only. Never the rest of the page text."""
    locator_fn = getattr(page, "locator", None)
    if not callable(locator_fn):
        return "no locator"
    try:
        inputs = locator_fn("input")
        total = _locator_count(inputs)
    except Exception:
        return "inputs unreadable"
    bits: list[str] = []
    for index in range(min(total, 8)):
        node = inputs.nth(index) if hasattr(inputs, "nth") else inputs
        ident = _attr(node, "id") or "-"
        placeholder = _attr(node, "placeholder") or "-"
        bits.append(f"id={ident} placeholder={placeholder}")
    return f"inputs={total} " + "; ".join(bits)


def _log_unreadable(page: Any, name: str, why: str) -> None:
    logger.info(
        "named client search unreadable url=%s name=%s hint=%s why=%s",
        _page_url(page) or "none",
        " ".join(str(name or "").split()),
        _dom_hint(page),
        why,
    )


def _matches_from_links(links: Any) -> list[dict[str, str]]:
    from .ezlynx_write_scope import applicant_id_from_ezlynx_url

    total = _locator_count(links)
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for index in range(total):
        item = links.nth(index) if hasattr(links, "nth") else links
        href = _attr(item, "href")
        applicant = str(applicant_id_from_ezlynx_url(href) or "").strip()
        if not applicant or applicant in seen:
            continue
        seen.add(applicant)
        text = ""
        inner = getattr(item, "inner_text", None)
        if callable(inner):
            try:
                text = str(inner() or "")
            except Exception:
                text = ""
        found.append({"applicant_id": applicant, **_visible_match_fields(text)})
    return found


def _matches_from_hrefs(hrefs: list[str]) -> list[dict[str, str]]:
    from .ezlynx_write_scope import applicant_id_from_ezlynx_url

    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for href in hrefs:
        applicant = str(applicant_id_from_ezlynx_url(href) or "").strip()
        if not applicant or applicant in seen:
            continue
        seen.add(applicant)
        found.append(
            {
                "applicant_id": applicant,
                "name": "",
                "address": "",
                "role": "",
                "detail": "",
            }
        )
    return found


def _visible_match_fields(raw: str) -> dict[str, str]:
    """Split a result row into the name and whatever else the page showed."""
    lines = [
        " ".join(part.split())
        for part in str(raw or "").replace("\r", "\n").split("\n")
        if part.strip()
    ]
    name = lines[0] if lines else ""
    address = ""
    role = ""
    extras: list[str] = []
    for line in lines[1:]:
        words = line.split()
        has_digit = any(character.isdigit() for character in line)
        if not address and (has_digit or "," in line or len(words) >= 2):
            address = line
        elif not role and len(words) <= 3 and not has_digit:
            role = line
        else:
            extras.append(line)
    detail = ", ".join(part for part in [address, role, *extras] if part)
    return {"name": name, "address": address, "role": role, "detail": detail}


def _matches_from_evaluated(rows: list[Any]) -> list[dict[str, str]]:
    from .ezlynx_write_scope import applicant_id_from_ezlynx_url

    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            href, text = row, ""
        elif isinstance(row, dict):
            href = str(row.get("href") or "")
            text = str(row.get("text") or "")
        else:
            continue
        applicant = str(applicant_id_from_ezlynx_url(href) or "").strip()
        if not applicant or applicant in seen:
            continue
        seen.add(applicant)
        found.append({"applicant_id": applicant, **_visible_match_fields(text)})
    return found


def _parse_results(page: Any) -> list[dict[str, str]] | None:
    """Applicant ids from result links, or None when the DOM is not recognizable.

    Header and nav links are not results. An empty recognizable state is an
    empty list. Anything else is unreadable.
    """
    locator_fn = getattr(page, "locator", None)
    evaluate = getattr(page, "evaluate", None)
    found: list[dict[str, str]] = []
    if callable(evaluate):
        try:
            hrefs = evaluate(
                """() => {
                  const nodes = Array.from(document.querySelectorAll(
                    "[role='listbox'] a[href], [role='option'] a[href], a[href*='/web/account/'], a[href*='/applicantportal/']"
                  ));
                  return nodes
                    .filter(node => !node.closest("header, nav, [role='banner']"))
                    .map(node => ({
                      href: node.getAttribute("href") || "",
                      text: node.innerText || ""
                    }));
                }"""
            )
        except Exception:
            return None
        if isinstance(hrefs, list):
            found = _matches_from_evaluated(hrefs)
    elif callable(locator_fn):
        seen: set[str] = set()
        for selector in _RESULT_SELECTORS:
            try:
                links = locator_fn(selector)
            except Exception:
                continue
            for row in _matches_from_links(links):
                applicant = row["applicant_id"]
                if applicant in seen:
                    continue
                seen.add(applicant)
                found.append(row)
            if found:
                break
    else:
        return None
    if found:
        return found
    if not callable(locator_fn):
        return None
    try:
        body = locator_fn("body")
        inner = getattr(body, "inner_text", None)
        text = " ".join(str(inner() or "").split()).casefold() if callable(inner) else ""
    except Exception:
        return None
    if any(marker in text for marker in _EMPTY_RESULTS):
        return []
    return None


def _wait_for_search_results(
    page: Any, *, seconds: float = SEARCH_RESULT_TIMEOUT_SECONDS
) -> None:
    """Enter opens the legacy Search/Index page. Its links are not there yet.

    Wait for that navigation, then for ``/web/account/<digits>/`` links or let
    the later parse see an explicit no-results marker. One bounded timeout.
    """
    deadline = time.monotonic() + max(0.0, float(seconds))

    def remaining_ms() -> int:
        return max(1, int((deadline - time.monotonic()) * 1000))

    wait_url = getattr(page, "wait_for_url", None)
    if callable(wait_url):
        try:
            wait_url(
                lambda url: "search/index" in str(url or "").casefold()
                or bool(re.search(r"/web/account/\d+/", str(url or ""), re.I)),
                timeout=remaining_ms(),
            )
        except Exception:
            pass
    locator_fn = getattr(page, "locator", None)
    if not callable(locator_fn):
        return
    try:
        links = locator_fn("a[href*='/web/account/']")
    except Exception:
        return
    wait_links = getattr(links, "wait_for", None)
    if not callable(wait_links):
        return
    try:
        wait_links(state="attached", timeout=remaining_ms())
    except Exception:
        return


def read_applicant_search(page: Any, name: str) -> dict[str, Any]:
    """Type the name into the open page's search box. Do not guess a URL."""
    if _is_auth_url(_page_url(page)):
        return {"status": "sign_in", "matches": []}
    box = _first_search_box(page)
    if box is None:
        _log_unreadable(page, name, "search box not found")
        return {"status": "error", "matches": []}
    try:
        box.fill(name)
        press = getattr(box, "press", None)
        if callable(press):
            press("Enter")
    except Exception:
        _log_unreadable(page, name, "search box could not be typed")
        return {"status": "error", "matches": []}
    if _is_auth_url(_page_url(page)):
        return {"status": "sign_in", "matches": []}
    _wait_for_search_results(page)
    if _is_auth_url(_page_url(page)):
        return {"status": "sign_in", "matches": []}
    matches = _parse_results(page)
    if matches is None:
        _log_unreadable(page, name, "results not recognized")
        return {"status": "error", "matches": []}
    return {"status": "ok", "matches": matches}


def _connect_current_page() -> tuple[Any, Any] | None:
    """The dashboard, or another page that is not an unsaved form.

    This never navigates to a guessed EZLynx URL.
    """
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
    page = choose_client_search_page(pages)
    if page is None:
        try:
            playwright.stop()
        except Exception:
            pass
        return None
    return page, playwright


def _search_open_page(name: str) -> dict[str, Any]:
    opened = _connect_current_page()
    if opened is None:
        logger.info(
            "named client search unreadable url=none name=%s hint=no safe EZLynx page why=no page",
            " ".join(str(name or "").split()),
        )
        return {"status": "error", "matches": []}
    page, playwright = opened
    try:
        outcome = read_applicant_search(page, name)
        matches = [row for row in (outcome.get("matches") or []) if isinstance(row, dict)]
        if str(outcome.get("status") or "") == "ok" and len(matches) == 1:
            context = getattr(page, "context", None)
            applicant = str(matches[0].get("applicant_id") or "").strip()
            if context is not None and applicant:
                outcome["account_tab"] = open_bound_account_in_fresh_page(
                    context, applicant
                )
        return outcome
    except Exception:
        _log_unreadable(page, name, "search failed")
        return {"status": "error", "matches": []}
    finally:
        try:
            playwright.stop()
        except Exception:
            pass


def default_searcher(name: str) -> dict[str, Any]:
    """Sync Playwright cannot run on the gateway loop.

    The EZLynx re-read already leaves the loop for the same reason. When this
    function is called from the loop, the sync search runs on a worker thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _search_open_page(name)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_search_open_page, name).result()


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


def _stored_match(row: dict[str, Any]) -> dict[str, str]:
    return {
        "applicant_id": str(row.get("applicant_id") or "").strip(),
        "name": str(row.get("name") or "").strip(),
        "address": str(row.get("address") or "").strip(),
        "city": str(row.get("city") or "").strip(),
        "role": str(row.get("role") or "").strip(),
        "detail": str(row.get("detail") or "").strip(),
    }


def _remember_search(
    store: Any,
    job_id: str,
    *,
    source: str,
    applicant_ids: list[str],
    user_line: str = "",
    name: str = "",
    candidates: list[str] | None = None,
    matches: list[dict[str, Any]] | None = None,
    reasked: bool = False,
    list_kind: str = "",
) -> None:
    stored = [
        _stored_match(row)
        for row in (matches or [])
        if str((row or {}).get("applicant_id") or "").strip()
    ]
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
            "matches": stored[:5],
            "reasked": bool(reasked),
            "list_kind": list_kind,
        },
    )


def _running_under_test() -> bool:
    """The suite must not open a live EZLynx page to resolve a name."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    import sys

    command = " ".join(sys.argv).casefold()
    return "pytest" in command or "unittest" in command


def prepare_named_write_client(
    store: Any,
    job_id: str,
    *,
    searcher: Callable[[str], dict[str, Any]] | None = None,
) -> str | None:
    """Name-search before a write when the request names a person and no id.

    One match is bound onto the job. Several matches ask which account.
    A missing browser in tests does not ask the user for an applicant id.
    """
    from .live_turn_guard import person_named_in_job

    job = store.get_job(job_id)
    name = person_named_in_job(job) or ""
    if not name:
        return None
    note = _search_note(store, job_id)
    if list(note.get("applicant_ids") or []) and str(note.get("source") or "") in {
        "search",
        "user_message",
    }:
        return None
    if str(note.get("source") or "") in {"several", "linked"}:
        return _consume_client_choice(store, job_id)
    explicit = user_message_applicant(job)
    if explicit:
        payload = dict(job.get("payload") or {})
        payload["applicant_id"] = explicit
        payload["client_name"] = name
        store.update_payload(job_id, payload)
        _remember_search(
            store,
            job_id,
            source="user_message",
            applicant_ids=[explicit],
            name=name,
        )
        return None
    runner = searcher or _SEARCHER_OVERRIDE
    if runner is None and _running_under_test():
        return None
    if runner is None:
        runner = default_searcher
    try:
        outcome = dict(runner(name) or {})
    except Exception:
        outcome = {"status": "error", "matches": []}
    status = str(outcome.get("status") or "error").casefold()
    matches = [row for row in (outcome.get("matches") or []) if isinstance(row, dict)]
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
        return None
    named, linked = _split_name_matches(name, matches)
    if len(named) == 1:
        applicant = str(named[0].get("applicant_id") or "").strip()
        payload = dict(store.get_job(job_id).get("payload") or {})
        payload["applicant_id"] = applicant
        payload["client_name"] = name
        store.update_payload(job_id, payload)
        _remember_search(
            store,
            job_id,
            source="search",
            applicant_ids=[applicant],
            name=name,
            matches=[_stored_match(named[0])],
        )
        return None
    if len(named) > 1:
        shown = [_stored_match(row) for row in named[:5]]
        line = which_client_question(name, shown)
        _remember_search(
            store,
            job_id,
            source="several",
            applicant_ids=[],
            user_line=line,
            name=name,
            candidates=[str(row.get("applicant_id") or "") for row in shown],
            matches=shown,
            list_kind="named",
        )
        return line
    if linked:
        shown = [_stored_match(row) for row in linked[:5]]
        line = linked_accounts_question(name, shown)
        _remember_search(
            store,
            job_id,
            source="linked",
            applicant_ids=[],
            user_line=line,
            name=name,
            candidates=[str(row.get("applicant_id") or "") for row in shown],
            matches=shown,
            list_kind="linked",
        )
        return line
    return None


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
        if str(note.get("source") or "") in {"several", "linked"}:
            return _consume_client_choice(store, job_id)
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
    named, linked = _split_name_matches(name, matches)
    if len(named) == 1:
        applicant = str(named[0].get("applicant_id") or "").strip()
        job = store.get_job(job_id)
        payload = dict(job.get("payload") or {})
        payload["applicant_id"] = applicant
        payload["client_name"] = name
        store.update_payload(job_id, payload)
        _remember_search(
            store,
            job_id,
            source="search",
            applicant_ids=[applicant],
            name=name,
            matches=[_stored_match(named[0])],
        )
        return None
    if len(named) > 1:
        shown = [_stored_match(row) for row in named[:5]]
        line = which_client_question(name, shown)
        _remember_search(
            store,
            job_id,
            source="several",
            applicant_ids=[],
            user_line=line,
            name=name,
            candidates=[str(row.get("applicant_id") or "") for row in shown],
            matches=shown,
            list_kind="named",
        )
        return line
    if linked:
        shown = [_stored_match(row) for row in linked[:5]]
        line = linked_accounts_question(name, shown)
        _remember_search(
            store,
            job_id,
            source="linked",
            applicant_ids=[],
            user_line=line,
            name=name,
            candidates=[str(row.get("applicant_id") or "") for row in shown],
            matches=shown,
            list_kind="linked",
        )
        return line
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


_CHOICE_SKIP = {
    "the",
    "a",
    "an",
    "one",
    "account",
    "and",
    "of",
    "to",
    "for",
    "please",
    "use",
    "which",
    "i",
    "me",
    "that",
    "this",
    "client",
    "on",
    "in",
    "at",
    "my",
    "it",
    "is",
    "with",
    "from",
    "or",
    "stop",
    "cancel",
}
_ORDINAL_WORDS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
}
_ORDINAL_REPLY = re.compile(
    r"(?:"
    r"(?:(?:the|option|number|choice|no\.?)\s+)*(?:#\s*)?(\d+)(?:st|nd|rd|th)?(?:\s+one)?"
    r"|(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)(?:\s+one)?"
    r")"
    r"(?:\s+please)?",
    re.IGNORECASE,
)


def _clarification_reply(store: Any, job: dict[str, Any]) -> str:
    """The user's answer to the pending question, not the original ask."""
    job_id = str(job.get("id") or "")
    try:
        note = store.get_checkpoint(job_id, "clarification_reply") or {}
    except Exception:
        note = {}
    if isinstance(note, dict):
        text = str(note.get("text") or "").strip()
        if text:
            return text
    return str(dict(job.get("payload") or {}).get("clarification_reply") or "").strip()


def _reply_is_stop(text: str) -> bool:
    """/stop and /cancel are not a client pick. The adapter handles them."""
    from .chat_turn_control import is_stop_command

    return is_stop_command(text)


def _displayed_matches(note: dict[str, Any]) -> list[dict[str, str]]:
    rows = [
        _stored_match(row)
        for row in (note.get("matches") or [])
        if isinstance(row, dict) and str(row.get("applicant_id") or "").strip()
    ]
    if rows:
        return rows[:5]
    found: list[dict[str, str]] = []
    for item in note.get("candidates") or []:
        token = str(item or "").strip()
        if token:
            found.append(_stored_match({"applicant_id": token}))
    return found[:5]


def _ordinal_index(reply: str, count: int) -> int | None:
    folded = " ".join(str(reply or "").casefold().split()).strip(" .,!?:;")
    match = _ORDINAL_REPLY.fullmatch(folded)
    if not match or count < 1:
        return None
    if match.group(1):
        number = int(match.group(1))
    else:
        number = _ORDINAL_WORDS[match.group(2)]
    if 1 <= number <= count:
        return number - 1
    return None


def _field_tokens(row: dict[str, Any]) -> set[str]:
    blob = " ".join(
        str(row.get(key) or "")
        for key in ("name", "address", "city", "role", "detail")
    )
    return {
        token
        for token in re.findall(r"[a-z0-9']+", blob.casefold())
        if token not in _CHOICE_SKIP and len(token) >= 2
    }


def _distinguishing_index(reply: str, matches: list[dict[str, str]]) -> int | None:
    row_tokens = [_field_tokens(row) for row in matches]
    nonempty = [tokens for tokens in row_tokens if tokens]
    common: set[str] = set()
    if len(nonempty) > 1:
        common = set.intersection(*nonempty)
    reply_tokens = [
        token
        for token in re.findall(r"[a-z0-9']+", str(reply or "").casefold())
        if token not in _CHOICE_SKIP and token not in common and len(token) >= 2
    ]
    if not reply_tokens:
        return None
    hits: set[int] = set()
    for token in reply_tokens:
        owners = [index for index, tokens in enumerate(row_tokens) if token in tokens]
        if len(owners) == 1:
            hits.add(owners[0])
    if len(hits) == 1:
        return next(iter(hits))
    return None


def _select_match_index(reply: str, matches: list[dict[str, str]]) -> int | None:
    id_hits: list[int] = []
    for index, row in enumerate(matches):
        applicant = str(row.get("applicant_id") or "").strip()
        if applicant and re.search(rf"(?<!\d){re.escape(applicant)}(?!\d)", reply):
            id_hits.append(index)
    ordinal = _ordinal_index(reply, len(matches))
    chosen = set(id_hits)
    if ordinal is not None:
        chosen.add(ordinal)
    if len(chosen) == 1:
        return next(iter(chosen))
    if chosen:
        return None
    return _distinguishing_index(reply, matches)


def _bind_chosen_match(
    store: Any,
    job_id: str,
    job: dict[str, Any],
    *,
    name: str,
    chosen: dict[str, str],
) -> None:
    applicant = str(chosen.get("applicant_id") or "").strip()
    payload = dict(job.get("payload") or {})
    payload["applicant_id"] = applicant
    account_name = str(chosen.get("name") or "").strip()
    if account_name and not account_name_matches(name, account_name):
        payload["client_name"] = _display_name(account_name)
    elif name:
        payload["client_name"] = name
    elif account_name:
        payload["client_name"] = account_name
    store.update_payload(job_id, payload)
    _remember_search(
        store,
        job_id,
        source="search",
        applicant_ids=[applicant],
        user_line="",
        name=name,
        candidates=[applicant],
        matches=[chosen],
    )
    from .chat_turn_control import clear_agent_stop
    from .models import JobStatus

    clear_agent_stop(job_id)
    current = store.get_job(job_id)
    if JobStatus(current["status"]) == JobStatus.NEEDS_CLARIFICATION:
        store.resume(job_id)


_NONE_REPLIES = {
    "none",
    "neither",
    "no",
    "no thanks",
    "none of them",
    "none of these",
    "neither of them",
}


def _reply_is_none(text: str) -> bool:
    folded = " ".join(str(text or "").casefold().split()).strip(" .,!?")
    return folded in _NONE_REPLIES


def _open_choice_question(name: str, matches: list[dict[str, str]], kind: str) -> str:
    if kind == "linked":
        return linked_accounts_question(name, matches)
    return which_client_question(name, matches)


def _consume_client_choice(store: Any, job_id: str) -> str | None:
    """Bind one saved match from the reply, or ask once more.

    None means the model may run. A returned line is the question to send
    instead. Stop does not bind and does not ask again. "None" declines the
    other accounts and does not bind one of them.
    """
    note = _search_note(store, job_id)
    source = str(note.get("source") or "")
    if source not in {"several", "linked"}:
        return str(note.get("user_line") or "").strip() or None
    if list(note.get("applicant_ids") or []):
        return None
    job = store.get_job(job_id)
    reply = _clarification_reply(store, job)
    question = str(note.get("user_line") or "").strip()
    if not reply:
        return question or None
    if _reply_is_stop(reply):
        return None
    matches = _displayed_matches(note)
    name = str(note.get("name") or "")
    kind = str(note.get("list_kind") or "") or ("linked" if source == "linked" else "named")
    if kind == "linked" and _reply_is_none(reply):
        line = f"I couldn't find a client named {_titled_name(name)}."
        _remember_search(
            store,
            job_id,
            source="none",
            applicant_ids=[],
            user_line=line,
            name=name,
        )
        return line
    index = _select_match_index(reply, matches) if matches else None
    if index is not None:
        _bind_chosen_match(store, job_id, job, name=name, chosen=matches[index])
        return None
    if note.get("reasked"):
        return question or _open_choice_question(name, matches, kind)
    line = _open_choice_question(name, matches, kind)
    ids = [str(row.get("applicant_id") or "") for row in matches if row.get("applicant_id")]
    _remember_search(
        store,
        job_id,
        source=source,
        applicant_ids=[],
        user_line=line,
        name=name,
        candidates=list(note.get("candidates") or ids),
        matches=matches,
        reasked=True,
        list_kind=kind,
    )
    return line


def pending_named_lookup_line(db_path: str, job_id: str | None) -> str:
    """The line to send before the model, or empty when the model should run.

    A which-client question stays here until the reply picks one saved match.
    That pick clears the line so the job can answer. A reply that picks nobody
    asks once more. Stop is not a pick and is not another question.
    """
    if not db_path or not job_id:
        return ""
    from .store import JobStore

    try:
        store = JobStore(db_path)
        note = _search_note(store, job_id)
    except Exception:
        return ""
    if str(note.get("source") or "") in {"several", "linked"}:
        return str(_consume_client_choice(store, job_id) or "").strip()
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
        probe = session_probe_url()
        # A login URL on the stuck tab is not a sign-out when a fresh page
        # in the same context is still authenticated.
        if probe and not _is_auth_url(probe):
            return "probe_ok"
        return "sign_in"
    return "none"
