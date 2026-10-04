"""Open an EZLynx account by id. Never search or guess URLs when the id is known.

Production job de9c530a (2026-08-27) already named account 220250093 and the
``/web/account/<id>/policies`` URL. The worker treated "open applicant" as a
search-box problem, then enumerated Summary/Details/Index URL variants for
36+ minutes with only ``gateway_progress`` heartbeats.

This helper is zip PYTHONPATH (``robie_job_engine/``). ``chat_guard`` injects
its contract into every Chat job. Skills / SOUL under ``.hermes`` are a
second copy and are not updated by a zip flip.

Never invents LOB steps. Never binds. Never authorizes COMPLETE.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse


PLAYWRIGHT_BLOCKED = "PLAYWRIGHT_BLOCKED"
HITL_OPERATOR = "Carlo"
EZLYNX_HOST = "https://app.ezlynx.com"
DEFAULT_ACCOUNT_PAGE = "policies"
CANONICAL_ACCOUNT_PREFIX = "/web/account/"

# Real account surfaces. Guessing Summary/Details/Index is the de9c530a loop.
ALLOWED_ACCOUNT_PAGES = frozenset(
    {
        "policies",
        "overview",
        "documents",
        "notes",
        "applications",
        "submissions-version-2",
        "quotes",
    }
)
GUESS_PAGES = frozenset({"summary", "details", "index", "home", "view", "applicant"})
SEARCH_MARKERS = (
    "search box",
    "searchbox",
    "applicant search",
    "search for applicant",
    "search applicants",
    "get_by_placeholder",
    "placeholder=\"search",
    "placeholder='search",
    "role=\"search",
    "locator('search",
    "locator(\"search",
)
WEB_ACCOUNT_RE = re.compile(
    r"/web/account/(\d{6,})(?:/([A-Za-z0-9._-]+))?",
    re.IGNORECASE,
)
EDIT_ACCOUNT_RE = re.compile(
    r"/applicantportal/policy/actions/edit/(\d{6,})/",
    re.IGNORECASE,
)
FORMENTRY_RE = re.compile(r"/applicantportal/formentry/(\d{6,})", re.IGNORECASE)
LABELED_ACCOUNT_RE = re.compile(
    r"\b(?:ezlynx\s+)?(?:account|applicant)(?:\s+id)?\s*[#:]?\s*(\d{6,})\b",
    re.IGNORECASE,
)
PAREN_ACCOUNT_RE = re.compile(
    r"\b(?:account|applicant|ezlynx)\b[^.\n]{0,40}\((\d{6,})\)",
    re.IGNORECASE,
)

STUCK_NO_ID = (
    "ROBIE was stuck and made no verified destination progress. "
    "The applicant search locator failed and no EZLynx account id was known. "
    f"HITL {HITL_OPERATOR}. Do not enumerate Summary/Details/Index URLs."
)
GUESS_LOOP = (
    f"{PLAYWRIGHT_BLOCKED}: EZLynx URL-guess loop refused; "
    "do not enumerate Summary/Details/Index variants; "
    f"HITL {HITL_OPERATOR}"
)


@dataclass(frozen=True)
class NavDecision:
    """One open-account choice. Tests and the Job Engine read this."""

    action: str
    url: str | None
    account_id: str | None
    allow_search: bool
    allow_url_guesses: bool
    reason: str
    hitl_operator: str | None = None
    max_direct_attempts: int = 1
    attempted_urls: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "url": self.url,
            "account_id": self.account_id,
            "allow_search": self.allow_search,
            "allow_url_guesses": self.allow_url_guesses,
            "reason": self.reason,
            "hitl_operator": self.hitl_operator,
            "max_direct_attempts": self.max_direct_attempts,
            "attempted_urls": list(self.attempted_urls),
        }


def direct_account_url(account_id: str, page: str = DEFAULT_ACCOUNT_PAGE) -> str:
    """Return the only first-open URL Robie may use for a known account."""
    ident = str(account_id or "").strip()
    if not ident.isdigit():
        raise ValueError("EZLynx account id must be numeric")
    suffix = str(page or DEFAULT_ACCOUNT_PAGE).strip("/").casefold()
    if not suffix or suffix in GUESS_PAGES:
        suffix = DEFAULT_ACCOUNT_PAGE
    return f"{EZLYNX_HOST}{CANONICAL_ACCOUNT_PREFIX}{ident}/{suffix}"


def _payload_blob(payload: dict[str, Any] | None) -> str:
    payload = dict(payload or {})
    parts = [
        str(payload.get("text") or ""),
        str(payload.get("account_id") or ""),
        str(payload.get("applicant_id") or ""),
        str(payload.get("account") or ""),
    ]
    return "\n".join(part for part in parts if part)


def extract_known_account_id(
    task_text: str,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Return the EZLynx account/applicant id already in the job or prompt.

    Unlabeled 8-digit policy numbers are ignored. A ``/web/account/<id>/``
    URL, a labeled "account 220250093", or payload ``account_id`` counts.
    """
    payload = dict(payload or {})
    for key in ("account_id", "applicant_id"):
        raw = str(payload.get(key) or "").strip()
        if raw.isdigit() and len(raw) >= 6:
            return raw
    blob = "\n".join(
        part for part in (str(task_text or ""), _payload_blob(payload)) if part
    )
    if not blob.strip():
        return None
    named = WEB_ACCOUNT_RE.search(blob)
    if named:
        return named.group(1)
    labeled = LABELED_ACCOUNT_RE.search(blob) or PAREN_ACCOUNT_RE.search(blob)
    if labeled:
        return labeled.group(1)
    edit = EDIT_ACCOUNT_RE.search(blob)
    if edit:
        return edit.group(1)
    form = FORMENTRY_RE.search(blob)
    if form:
        return form.group(1)
    return None


def named_direct_account_url(
    task_text: str,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """If the task already names a real ``/web/account/<id>/…`` URL, keep it."""
    blob = "\n".join(
        part for part in (str(task_text or ""), _payload_blob(payload)) if part
    )
    match = WEB_ACCOUNT_RE.search(blob)
    if not match:
        return None
    account_id, page = match.group(1), match.group(2) or DEFAULT_ACCOUNT_PAGE
    if str(page).casefold() in GUESS_PAGES:
        page = DEFAULT_ACCOUNT_PAGE
    if str(page).casefold() not in ALLOWED_ACCOUNT_PAGES:
        page = DEFAULT_ACCOUNT_PAGE
    return direct_account_url(account_id, page)


def is_search_attempt(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(marker in lowered for marker in SEARCH_MARKERS)


def _path_segments(url: str) -> list[str]:
    path = urlparse(str(url or "").strip()).path.casefold().strip("/")
    return [part for part in path.split("/") if part]


def is_direct_account_url(url: str, account_id: str | None = None) -> bool:
    match = WEB_ACCOUNT_RE.search(str(url or ""))
    if not match:
        return False
    if account_id and match.group(1) != str(account_id):
        return False
    page = (match.group(2) or DEFAULT_ACCOUNT_PAGE).casefold()
    return page not in GUESS_PAGES


def is_applicant_search_url(url: str) -> bool:
    """Name-search routes. These are not Summary/Details/Index guesses.

    A john smith lookup opens ``/web/applicant/search`` or
    ``/applicantportal/Search/Index``. Those pages are the search. Refusing
    them as unknown-account guesses parked the lookup.
    """
    segments = _path_segments(url)
    if not segments:
        return False
    if "search" not in segments:
        return False
    if "web" in segments and "applicant" in segments:
        return True
    return "applicantportal" in segments


def is_account_url_guess(url: str) -> bool:
    """True for Summary/Details/Index (and sibling) applicant URL guesses."""
    raw = str(url or "").strip()
    if not raw:
        return False
    if is_applicant_search_url(raw):
        return False
    segments = _path_segments(raw)
    if not segments:
        return False
    if any(part in GUESS_PAGES for part in segments):
        if "web" in segments and "account" in segments:
            return True
        if "applicant" in segments or "applicantportal" in segments:
            return True
        if any(part.isdigit() and len(part) >= 6 for part in segments):
            return True
    return False


def first_navigation(
    task_text: str,
    payload: dict[str, Any] | None = None,
) -> NavDecision:
    """First open must be the direct account URL when an id is already known."""
    account_id = extract_known_account_id(task_text, payload)
    if not account_id:
        return NavDecision(
            action="SEARCH_ALLOWED",
            url=None,
            account_id=None,
            allow_search=True,
            allow_url_guesses=False,
            reason="no EZLynx account id in the job or prompt",
        )
    url = named_direct_account_url(task_text, payload) or direct_account_url(
        account_id
    )
    return NavDecision(
        action="DIRECT",
        url=url,
        account_id=account_id,
        allow_search=False,
        allow_url_guesses=False,
        reason=(
            "account id is already known; first navigation is the direct "
            f"/web/account/{account_id}/ URL, not search"
        ),
    )


def after_search_locator_failure(
    task_text: str,
    *,
    attempted_urls: Iterable[str] = (),
    payload: dict[str, Any] | None = None,
) -> NavDecision:
    """Search locator failed: one direct-URL try if id known, else HITL."""
    prior = tuple(str(item) for item in attempted_urls if str(item).strip())
    account_id = extract_known_account_id(task_text, payload)
    if not account_id:
        return NavDecision(
            action="HITL",
            url=None,
            account_id=None,
            allow_search=False,
            allow_url_guesses=False,
            reason=STUCK_NO_ID,
            hitl_operator=HITL_OPERATOR,
            attempted_urls=prior,
        )
    url = named_direct_account_url(task_text, payload) or direct_account_url(
        account_id
    )
    already_direct = any(is_direct_account_url(item, account_id) for item in prior)
    already_guessed = any(is_account_url_guess(item) for item in prior)
    if already_direct or already_guessed:
        return NavDecision(
            action="STOP",
            url=None,
            account_id=account_id,
            allow_search=False,
            allow_url_guesses=False,
            reason=(
                f"{PLAYWRIGHT_BLOCKED}: already used the one direct-URL "
                f"fallback for account {account_id}; stop; HITL {HITL_OPERATOR}; "
                "do not enumerate more applicant URLs"
            ),
            hitl_operator=HITL_OPERATOR,
            attempted_urls=prior,
            max_direct_attempts=1,
        )
    return NavDecision(
        action="DIRECT_FALLBACK",
        url=url,
        account_id=account_id,
        allow_search=False,
        allow_url_guesses=False,
        reason=(
            "search locator failed and account id is known; try the direct "
            f"/web/account/{account_id}/ URL once, then stop"
        ),
        max_direct_attempts=1,
        attempted_urls=prior,
    )


def plan_open_applicant(
    task_text: str,
    *,
    search_locator_failed: bool = False,
    attempted_urls: Iterable[str] = (),
    payload: dict[str, Any] | None = None,
    playwright_code: str = "",
) -> NavDecision:
    """Single entry used by tests and the execution contract."""
    prior = tuple(str(item) for item in attempted_urls if str(item).strip())
    if search_locator_failed:
        return after_search_locator_failure(
            task_text, attempted_urls=prior, payload=payload
        )
    decision = first_navigation(task_text, payload)
    if (
        decision.action == "DIRECT"
        and playwright_code
        and is_search_attempt(playwright_code)
        and not any(is_direct_account_url(item, decision.account_id) for item in prior)
    ):
        return NavDecision(
            action="DIRECT",
            url=decision.url,
            account_id=decision.account_id,
            allow_search=False,
            allow_url_guesses=False,
            reason=(
                "account id is already known; refuse search-box locators; "
                f"first navigation is {decision.url}"
            ),
        )
    return decision


def refuse_guessed_account_url(
    url: str,
    *,
    attempted_urls: Iterable[str] = (),
    known_account_id: str | None = None,
) -> None:
    """Raise if this goto is a Summary/Details/Index guess or a guess loop."""
    raw = str(url or "").strip()
    prior = [str(item) for item in attempted_urls if str(item).strip()]
    account_id = known_account_id or extract_known_account_id(raw)
    if is_account_url_guess(raw):
        if account_id:
            raise RuntimeError(
                f"{PLAYWRIGHT_BLOCKED}: EZLynx account {account_id} is already "
                f"known; navigate to {direct_account_url(account_id)} once. "
                "Do not enumerate Summary/Details/Index URLs. "
                f"HITL {HITL_OPERATOR}"
            )
        raise RuntimeError(
            f"{PLAYWRIGHT_BLOCKED}: applicant URL guess refused and no "
            f"account id is known; stop; HITL {HITL_OPERATOR}"
        )
    if any(is_account_url_guess(item) for item in prior) and not is_direct_account_url(
        raw, account_id
    ):
        raise RuntimeError(GUESS_LOOP)


def account_nav_contract_lines(
    task_text: str,
    payload: dict[str, Any] | None = None,
) -> list[str]:
    """Hard rules the Chat worker actually sees (zip-path execution contract)."""
    decision = first_navigation(task_text, payload)
    lines = [
        "If this Job already names an EZLynx account or applicant id, the first navigation MUST be that account's /web/account/<id>/… URL (default https://app.ezlynx.com/web/account/<id>/policies). Do not open or guess a search-box locator. Do not enumerate Summary, Details, or Index URL variants.",
        "If a search locator fails and an account id is already known, try the direct /web/account/<id>/ URL once, then stop (HITL Carlo or stuck / no verified progress). If no account id is known, stop immediately: HITL Carlo or say stuck / no verified progress. Never spend the Job URL-guessing.",
    ]
    if decision.account_id and decision.url:
        lines.append(
            f"Known EZLynx account id: {decision.account_id}. "
            f"First navigation: {decision.url}. Do not search."
        )
    return lines


def _refuse_untrusted_named_lookup(url: object) -> str | None:
    """A named-client job may open only this search or the user's own id."""
    from .live_turn_guard import acting_db_path, acting_job_id, refuse_untrusted_applicant

    job_id = acting_job_id()
    db_path = acting_db_path()
    if not job_id or not db_path:
        return None
    try:
        from .client_name_lookup import refuse_named_lookup_navigation
        from .ezlynx_write_scope import applicant_id_from_ezlynx_url
        from .store import JobStore

        store = JobStore(db_path)
        job = store.get_job(job_id)
    except Exception:
        return None
    named = refuse_named_lookup_navigation(store, job, str(url or ""))
    if named:
        return named
    applicant = str(applicant_id_from_ezlynx_url(str(url or "")) or "").strip()
    return refuse_untrusted_applicant(store, job, applicant)


def install_account_nav_guard(scope: dict[str, Any]) -> dict[str, Any]:
    """Wrap Page.goto so a URL-guess loop cannot run for minutes."""
    page_cls = scope.get("Page")
    if page_cls is None:
        return {}
    original = getattr(page_cls, "goto", None)
    if not callable(original):
        return {}
    known = scope.get("_robie_known_account_id")
    if not known:
        known = extract_known_account_id(str(scope.get("_robie_task_text") or ""))

    def wrapped(self, url, *args, **kwargs):
        attempted = scope.setdefault("_robie_account_nav_attempted", [])
        refused = _refuse_untrusted_named_lookup(url)
        if refused:
            raise RuntimeError(refused)
        refuse_guessed_account_url(
            url,
            attempted_urls=attempted,
            known_account_id=known or scope.get("_robie_known_account_id"),
        )
        from .ezlynx_discussions import (
            arm_discussion_api_call,
            note_discussion_api_success,
            record_discussion_api_miss,
            response_status,
        )

        # A DiscussionApi 404/405 is recorded. The next guessed path raises
        # here, before the browser asks for it.
        arm_discussion_api_call(str(url or ""))
        attempted.append(str(url or ""))
        result = original(self, url, *args, **kwargs)
        status = response_status(result)
        if status in {404, 405}:
            record_discussion_api_miss(str(url or ""), status)
        elif status is not None and 200 <= status < 400:
            note_discussion_api_success(str(url or ""))
        return result

    wrapped.__name__ = getattr(original, "__name__", "goto")
    wrapped.__qualname__ = getattr(original, "__qualname__", "goto")
    setattr(page_cls, "goto", wrapped)
    scope["_robie_account_nav_guard"] = True
    return {"Page.goto": True}
