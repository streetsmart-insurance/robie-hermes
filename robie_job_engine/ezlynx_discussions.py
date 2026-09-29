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
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib import error, parse, request

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


def _default_urlopen(url: str, *, data: bytes | None, headers: dict[str, str], timeout: int):
    req = request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    return request.urlopen(req, timeout=timeout)


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
    ) -> None:
        self._config = config
        self._urlopen = urlopen or _default_urlopen
        self._clock = clock or time.time
        self._token: str | None = None
        self._token_expires_at: float = 0.0

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


def discussion_last_active_of(record: dict[str, Any]) -> str:
    """Best-effort last-activity timestamp of a discussion record.

    Prefers ``lastModified``, falls back to ``created``. Returns the raw
    string (or "" when neither is present); callers parse defensively.
    """
    for key in ("lastModified", "LastModified", "last_modified",
                "mostRecentNoteDate", "created", "Created", "createdDate"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def _parse_activity_ts(value: str) -> float:
    """Parse an activity timestamp to epoch seconds; unparseable -> 0.0."""
    text = (value or "").strip()
    if not text:
        return 0.0
    try:
        # fromisoformat handles "2026-09-27T20:00:00" and offsets; normalize
        # the trailing-Z form first.
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:  # noqa: BLE001 - any unparsable stamp sorts oldest
        return 0.0
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001 - defensive; unparsable sorts oldest
        return 0.0


def most_recently_active_discussion(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the titled discussion with the latest activity stamp.

    Ties (including all-unparsable stamps) break by original order, so the
    choice is deterministic. Callers must only pass titled rows.
    """
    return max(
        rows,
        key=lambda row: (
            _parse_activity_ts(discussion_last_active_of(row)),
            -rows.index(row),
        ),
    )


def select_discussion_for_note(
    discussions: list[dict[str, Any]] | None, *, title_hint: str | None = None
) -> dict[str, Any]:
    """Choose the single existing titled discussion to append to, or raise.

    Untitled cards are never selected. A discussion is never created.

    - A supplied ``title_hint`` must match exactly one titled discussion.
      Zero or multiple matches hold for human review; recency never overrides
      an absent or ambiguous title-pattern match.
    - Without a title hint, an applicant's sole titled discussion may be
      selected. Multiple discussions without a hint hold for review.
    - A discussion is never created as a fallback.
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
    hint = str(title_hint or "").strip().lower()
    if hint:
        matched = [row for row in rows if hint in discussion_title_of(row).lower()]
        if len(matched) == 1:
            return matched[0]
        if not matched:
            raise DiscussionSelectionError(
                AMBIGUOUS_DISCUSSIONS,
                f"title hint {title_hint!r} matched none of {len(rows)} "
                "discussions; holding for human review",
            )
        raise DiscussionSelectionError(
            AMBIGUOUS_DISCUSSIONS,
            f"title hint {title_hint!r} matched {len(matched)} of {len(rows)} "
            "discussions; refusing to guess",
        )
    if len(rows) == 1:
        return rows[0]
    raise DiscussionSelectionError(
        AMBIGUOUS_DISCUSSIONS,
        f"applicant has {len(rows)} discussions and no title hint was given; "
        "refusing to guess",
    )


def file_note_to_existing_discussion(
    client: DiscussionApiClient,
    applicant_id: str,
    note_body: str,
    *,
    title_hint: str | None = None,
    note_type: str = "Note",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Append ``note_body`` to the applicant's existing discussion.

    Fail-closed: the write-scope allowlist is enforced first; when no single
    existing discussion can be chosen the result is ``status="pending"`` and
    nothing is written. A discussion is never created and nothing is deleted.

    Returns a result dict with ``status`` one of ``filed`` / ``pending`` /
    ``dry_run``, plus ``applicant_id``, ``discussion_id``, ``note_id`` and a
    human-readable ``reason``.
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
    if dry_run:
        return {
            "status": "dry_run",
            "reason_code": None,
            "reason": "dry run: note validated, nothing written",
            "applicant_id": applicant,
            "discussion_id": discussion_id,
            "discussion_title": discussion_title_of(record),
            "note_id": None,
        }
    created = client.append_note(discussion_id, text, note_type=note_type)
    note_id = ""
    if isinstance(created, dict):
        for key in ("noteId", "NoteId", "id", "Id"):
            value = str(created.get(key) or "").strip()
            if value:
                note_id = value
                break
    if not note_id:
        raise DiscussionApiError(
            None,
            "DiscussionApi append returned no note_id; refusing success",
        )
    # Fresh GET before success. Playwright/DOM is never this proof.
    from .ezlynx_api_only_writes import confirm_discussion_note

    confirm_discussion_note(client, discussion_id, note_id)
    return {
        "status": "filed",
        "reason_code": None,
        "reason": "note appended to existing discussion",
        "applicant_id": applicant,
        "discussion_id": discussion_id,
        "discussion_title": discussion_title_of(record),
        "note_id": note_id,
        "read_back": True,
        "response": created,
    }


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
