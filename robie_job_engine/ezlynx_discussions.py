"""EZLynx Discussion API v8 — applicant discussions and notes.

Fail-closed contract (standing agency rules):

- Look up an applicant's discussions, pick the single right existing one, and
  append the note to it.
- Discussion CREATION is allowed ONLY through
  :func:`create_discussion_with_note`, and only under Carlo's 2026-09-28
  standing authorization for the certificate sweep: when a certificate
  request matches an applicant but no existing discussion safely fits, the
  sweep auto-creates a NAMED discussion (never "Untitled") with the filing
  note as its first note. Ad-hoc creation anywhere else is still forbidden.
- Every creation is fail-closed: the write-scope allowlist and the
  no-phone-number note guard run before the POST, and a fresh GET read-back
  must prove the returned discussion exists, carries the requested title,
  and has noteCount >= 1. Any missing proof raises; the caller holds the
  request UNVERIFIED.
- The delete-discussion endpoints EZLynx documents are NOT implemented and
  are never called.
- Writes are allowlist-gated through
  :func:`robie_job_engine.ezlynx_write_scope.require_allowed_ezlynx_write_applicant`.
  A non-allowlisted applicant raises before any HTTP request is made.
- Note bodies must not contain phone numbers: the agency's call automation
  watches discussion text and auto-dials numbers it finds. A note carrying a
  phone number is refused outright.

Endpoints implemented (per the EZLynx Discussion API documentation):

- ``POST {identity}/connect/token`` with ``grant_type=vendor_data_access``
- ``GET {base}/v8/discussions/ids-by-applicant?applicantId={id}``
- ``GET {base}/v8/discussions/by-applicant?applicantId={id}``
- ``GET {base}/v8/discussions/{discussionId}``
- ``POST {base}/v8/discussions/{discussionId}/notes``
- ``POST {base}/v8/discussions/with-note`` (named-discussion creation)

``urlopen`` is injectable for tests; production uses urllib directly.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib import error, parse, request
from urllib.parse import urlparse

from .ezlynx_write_scope import require_allowed_ezlynx_write_applicant

GRANT_TYPE = "vendor_data_access"
TOKEN_EXPIRY_SKEW_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 30

# UAT base from the EZLynx documentation email. The Production hostname in
# that same email ("app.tezlynx.com") looks like a typo and is UNVERIFIED, so
# Production intentionally has no default: pass the literal base explicitly.
UAT_DISCUSSION_BASE_URL = "https://app.uatezlynx.com/DiscussionApi/"

_PHONE_LIKE = re.compile(
    r"(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}"
)


class DiscussionApiError(RuntimeError):
    """Transport, authentication, or API failure. Messages never carry secrets."""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        super().__init__(message)


class DiscussionSelectionError(RuntimeError):
    """No single existing discussion could be chosen. Nothing was written."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


NO_DISCUSSIONS = "NO_DISCUSSIONS"
AMBIGUOUS_DISCUSSIONS = "AMBIGUOUS_DISCUSSIONS"
UNTITLED_FORBIDDEN = "UNTITLED_FORBIDDEN"


@dataclass(frozen=True)
class DiscussionApiConfig:
    """Connection details for the Discussion API.

    ``discussion_base_url`` is the DiscussionApi root, e.g.
    ``https://app.uatezlynx.com/DiscussionApi/``. ``token_endpoint`` is the
    identity ``connect/token`` URL. Credentials are supplied by the caller
    (Secret Manager at runtime); they are never hardcoded here.
    """

    discussion_base_url: str
    token_endpoint: str
    client_id: str
    client_secret: str
    username: str
    integration_group_id: str
    scope: str = "DiscussionApi openid"
    # Live SSRobie password. Empty for the UAT vendor grant. Never logged.
    password: str = field(default="", repr=False)


def _default_urlopen(url: str, *, data: bytes | None, headers: dict[str, str], timeout: int):
    req = request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    return request.urlopen(req, timeout=timeout)


# A browser User-Agent. Cloudflare 1010 rejects the default Python urllib
# signature. Cookie values are never logged.
_CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_BROWSER_CACHE: dict[str, Any] = {}
_BROWSER_CACHE_SECONDS = 30.0


def _is_token_endpoint(url: str) -> bool:
    path = urlparse(str(url or "")).path.casefold()
    return path.endswith("/connect/token") or path.rstrip("/").endswith("/token")


def _cdp_port_open(timeout: float = 0.2) -> bool:
    """True when the persistent Chrome debug port accepts a connection."""
    from .ezlynx_portal_session import cdp_url

    raw = cdp_url()
    parsed = urlparse(raw if "://" in raw else "http://" + raw)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9222
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discussion_request_headers(
    url: str,
    *,
    cookie_loader: Callable[[], list[dict[str, Any]]] | None = None,
    port_open: Callable[[], bool] | None = None,
) -> dict[str, str]:
    """Chrome identity for Discussion API calls. The token host gets no cookies.

    Cookies come from the signed-in Chrome session when that port is open.
    Unit tests pass ``cookie_loader`` or ``port_open`` and do not attach.
    """
    headers = {"User-Agent": _CHROME_USER_AGENT}
    if _is_token_endpoint(url):
        return headers
    headers["Accept"] = "application/json"
    cookies = _discussion_cookies(cookie_loader=cookie_loader, port_open=port_open)
    if not cookies:
        return headers
    from .ezlynx_portal_session import format_cookie_header, portal_session_headers

    cookie_header = format_cookie_header(cookies)
    if not cookie_header:
        return headers
    parsed = urlparse(str(url or ""))
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    headers["Cookie"] = cookie_header
    if not origin:
        return headers
    try:
        portal = portal_session_headers(cookie_header, origin, cookies)
    except Exception:
        headers["Origin"] = origin
        headers["Referer"] = origin + "/"
        return headers
    for key, value in portal.items():
        if key == "Content-Type":
            continue
        headers[key] = value
    headers["User-Agent"] = _CHROME_USER_AGENT
    return headers


def _discussion_cookies(
    *,
    cookie_loader: Callable[[], list[dict[str, Any]]] | None,
    port_open: Callable[[], bool] | None,
) -> list[dict[str, Any]]:
    if cookie_loader is not None:
        return [row for row in (cookie_loader() or []) if isinstance(row, dict)]
    # Pytest must not open Chrome. Callers that want the session pass a loader
    # or port_open, which is how the live gateway attaches.
    if port_open is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    now = time.monotonic()
    cached_at = float(_BROWSER_CACHE.get("at") or 0)
    if now - cached_at < _BROWSER_CACHE_SECONDS and "cookies" in _BROWSER_CACHE:
        return list(_BROWSER_CACHE.get("cookies") or [])
    cookies: list[dict[str, Any]] = []
    opener = port_open or _cdp_port_open
    try:
        if opener():
            from .ezlynx_portal_session import load_cdp_session_cookies

            cookies = [
                row
                for row in (load_cdp_session_cookies() or [])
                if isinstance(row, dict)
            ]
    except Exception:
        cookies = []
    _BROWSER_CACHE["at"] = now
    _BROWSER_CACHE["cookies"] = cookies
    return list(cookies)


def reject_phone_numbers(body: str) -> str:
    """Refuse a note body containing a dialable phone number.

    The agency's call automation watches EZLynx discussion text and dials
    numbers it finds; discussion cards cannot be edited or removed afterwards.
    """
    text = str(body or "")
    match = _PHONE_LIKE.search(text)
    if match:
        raise DiscussionApiError(
            None,
            "note body contains a phone-number-like value "
            f"({match.group(0)!r}); remove it or write '(phone on file)' instead",
        )
    return text


class DiscussionApiClient:
    """Minimal Discussion API v8 client (lookup + append only)."""

    def __init__(
        self,
        config: DiscussionApiConfig,
        urlopen: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        *,
        session_headers: Callable[[str], dict[str, str]] | None = None,
    ) -> None:
        self._config = config
        self._urlopen = urlopen or _default_urlopen
        self._clock = clock or time.time
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._session_headers = session_headers or discussion_request_headers

    # -- authentication -------------------------------------------------

    def get_token(self) -> str:
        now = self._clock()
        if self._token and now < self._token_expires_at:
            return self._token
        form = {
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "grant_type": GRANT_TYPE,
            "scope": self._config.scope,
            "username": self._config.username,
            "integration_group_id": self._config.integration_group_id,
        }
        if str(self._config.password or "").strip():
            form["password"] = self._config.password
        body = self._request_json(
            "POST",
            self._config.token_endpoint,
            data=parse.urlencode(form).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            authenticated=False,
        )
        if not isinstance(body, dict):
            raise DiscussionApiError(None, "token endpoint returned unexpected shape")
        access_token = str(body.get("access_token") or "").strip()
        if not access_token:
            raise DiscussionApiError(None, "token endpoint returned no access_token")
        try:
            expires_in = int(body.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        self._token = access_token
        self._token_expires_at = now + max(expires_in - TOKEN_EXPIRY_SKEW_SECONDS, 60)
        return self._token

    # -- HTTP helpers ---------------------------------------------------

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None,
        headers: dict[str, str],
        authenticated: bool = True,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> Any:
        headers = dict(headers)
        try:
            extra = self._session_headers(url)
        except Exception:
            extra = {}
        if isinstance(extra, dict):
            for key, value in extra.items():
                headers.setdefault(key, value)
        if authenticated:
            headers["Authorization"] = f"Bearer {self.get_token()}"
        try:
            resp = self._urlopen(url, data=data, headers=headers, timeout=timeout)
            raw = resp.read()
        except error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001 - best-effort detail only
                detail = ""
            raise DiscussionApiError(
                exc.code, f"Discussion API {method} failed: HTTP {exc.code} {detail}"
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise DiscussionApiError(None, f"Discussion API {method} transport failed") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DiscussionApiError(None, "Discussion API returned non-JSON") from exc
        return parsed

    def _base(self) -> str:
        return self._config.discussion_base_url.rstrip("/") + "/"

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = self._base() + path.lstrip("/")
        if params:
            url += "?" + parse.urlencode(params)
        return self._request_json(
            "GET", url, data=None, headers={"Accept": "application/json"}
        )

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request_json(
            "POST",
            self._base() + path.lstrip("/"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    # -- discussion lookup ----------------------------------------------

    def get_discussion_ids(self, applicant_id: str) -> list[str]:
        """IDs of discussions on the applicant (v8 ids-by-applicant)."""
        applicant = str(applicant_id or "").strip()
        if not applicant:
            raise DiscussionApiError(None, "applicant id is required")
        parsed = self._get("v8/discussions/ids-by-applicant", {"applicantId": applicant})
        return _normalize_id_list(parsed)

    def get_discussions(self, applicant_id: str) -> list[dict[str, Any]]:
        """Full discussion records on the applicant (v8 by-applicant)."""
        applicant = str(applicant_id or "").strip()
        if not applicant:
            raise DiscussionApiError(None, "applicant id is required")
        parsed = self._get("v8/discussions/by-applicant", {"applicantId": applicant})
        return _normalize_record_list(parsed)

    def get_discussion(self, discussion_id: str) -> dict[str, Any]:
        """Single discussion by id (v8 discussions/:discussionId)."""
        discussion = str(discussion_id or "").strip()
        if not discussion:
            raise DiscussionApiError(None, "discussion id is required")
        parsed = self._get(f"v8/discussions/{parse.quote(discussion, safe='')}")
        if isinstance(parsed, dict):
            return parsed
        raise DiscussionApiError(None, "discussion lookup returned unexpected shape")

    # -- append ----------------------------------------------------------

    def append_note(
        self, discussion_id: str, body: str, *, note_type: str = "Note"
    ) -> dict[str, Any]:
        """Append a note to an EXISTING discussion (v8 discussions/:id/notes).

        Never creates a discussion. The body is refused when it contains a
        phone-number-like value.
        """
        discussion = str(discussion_id or "").strip()
        if not discussion:
            raise DiscussionApiError(None, "discussion id is required")
        text = reject_phone_numbers(body).strip()
        if not text:
            raise DiscussionApiError(None, "note body is required")
        parsed = self._post(
            f"v8/discussions/{parse.quote(discussion, safe='')}/notes",
            {"type": note_type, "body": text},
        )
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}


def _normalize_id_list(parsed: Any) -> list[str]:
    if isinstance(parsed, list):
        return [str(row).strip() for row in parsed if str(row).strip()]
    if isinstance(parsed, dict):
        for key in ("ids", "Ids", "discussionIds", "DiscussionIds", "items", "Items"):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                return [str(row).strip() for row in candidate if str(row).strip()]
    return []


def _normalize_record_list(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    if isinstance(parsed, dict):
        for key in ("discussions", "Discussions", "items", "Items", "data", "Data"):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                return [row for row in candidate if isinstance(row, dict)]
        if parsed.get("discussionId") or parsed.get("id") or parsed.get("title"):
            return [parsed]
    return []


def discussion_id_of(record: dict[str, Any]) -> str:
    """Best-effort discussion id from a record (API field names vary)."""
    for key in ("discussionId", "DiscussionId", "id", "Id"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def discussion_title_of(record: dict[str, Any]) -> str:
    for key in ("title", "Title", "subject", "Subject", "name", "Name"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def is_untitled_discussion(record: dict[str, Any]) -> bool:
    """True when the card has no usable title or is literally Untitled."""
    title = discussion_title_of(record)
    return (not title) or title.casefold() == "untitled"


def select_discussion_for_note(
    discussions: list[dict[str, Any]] | None, *, title_hint: str | None = None
) -> dict[str, Any]:
    """Choose the single existing titled discussion to append to, or raise.

    Untitled cards are never selected. A discussion is never created.

    - Exactly one titled discussion -> it wins.
    - Several titled discussions + a ``title_hint`` matching exactly one -> it wins.
    - Anything else -> :class:`DiscussionSelectionError`. The caller must not
      fall back to creating a discussion.
    """
    raw_rows = [row for row in (discussions or []) if isinstance(row, dict)]
    rows = [row for row in raw_rows if not is_untitled_discussion(row)]
    if not rows:
        if raw_rows:
            raise DiscussionSelectionError(
                UNTITLED_FORBIDDEN,
                "applicant has only Untitled or untitled discussions; "
                "refusing to write there or to create one",
            )
        raise DiscussionSelectionError(
            NO_DISCUSSIONS,
            "applicant has no discussions; refusing to create one (untitled "
            "discussions are forbidden)",
        )
    if len(rows) == 1:
        return rows[0]
    hint = str(title_hint or "").strip().lower()
    if hint:
        matched = [row for row in rows if hint in discussion_title_of(row).lower()]
        if len(matched) == 1:
            return matched[0]
        raise DiscussionSelectionError(
            AMBIGUOUS_DISCUSSIONS,
            f"title hint {title_hint!r} matched {len(matched)} of {len(rows)} "
            "discussions; refusing to guess",
        )
    raise DiscussionSelectionError(
        AMBIGUOUS_DISCUSSIONS,
        f"applicant has {len(rows)} discussions and no title hint was given; "
        "refusing to guess",
    )


def _note_body(row: Any) -> str:
    if isinstance(row, str):
        return row.strip()
    if not isinstance(row, dict):
        return ""
    for key in ("body", "Body", "text", "Text", "noteText", "NoteText"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _note_body(value)
            if nested:
                return nested
    return ""


def _note_id_of(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("noteId", "NoteId", "id", "Id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def iter_discussion_notes(record: Any):
    """Yield note dicts from a discussion payload. Does not invent ids."""
    if isinstance(record, list):
        for item in record:
            yield from iter_discussion_notes(item)
        return
    if not isinstance(record, dict):
        return
    body = _note_body(record)
    if body and (
        _note_id_of(record)
        or any(key in record for key in ("body", "Body", "text", "Text"))
    ):
        yield record
    for key in ("notes", "Notes", "items", "Items", "data", "Data"):
        rows = record.get(key)
        if isinstance(rows, list):
            for row in rows:
                yield from iter_discussion_notes(row)


_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")


def _payload_has_note_bodies(record: Any) -> bool:
    for row in iter_discussion_notes(record):
        if str(_note_body(row) or "").strip():
            return True
    return False


def _posted_text_matches(record: Any, note_body: str) -> bool:
    """True when a note body matches, ignoring case and extra whitespace."""

    def _norm(value: str) -> str:
        cleaned = _ZERO_WIDTH_RE.sub("", str(value or ""))
        return " ".join(cleaned.casefold().split())

    want = _norm(note_body)
    if not want:
        return False
    for row in iter_discussion_notes(record):
        if _norm(_note_body(row)) == want:
            return True
    return False


def _same_note_text(left: str, right: str) -> bool:
    def _norm(value: str) -> str:
        cleaned = _ZERO_WIDTH_RE.sub("", str(value or ""))
        return " ".join(cleaned.split())

    return _norm(left) == _norm(right)


def find_identical_note(record: Any, note_body: str) -> dict[str, Any] | None:
    """The note already on this discussion whose text matches, if the payload has bodies.

    Live discussion reads do not include note text, so filing does not use
    this as a duplicate guard. Accepted notes are remembered in the local ledger.
    """
    want = str(note_body or "").strip()
    if not want:
        return None
    for row in iter_discussion_notes(record):
        if _same_note_text(_note_body(row), want):
            return row
    return None


def discussion_note_snapshot(record: Any) -> dict[str, Any]:
    """Title, note count, and latest note id from a discussion metadata read.

    Live ``GET v8/discussions/{id}`` returns those fields and no note bodies.
    """

    if not isinstance(record, dict):
        return {"title": "", "note_count": None, "most_recent_note_id": ""}
    raw_count = record.get("noteCount", record.get("NoteCount"))
    count: int | None
    if isinstance(raw_count, bool) or raw_count is None:
        count = None
    elif isinstance(raw_count, int):
        count = raw_count
    elif isinstance(raw_count, str) and raw_count.strip().isdigit():
        count = int(raw_count.strip())
    else:
        count = None
    recent = str(record.get("mostRecentNoteId", record.get("MostRecentNoteId", "")) or "").strip()
    return {
        "title": discussion_title_of(record),
        "note_count": count,
        "most_recent_note_id": recent,
    }


def _metadata_note_confirmation(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[bool, str]:
    """True only when the discussion gained exactly one note and a new latest id.

    The wording stays plain English. Callers show ``reason`` to people.
    """

    before_count = before.get("note_count")
    after_count = after.get("note_count")
    if not isinstance(before_count, int) or not isinstance(after_count, int):
        return False, (
            "The note was sent, but the discussion could not be confirmed. "
            "It was not sent again."
        )
    gained_one = after_count == before_count + 1
    latest = str(after.get("most_recent_note_id") or "")
    latest_changed = bool(latest) and latest != str(before.get("most_recent_note_id") or "")
    title = str(after.get("title") or "")
    title_same = bool(title) and title == str(before.get("title") or "")
    if gained_one and latest_changed and title_same:
        return True, "The note was added to the discussion."
    if after_count == before_count:
        return False, (
            "The note was sent, but the discussion still has the same notes. "
            "It was not sent again."
        )
    if not gained_one:
        return False, (
            "The note was sent, but the discussion did not show exactly one new note. "
            "It was not sent again."
        )
    if not latest_changed:
        return False, (
            "The note was sent, but the latest note did not change. "
            "It was not sent again."
        )
    return False, (
        "The note was sent, but the discussion title changed. It was not sent again."
    )


def file_note_to_existing_discussion(
    client: DiscussionApiClient,
    applicant_id: str,
    note_body: str,
    *,
    title_hint: str | None = None,
    note_type: str = "Note",
    dry_run: bool = False,
    document_id: str | None = None,
    ledger_path: Any = None,
    allow_repost: bool = False,
) -> dict[str, Any]:
    """Append ``note_body`` to the applicant's existing discussion.

    Fail-closed: the write-scope allowlist is enforced first; when no single
    existing discussion can be chosen the result is ``status="pending"`` and
    nothing is written. A discussion is never created and nothing is deleted.

    Confirmation reads the discussion before the post and once after it.
    The note is filed only when a second signal says the new note is ours
    (its text, or a returned note id that is the new latest id). Live
    EZLynx has neither: the post body has no id, and the discussion read
    has no note text. A tight second read that still shows exactly one new
    note, a new latest id, and the same title is the best that API can
    give. That is reported as sent, not filed, so the job is not called
    done. The ledger row stays sent, unconfirmed and blocks a repost. The
    post is never repeated automatically.

    A local ledger remembers accepted notes so a rerun does not post them
    again. A send that cannot be confirmed is stored as sent, unconfirmed
    and blocks a repost until a person says yes or the 24-hour question
    window passes. If that ledger write fails, nothing is posted.

    Returns a result dict with ``status`` one of ``filed`` / ``pending`` /
    ``held`` / ``dry_run``, plus ``applicant_id``, ``discussion_id``,
    ``note_id`` and a human-readable ``reason``.
    """
    applicant = require_allowed_ezlynx_write_applicant(applicant_id)
    text = reject_phone_numbers(note_body).strip()
    if not text:
        raise DiscussionApiError(None, "note body is required")

    discussions = client.get_discussions(applicant)
    try:
        record = select_discussion_for_note(discussions, title_hint=title_hint)
    except DiscussionSelectionError as exc:
        return {
            "status": "pending",
            "reason_code": exc.code,
            "reason": str(exc),
            "applicant_id": applicant,
            "discussion_id": None,
            "note_id": None,
        }
    discussion_id = discussion_id_of(record)
    if not discussion_id:
        return {
            "status": "pending",
            "reason_code": AMBIGUOUS_DISCUSSIONS,
            "reason": "selected discussion has no usable id; refusing to write",
            "applicant_id": applicant,
            "discussion_id": None,
            "note_id": None,
        }
    title = discussion_title_of(record)
    if dry_run:
        return {
            "status": "dry_run",
            "reason_code": None,
            "reason": "dry run: note validated, nothing written",
            "applicant_id": applicant,
            "discussion_id": discussion_id,
            "discussion_title": title,
            "note_id": None,
        }
    from .discussion_note_ledger import (
        DiscussionNoteLedgerError,
        SENT_UNCONFIRMED,
        already_added_question,
        begin_unconfirmed_note,
        find_posted_note,
        find_recent_same_text,
        note_still_blocks_repost,
        note_was_unconfirmed,
        undo_unconfirmed_note,
    )

    doc_id = str(document_id or "").strip()
    try:
        already = find_posted_note(
            applicant,
            discussion_id,
            text,
            document_id=doc_id,
            ledger_path=ledger_path,
        )
        recent = find_recent_same_text(
            applicant,
            discussion_id,
            text,
            ledger_path=ledger_path,
        )
    except DiscussionNoteLedgerError as exc:
        return _note_result(
            "held",
            reason=str(exc),
            applicant=applicant,
            discussion_id=discussion_id,
            title=title,
        )
    # The same downloaded document is not posted again. A repeated note
    # ask in the last day asks before posting, and does not count as done.
    if already is not None and doc_id and str(already.get("document_id") or "").strip() == doc_id:
        if note_was_unconfirmed(already):
            if not allow_repost and note_still_blocks_repost(already):
                return _note_result(
                    "already_posted",
                    reason=already_added_question(already.get("posted_at")),
                    applicant=applicant,
                    discussion_id=discussion_id,
                    title=title,
                    note_id=str(already.get("note_id") or "").strip() or None,
                    read_back=False,
                    verified_by=None,
                    confirmation=SENT_UNCONFIRMED,
                )
        else:
            remembered = str(already.get("note_id") or "").strip()
            return _note_result(
                "filed",
                reason="This note was already sent, so it was not sent again.",
                applicant=applicant,
                discussion_id=discussion_id,
                title=title,
                note_id=remembered or None,
                read_back=True,
                verified_by="ledger",
                idempotent=True,
            )
    if recent is not None and not allow_repost:
        return _note_result(
            "already_posted",
            reason=already_added_question(recent.get("posted_at")),
            applicant=applicant,
            discussion_id=discussion_id,
            title=title,
            note_id=str(recent.get("note_id") or "").strip() or None,
            read_back=False,
            verified_by=None,
            confirmation=SENT_UNCONFIRMED if note_was_unconfirmed(recent) else None,
        )
    getter = getattr(client, "get_discussion", None)
    if not callable(getter):
        return _note_result(
            "held",
            reason="The discussion could not be read, so the note was not sent.",
            applicant=applicant,
            discussion_id=discussion_id,
            title=title,
        )
    try:
        before = discussion_note_snapshot(getter(discussion_id))
    except Exception:
        return _note_result(
            "held",
            reason="The discussion could not be read, so the note was not sent.",
            applicant=applicant,
            discussion_id=discussion_id,
            title=title,
        )
    try:
        prior_row = begin_unconfirmed_note(
            applicant,
            discussion_id,
            note_text=text,
            document_id=doc_id,
            ledger_path=ledger_path,
        )
    except DiscussionNoteLedgerError as exc:
        return _note_result(
            "held",
            reason=str(exc),
            applicant=applicant,
            discussion_id=discussion_id,
            title=title,
        )
    try:
        created = client.append_note(discussion_id, text, note_type=note_type)
    except Exception:
        try:
            undo_unconfirmed_note(
                applicant,
                discussion_id,
                note_text=text,
                document_id=doc_id,
                previous=prior_row,
                ledger_path=ledger_path,
            )
        except DiscussionNoteLedgerError:
            pass
        raise
    try:
        after_record = getter(discussion_id)
    except Exception:
        return _unconfirmed_note_result(
            reason=(
                "The note was sent, but the discussion could not be read afterward. "
                "It was not sent again."
            ),
            applicant=applicant,
            discussion_id=discussion_id,
            title=title,
            response=created,
        )
    after = discussion_note_snapshot(after_record)
    if before["note_count"] is not None or after["note_count"] is not None:
        confirmed, reason = _metadata_note_confirmation(before, after)
        if not confirmed:
            return _unconfirmed_note_result(
                reason=reason,
                applicant=applicant,
                discussion_id=discussion_id,
                title=title,
                response=created,
            )
        identity = _new_note_identity(after_record, after, text, created)
        if identity is None:
            returned = _note_id_of(created) if isinstance(created, dict) else ""
            if not returned and not _payload_has_note_bodies(after_record):
                stable_id = _stable_count_reread(getter, discussion_id, after)
                if stable_id:
                    _remember_observed_note_id(
                        applicant,
                        discussion_id,
                        text,
                        document_id=doc_id,
                        note_id=stable_id,
                        ledger_path=ledger_path,
                    )
                    return _note_result(
                        "sent",
                        reason=(
                            "The note was added. The discussion shows one new note, "
                            "but its text could not be read. It was not sent again."
                        ),
                        applicant=applicant,
                        discussion_id=discussion_id,
                        title=title,
                        note_id=stable_id,
                        read_back=False,
                        verified_by="count",
                        confirmation=SENT_UNCONFIRMED,
                        response=created,
                    )
            if _payload_has_note_bodies(after_record) and not _posted_text_matches(
                after_record, text
            ):
                hold_reason = (
                    "The note was sent, but the new text did not match. "
                    "It was not sent again."
                )
            else:
                hold_reason = (
                    "The note was sent, but it could not be told apart from another note. "
                    "It was not sent again."
                )
            return _unconfirmed_note_result(
                reason=hold_reason,
                applicant=applicant,
                discussion_id=discussion_id,
                title=title,
                response=created,
            )
        note_id, verified_by = identity
        _remember_posted_note(
            applicant,
            discussion_id,
            text,
            document_id=doc_id,
            note_id=note_id,
            ledger_path=ledger_path,
            source="discussion_count" if verified_by == "text" else "returned_note_id",
        )
        return _note_result(
            "filed",
            reason=reason,
            applicant=applicant,
            discussion_id=discussion_id,
            title=title,
            note_id=note_id,
            read_back=True,
            verified_by=verified_by,
            response=created,
        )
    # Older payloads omit the count. Accept only an id that a fresh read shows.
    note_id = _note_id_of(created) if isinstance(created, dict) else ""
    if not note_id:
        return _unconfirmed_note_result(
            reason=(
                "The note was sent, but it could not be confirmed. It was not sent again."
            ),
            applicant=applicant,
            discussion_id=discussion_id,
            title=title,
            response=created,
        )
    from .ezlynx_api_only_writes import confirm_discussion_note

    confirm_discussion_note(client, discussion_id, note_id)
    _remember_posted_note(
        applicant,
        discussion_id,
        text,
        document_id=doc_id,
        note_id=note_id,
        ledger_path=ledger_path,
        source="returned_note_id",
    )
    return _note_result(
        "filed",
        reason="The note was added to the discussion.",
        applicant=applicant,
        discussion_id=discussion_id,
        title=title,
        note_id=note_id,
        read_back=True,
        verified_by="note_id",
        response=created,
    )


def _stable_count_reread(
    getter: Any, discussion_id: str, after: dict[str, Any]
) -> str | None:
    """Latest id when a second read still shows the same one new note.

    Live reads have no note text. A second read that moved, failed, or
    grew a note body is not this signal.
    """

    try:
        record = getter(discussion_id)
    except Exception:
        return None
    if _payload_has_note_bodies(record):
        return None
    again = discussion_note_snapshot(record)
    latest = str(again.get("most_recent_note_id") or "")
    same = (
        again.get("note_count") == after.get("note_count")
        and bool(latest)
        and latest == str(after.get("most_recent_note_id") or "")
        and str(again.get("title") or "") == str(after.get("title") or "")
    )
    if not same:
        return None
    return latest


def _remember_observed_note_id(
    applicant_id: str,
    discussion_id: str,
    note_text: str,
    *,
    document_id: str,
    note_id: str,
    ledger_path: Any,
) -> None:
    """Store the id we saw. The row stays sent, unconfirmed."""

    from .discussion_note_ledger import (
        SENT_UNCONFIRMED,
        DiscussionNoteLedgerError,
        record_posted_note,
    )

    try:
        record_posted_note(
            applicant_id,
            discussion_id,
            note_text=note_text,
            document_id=document_id,
            note_id=note_id,
            source="discussion_count",
            ledger_path=ledger_path,
            refresh=True,
            confirmation=SENT_UNCONFIRMED,
        )
    except DiscussionNoteLedgerError:
        return


def _new_note_identity(
    after_record: Any,
    after: dict[str, Any],
    note_text: str,
    created: Any,
) -> tuple[str, str] | None:
    """Id of the note we just added, and how we know it is ours.

    A higher count alone is not that signal. The live discussion read has no
    note text, so a different note that landed in the same gap must not be
    marked done.
    """

    latest = str(after.get("most_recent_note_id") or "")
    returned = _note_id_of(created) if isinstance(created, dict) else ""
    if _payload_has_note_bodies(after_record):
        latest_rows = [
            row
            for row in iter_discussion_notes(after_record)
            if latest and _note_id_of(row) == latest
        ]
        if latest_rows:
            if any(_posted_text_matches(row, note_text) for row in latest_rows):
                return latest, "text"
            return None
        if returned and latest and returned == latest:
            return latest, "note_id"
        return None
    if returned and latest and returned == latest:
        return latest, "note_id"
    return None


def _note_result(
    status: str,
    *,
    reason: str,
    applicant: str,
    discussion_id: str | None,
    title: str,
    note_id: str | None = None,
    read_back: bool = False,
    verified_by: str | None = None,
    idempotent: bool = False,
    response: Any = None,
    confirmation: str | None = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "reason_code": None,
        "reason": reason,
        "applicant_id": applicant,
        "discussion_id": discussion_id,
        "discussion_title": title,
        "note_id": note_id,
        "read_back": read_back,
        "verified_by": verified_by,
    }
    if confirmation:
        result["confirmation"] = confirmation
    if idempotent:
        result["idempotent"] = True
    if response is not None:
        result["response"] = response
    return result


def _unconfirmed_note_result(**kwargs: Any) -> dict[str, Any]:
    """Held, not filed. The ledger already says sent, unconfirmed."""

    from .discussion_note_ledger import SENT_UNCONFIRMED

    return _note_result("held", confirmation=SENT_UNCONFIRMED, **kwargs)


def _remember_posted_note(
    applicant_id: str,
    discussion_id: str,
    note_text: str,
    *,
    document_id: str,
    note_id: str,
    ledger_path: Any,
    source: str,
) -> None:
    """Upgrade the pre-post row to confirmed. A failed upgrade stays unconfirmed."""

    from .discussion_note_ledger import CONFIRMED, DiscussionNoteLedgerError, record_posted_note

    try:
        record_posted_note(
            applicant_id,
            discussion_id,
            note_text=note_text,
            document_id=document_id,
            note_id=note_id,
            source=source,
            ledger_path=ledger_path,
            refresh=True,
            confirmation=CONFIRMED,
        )
    except DiscussionNoteLedgerError:
        return


# ---------------------------------------------------------------------------
# Named discussion creation (Carlo's 2026-09-28 standing authorization for the
# certificate sweep only). POST v8/discussions/with-note.
# ---------------------------------------------------------------------------

WITH_NOTE_PATH = "v8/discussions/with-note"


def build_with_note_payload(
    applicant_id: str | int,
    title: str,
    note_body: str,
    note_type: str = "Note",
) -> dict[str, Any]:
    """Pure builder for the ``POST v8/discussions/with-note`` body.

    SHAPE UNVERIFIED (2026-09-28): the public Postman documentation names the
    endpoint and says it creates a discussion with a note and returns a
    DiscussionId, but does not show the request body. UAT probing was blocked
    because no valid UAT applicant id is on file (220250093 is
    Production-only). The shape below is the documented camelCase convention
    used by the rest of the v8 Discussion API. If EZLynx rejects it, the
    caller's 400 detail surfaces in the error and the filing holds
    UNVERIFIED — never silently FILED.
    """
    applicant = str(applicant_id or "").strip()
    heading = str(title or "").strip()
    text = str(note_body or "").strip()
    if not applicant:
        raise DiscussionApiError(None, "applicant id is required")
    if not heading or heading.casefold() == "untitled":
        raise DiscussionApiError(
            None, "a real discussion title is required; Untitled is forbidden"
        )
    if not text:
        raise DiscussionApiError(None, "note body is required")
    return {
        "applicantId": applicant,
        "title": heading,
        "note": {"type": note_type, "body": text},
    }


def create_discussion_with_note(
    client: DiscussionApiClient,
    applicant_id: str | int,
    title: str,
    note_body: str,
    *,
    note_type: str = "Note",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a NAMED discussion with the note as its first note.

    Fail-closed: the write-scope allowlist and the no-phone-number note guard
    run before any HTTP. The response's DiscussionId is then proven by a
    fresh ``GET v8/discussions/{id}`` that must show the returned discussion
    exists, carries the requested title exactly, and has noteCount >= 1.
    Anything else raises :class:`DiscussionApiError`; the caller must hold
    the request UNVERIFIED.

    Returns a result dict with ``status`` ``created`` / ``dry_run`` plus
    ``applicant_id``, ``discussion_id``, ``discussion_title``, ``note_id``
    (mostRecentNoteId from the read-back), ``read_back`` and ``response``.
    """
    applicant = require_allowed_ezlynx_write_applicant(applicant_id)
    heading = str(title or "").strip()
    text = reject_phone_numbers(note_body).strip()
    if not heading or heading.casefold() == "untitled":
        raise DiscussionApiError(
            None, "a real discussion title is required; Untitled is forbidden"
        )
    if not text:
        raise DiscussionApiError(None, "note body is required")

    if dry_run:
        return {
            "status": "dry_run",
            "reason": "dry run: create payload validated, nothing written",
            "applicant_id": applicant,
            "discussion_id": None,
            "discussion_title": heading,
            "note_id": None,
            "read_back": False,
            "response": None,
        }

    payload = build_with_note_payload(applicant, heading, text,
                                      note_type=note_type)
    created = client._post(WITH_NOTE_PATH, payload)
    discussion_id = ""
    if isinstance(created, dict):
        for key in ("discussionId", "DiscussionId", "id", "Id"):
            value = str(created.get(key) or "").strip()
            if value:
                discussion_id = value
                break
    if not discussion_id:
        raise DiscussionApiError(
            None,
            "DiscussionApi with-note returned no DiscussionId; "
            "refusing success",
        )

    # Mandatory fresh GET read-back. This is the destination proof:
    # the discussion must exist, carry the requested title exactly, and
    # already contain at least one note.
    record = client.get_discussion(discussion_id)
    if not isinstance(record, dict) or not record:
        raise DiscussionApiError(
            None,
            f"DiscussionApi read-back failed: discussion {discussion_id} "
            "did not read back after creation",
        )
    seen_title = discussion_title_of(record)
    if seen_title != heading:
        raise DiscussionApiError(
            None,
            f"DiscussionApi read-back failed: created discussion "
            f"{discussion_id} has title {seen_title!r}, expected "
            f"{heading!r}",
        )
    note_count = 0
    for key in ("noteCount", "NoteCount", "note_count"):
        try:
            note_count = int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
        break
    if note_count < 1:
        raise DiscussionApiError(
            None,
            f"DiscussionApi read-back failed: discussion {discussion_id} "
            f"shows noteCount={note_count}; the first note did not land",
        )
    note_id = ""
    for key in ("mostRecentNoteId", "MostRecentNoteId"):
        value = str(record.get(key) or "").strip()
        if value:
            note_id = value
            break
    return {
        "status": "created",
        "reason": "named discussion created with the filing note",
        "applicant_id": applicant,
        "discussion_id": discussion_id,
        "discussion_title": heading,
        "note_id": note_id,
        "read_back": True,
        "response": created,
    }
