"""Direct EZLynx Task API: TaskCreationNote on an applicant discussion.

Zapier is the fallback. This module is the primary path when it can run.
Ported from #732's ``ezlynx_task_api.py``. Login, token cache, the
one-failed-login stop, and the user id map follow the phone watchdog
(streetsmart-phone-watchdog ``src/ezlynx/direct_task_api.py``).

Login: ``grant_type=vendor_data_access`` with client_id, client_secret,
token endpoint, and integration_group_id from the ROBIE EZLynx API secret
(``load_ezlynx_api_config``). The acting EZLynx username comes from
``ROBIE_EZLYNX_TASK_API_ACT_AS_USERNAME``. No password is read and
``vendor_username`` is never sent. ``ssr_user*`` is refused before any
token POST because that vendor user 403s on agency applicants.

One failed login opens a 60 minute stop for that username, in memory and
on disk, so a bad grant is not retried every task. Tokens are cached in
memory and in a 0600 file.

``ROBIE_EZLYNX_DIRECT_TASK_API_ENABLED=0`` forces Zapier only. Unset is on,
but the API also stays off while the act-as username is unset. Ship the
flag at 0 until the live proof passes.

Honesty (builds on #759): ``created`` only after the new note is read
back as a TaskCreationNote with the requested assignedUserId. A failure
before the task POST is safe to send to Zapier. A POST that may have
landed but was not confirmed is ``unverified``: not a success, and not
sent to Zapier either, because that could make a duplicate task.

Writes go through the same gates as notes: the EZLynx write allowlist
(and driver lease) and the no-phone-number body guard, before any HTTP.

Discussion: the caller's id, else a unique match for a title hint, else
the exact ``Tasks by Robie`` discussion. When the applicant has no
``Tasks by Robie`` discussion and the caller named none, it is created
with the task as its first note (POST /v8/discussions/with-note, the
watchdog's shape for ``Phone Call by Robie``). Carlo authorized this on
2026-10-04 for that exact title only, on allowlisted applicants, with a
read-back: the new discussion must read back with that title and the
task note with the requested assignee. A failed or timed-out create is
settled by listing the applicant's discussions again. If no ``Tasks by
Robie`` discussion appeared, nothing was written and Zapier may run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib import error, parse, request
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DIRECT_TASK_API_ENV = "ROBIE_EZLYNX_DIRECT_TASK_API_ENABLED"
ACT_AS_ENV = "ROBIE_EZLYNX_TASK_API_ACT_AS_USERNAME"
STATE_DIR_ENV = "ROBIE_EZLYNX_TASK_API_STATE_DIR"
DEFAULT_STATE_DIR = Path.home() / ".robie" / "ezlynx-task-api"

GRANT_TYPE = "vendor_data_access"
DEFAULT_SCOPE = "DiscussionApi openid"
TOKEN_EXPIRY_SKEW_SECONDS = 60
BREAKER_SECONDS = 60 * 60
HTTP_TIMEOUT_SECONDS = 15

# A discussion with this exact title is used when the caller names none.
ROBIE_TASK_DISCUSSION_TITLE = "Tasks by Robie"

# Cloudflare error 1010 rejects urllib's default User-Agent.
CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_EASTERN = ZoneInfo("America/New_York")

# Statuses where nothing was written to EZLynx, so Zapier may run.
CREATED = "created"
UNVERIFIED = "unverified"
DRY_RUN = "dry_run"

_memory_tokens: dict[str, tuple[str, float]] = {}
_memory_breakers: dict[str, float] = {}
_logged_once: set[str] = set()

UrlOpen = Callable[..., Any]


class EZLynxTaskApiError(RuntimeError):
    """Task creation failed."""


def zapier_fallback_allowed(result: dict[str, Any]) -> bool:
    """True when the direct attempt wrote nothing, so Zapier may run."""
    return str(result.get("status") or "") not in {CREATED, UNVERIFIED, DRY_RUN}


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------


def direct_task_api_enabled() -> bool:
    """Kill switch. Unset or blank is on. 0/false/no/off is Zapier only."""
    raw = os.getenv(DIRECT_TASK_API_ENV)
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def act_as_username() -> str:
    """EZLynx user the vendor grant acts as. Empty means the API is off."""
    return os.getenv(ACT_AS_ENV, "").strip()


def is_vendor_integration_username(username: str) -> bool:
    """``ssr_userPROD`` and any ``ssr_user*`` name. SSRobie does not match."""
    folded = (username or "").strip().casefold()
    return folded == "ssr_userprod" or folded.startswith("ssr_user")


def _log_once(key: str, message: str, *args: Any) -> None:
    if key in _logged_once:
        return
    _logged_once.add(key)
    logger.warning(message, *args)


def clear_runtime_caches() -> None:
    """Drop in-memory token, stop, and one-shot log state. Disk is kept."""
    _memory_tokens.clear()
    _memory_breakers.clear()
    _logged_once.clear()


def _now() -> float:
    return datetime.now().timestamp()


# ---------------------------------------------------------------------------
# Token cache and one-failed-login stop (disk state is 0600)
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    raw = os.getenv(STATE_DIR_ENV, "").strip()
    base = Path(raw) if raw else DEFAULT_STATE_DIR
    return base / "ezlynx_task_api_state.json"


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tokens": {}, "breakers": {}}
    if not isinstance(data, dict):
        return {"tokens": {}, "breakers": {}}
    data.setdefault("tokens", {})
    data.setdefault("breakers", {})
    return data


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".ezlynx-task-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(state, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp_name, path)
    os.chmod(path, 0o600)


def _breaker_until(username: str) -> Optional[float]:
    candidates = []
    if username in _memory_breakers:
        candidates.append(_memory_breakers[username])
    row = (_read_state().get("breakers") or {}).get(username)
    if isinstance(row, dict):
        try:
            candidates.append(float(row.get("until")))
        except (TypeError, ValueError):
            pass
    return max(candidates) if candidates else None


def breaker_open(username: str) -> bool:
    until = _breaker_until(username)
    return until is not None and _now() < until


def open_breaker(username: str, reason: str) -> float:
    until = _now() + BREAKER_SECONDS
    _memory_breakers[username] = until
    state = _read_state()
    state.setdefault("breakers", {})[username] = {"until": until, "reason": reason[:200]}
    _write_state(state)
    return until


def cached_token(username: str) -> Optional[str]:
    now = _now()
    mem = _memory_tokens.get(username)
    if mem and now < mem[1]:
        return mem[0]
    row = (_read_state().get("tokens") or {}).get(username)
    if isinstance(row, dict):
        try:
            expires_at = float(row.get("expires_at"))
        except (TypeError, ValueError):
            return None
        token = str(row.get("access_token") or "")
        if token and now < expires_at:
            _memory_tokens[username] = (token, expires_at)
            return token
    return None


def store_token(username: str, token: str, expires_in: int) -> None:
    expires_at = _now() + max(int(expires_in) - TOKEN_EXPIRY_SKEW_SECONDS, 60)
    _memory_tokens[username] = (token, expires_at)
    state = _read_state()
    state.setdefault("tokens", {})[username] = {
        "access_token": token,
        "expires_at": expires_at,
    }
    _write_state(state)


# ---------------------------------------------------------------------------
# App config and HTTP
# ---------------------------------------------------------------------------


def load_app_config(accessor: Any = None) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Client settings from the ROBIE EZLynx API secret. No password.

    ``ROBIE_EZLYNX_DISCUSSION_API=live`` reads the Production secret, the
    same switch the note tool uses for Buster Brown on Test. A live target
    that points at UAT is refused.
    """
    from .ezlynx_api import load_ezlynx_api_config
    from .ezlynx_api_only_writes import LIVE_DISCUSSION_API, discussion_api_target

    try:
        live = discussion_api_target() == LIVE_DISCUSSION_API
        api = load_ezlynx_api_config(
            environment="PRODUCTION" if live else None, accessor=accessor
        )
    except Exception as exc:  # noqa: BLE001 - reason only, never secret text
        return None, f"EZLynx API secret unavailable ({type(exc).__name__})"
    parsed = urlparse(str(api.document_base_url or api.token_endpoint))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if live and "uatezlynx" in origin.casefold():
        return None, "live Discussion API target points at UAT"
    return {
        "client_id": str(api.client_id),
        "client_secret": str(api.client_secret),
        "scope": DEFAULT_SCOPE,
        "token_endpoint": str(api.token_endpoint),
        "integration_group_id": str(api.integration_group_id),
        "discussion_base": origin + "/DiscussionApi",
    }, None


def token_form(app: dict[str, str], username: str) -> dict[str, str]:
    """vendor_data_access form, same as the watchdog. No password."""
    return {
        "client_id": app["client_id"],
        "client_secret": app["client_secret"],
        "grant_type": GRANT_TYPE,
        "scope": app["scope"],
        "username": username,
        "integration_group_id": app.get("integration_group_id") or "159",
    }


def redact_body(text: str, secrets: Optional[list[str]] = None) -> str:
    """Shorten an HTTP body and strip credential material before logging."""
    out = str(text or "")[:500]
    for secret in secrets or []:
        if secret:
            out = out.replace(str(secret), "[redacted]")
    return re.sub(
        r'(?i)(password|client_secret|access_token|refresh_token|client_id)"?\s*[:=]\s*"?[^"&\s,}]+',
        r"\1=[redacted]",
        out,
    )


def _default_urlopen(req: request.Request, timeout: int) -> Any:
    return request.urlopen(req, timeout=timeout)


def _send(
    req: request.Request, urlopen: UrlOpen, secrets: list[str]
) -> tuple[Optional[int], str, Optional[str]]:
    """(HTTP status, body, transport error name). Bodies are redacted."""
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return int(getattr(resp, "status", 200)), body, None
    except error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        return int(exc.code), redact_body(body, secrets), None
    except Exception as exc:  # noqa: BLE001 - timeout, reset, DNS
        return None, "", type(exc).__name__


def _headers(token: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": CHROME_USER_AGENT,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def fetch_token_once(
    app: dict[str, str], username: str, urlopen: UrlOpen = _default_urlopen
) -> Optional[str]:
    """One token request. Any failure opens the stop and does not retry."""
    if is_vendor_integration_username(username) or breaker_open(username):
        return None
    url = app["token_endpoint"]
    req = request.Request(
        url,
        data=parse.urlencode(token_form(app, username)).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": CHROME_USER_AGENT,
        },
        method="POST",
    )
    secrets = [app["client_secret"], app["client_id"]]
    status, body, transport = _send(req, urlopen, secrets)
    reason = ""
    parsed: Any = None
    if transport:
        reason = transport
    elif status is None or status < 200 or status >= 300:
        reason = f"HTTP {status}"
    else:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            reason = "non-JSON token response"
    token = str((parsed or {}).get("access_token") or "").strip() if isinstance(parsed, dict) else ""
    if not reason and not token:
        reason = "no access_token"
    if reason:
        logger.error(
            "EZLynx task API login failed once (%s %s); stopped for 60 minutes; "
            "using Zapier. response=%s",
            reason,
            url,
            redact_body(body, secrets),
        )
        open_breaker(username, reason)
        return None
    try:
        expires_in = int(parsed.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    store_token(username, token, expires_in)
    return token


def _ensure_token(
    username: str, accessor: Any, urlopen: UrlOpen
) -> tuple[Optional[str], Optional[dict[str, str]], str]:
    if breaker_open(username):
        logger.warning("EZLynx task API login stopped for %s; using Zapier.", username)
        return None, None, "circuit_open"
    app, reason = load_app_config(accessor)
    if app is None:
        logger.warning("EZLynx task API unavailable: %s; using Zapier.", reason)
        return None, None, "unavailable"
    token = cached_token(username) or fetch_token_once(app, username, urlopen)
    if not token:
        return None, app, "circuit_open" if breaker_open(username) else "auth_failed"
    return token, app, ""


# ---------------------------------------------------------------------------
# Payload, discussion choice, readback
# ---------------------------------------------------------------------------


def due_at_10pm_utc_z(due_date_iso: Optional[str]) -> Optional[str]:
    """10:00 PM America/New_York on the due date, as UTC ``...000Z``."""
    if not due_date_iso:
        return None
    local = datetime.strptime(str(due_date_iso)[:10], "%Y-%m-%d").replace(
        hour=22, tzinfo=_EASTERN
    )
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_task_note(
    *, assigned_user_id: int, title: str, description: str, due_date: Optional[str]
) -> dict[str, Any]:
    """POST /v8/discussions/{id}/notes body (EZLynx Postman sample).

    assignedUserId is an integer inside ``task``. No priority or title field.
    One in-app reminder to the assignee at the due time.
    """
    text = "\n\n".join(p.strip() for p in (title, description) if str(p or "").strip())
    due = due_at_10pm_utc_z(due_date)
    task: dict[str, Any] = {"assignedUserId": int(assigned_user_id)}
    if due:
        task["due"] = due
        task["reminders"] = [
            {
                "scheduled": due,
                "remindees": {"myself": False, "assignee": True, "followers": False},
                "types": {"email": False, "text": False, "notification": True},
            }
        ]
    return {"type": "TaskCreationNote", "body": text, "task": task}


def _records(parsed: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    if isinstance(parsed, dict):
        for key in keys:
            value = parsed.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [parsed]
    return []


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value in (None, "") or isinstance(value, (dict, list, bool)):
            continue
        return str(value).strip()
    return ""


def discussion_id_of(record: dict[str, Any]) -> str:
    return _first(record, ("discussionId", "DiscussionId", "id", "Id"))


def discussion_title_of(record: dict[str, Any]) -> str:
    return _first(record, ("title", "Title", "subject", "Subject"))


def note_id_of(record: dict[str, Any]) -> str:
    return _first(record, ("noteId", "NoteId", "id", "Id"))


def select_discussion(
    discussions: list[dict[str, Any]], title_hint: str = ""
) -> tuple[Optional[str], str]:
    """One titled discussion: a unique match for the hint, else the exact
    ``Tasks by Robie`` title (newest id). Never the first one in the list."""
    titled = [
        row for row in discussions
        if discussion_id_of(row)
        and discussion_title_of(row).casefold() not in {"", "untitled"}
    ]
    titles = [discussion_title_of(row) for row in titled]
    hint = " ".join(str(title_hint or "").split()).casefold()
    if hint:
        exact = [r for r in titled if discussion_title_of(r).casefold() == hint]
        partial = [r for r in titled if hint in discussion_title_of(r).casefold()]
        for matches in (exact, partial):
            if len(matches) == 1:
                return discussion_id_of(matches[0]), ""
        return None, f"no single titled discussion matched {title_hint!r} (titled: {titles or 'none'})"
    wanted = ROBIE_TASK_DISCUSSION_TITLE.casefold()
    robie = [r for r in titled if discussion_title_of(r).casefold() == wanted]
    if robie:
        newest = max(robie, key=lambda r: int(discussion_id_of(r)) if discussion_id_of(r).isdigit() else 0)
        return discussion_id_of(newest), ""
    return None, f"no discussion titled {ROBIE_TASK_DISCUSSION_TITLE!r} (titled: {titles or 'none'})"


def _discussion_records(parsed: Any) -> list[dict[str, Any]]:
    return _records(parsed, ("discussions", "Discussions", "items", "Items", "data", "Data"))


def _get_json(url: str, token: str, urlopen: UrlOpen) -> tuple[Any, Optional[str]]:
    req = request.Request(url, headers=_headers(token), method="GET")
    status, body, transport = _send(req, urlopen, [token])
    if transport:
        return None, transport
    if status is None or status < 200 or status >= 300:
        logger.error("EZLynx task API HTTP %s %s response=%s", status, url, body)
        return None, f"HTTP {status}"
    try:
        return json.loads(body), None
    except json.JSONDecodeError:
        return None, "non-JSON"


def read_back_task_note(
    base: str, discussion_id: str, note_id: str, token: str, urlopen: UrlOpen
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Find the note by id: notes/query first, then the discussion with notes."""
    note_key: Any = int(note_id) if note_id.isdigit() else note_id
    req = request.Request(
        f"{base}/v8/notes/query",
        data=json.dumps({"noteIds": [note_key]}).encode("utf-8"),
        headers=_headers(token, json_body=True),
        method="POST",
    )
    status, body, transport = _send(req, urlopen, [token])
    if not transport and status is not None and 200 <= status < 300:
        try:
            for note in _records(json.loads(body), ("notes", "Notes", "items", "Items")):
                if note_id_of(note) == note_id:
                    return note, None
        except json.JSONDecodeError:
            pass
    quoted = parse.quote(str(discussion_id), safe="")
    document, err = _get_json(f"{base}/v8/discussions/{quoted}/with-notes", token, urlopen)
    if err:
        return None, err
    for note in _records(document, ("notes", "Notes", "items", "Items")):
        if note_id_of(note) == note_id:
            return note, None
    return None, f"note {note_id} not in discussion {discussion_id}"


def note_id_from_create_response(posted: Any) -> str:
    if isinstance(posted, bool):
        return ""
    if isinstance(posted, int):
        return str(posted)
    if isinstance(posted, str) and posted.strip().isdigit():
        return posted.strip()
    if isinstance(posted, dict):
        nested = posted.get("note")
        for source in (posted, nested if isinstance(nested, dict) else None):
            if isinstance(source, dict):
                found = note_id_of(source)
                if found:
                    return found
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def resolve_assignee_id(assignee: str = "", assigned_user_id: Any = None) -> Optional[int]:
    """Explicit positive id, else the confirmed id for the login. No guessing."""
    if assigned_user_id not in (None, "") and not isinstance(assigned_user_id, bool):
        try:
            value = int(assigned_user_id)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    from .ezlynx_user_ids import ezlynx_user_id_for

    return ezlynx_user_id_for(assignee)


def create_task(
    *,
    applicant_id: Any,
    title: str,
    description: str = "",
    assignee: str = "",
    assigned_user_id: Any = None,
    due_date: Optional[str] = None,
    discussion_id: str = "",
    discussion_title: str = "",
    dry_run: bool = False,
    accessor: Any = None,
    urlopen: UrlOpen = _default_urlopen,
) -> dict[str, Any]:
    """Create one EZLynx task through the Discussion API.

    Returns a dict with ``status``. Only ``created`` is a success, and it
    means the note was read back with the requested assignee. Use
    :func:`zapier_fallback_allowed` to decide whether Zapier may run.
    """
    if not direct_task_api_enabled():
        return {"status": "disabled", "reason": f"{DIRECT_TASK_API_ENV}=0"}
    username = act_as_username()
    if not username:
        _log_once("act-as-unset", "EZLynx task API off: %s is unset; using Zapier.", ACT_AS_ENV)
        return {"status": "unavailable", "reason": f"{ACT_AS_ENV} is unset"}
    if is_vendor_integration_username(username):
        _log_once(
            f"vendor-user:{username.casefold()}",
            "EZLynx task API off: acting username %r is the vendor integration user; using Zapier.",
            username,
        )
        return {"status": "refused_vendor_username", "reason": "vendor integration username is refused"}

    user_id = resolve_assignee_id(assignee, assigned_user_id)
    if user_id is None:
        logger.info("EZLynx task API skipped: no confirmed user id for %r; using Zapier.", assignee)
        return {"status": "refused_unresolved_assignee", "reason": f"no confirmed EZLynx user id for {assignee!r}"}

    applicant = str(applicant_id or "").split(".")[0].strip()
    if not applicant.isdigit():
        return {"status": "skipped_no_applicant", "reason": "a numeric applicant id is required"}
    if not str(title or "").strip():
        raise EZLynxTaskApiError("title is required")

    note = build_task_note(
        assigned_user_id=user_id, title=title, description=description, due_date=due_date
    )
    try:
        from .ezlynx_discussions import reject_phone_numbers
        from .ezlynx_write_scope import require_allowed_ezlynx_write_applicant

        reject_phone_numbers(note["body"])
        require_allowed_ezlynx_write_applicant(applicant)
    except Exception as exc:  # noqa: BLE001 - gate refusals, nothing written
        return {"status": "write_refused", "reason": f"{type(exc).__name__}: {exc}"}

    if dry_run:
        return {
            "status": DRY_RUN,
            "applicant_id": applicant,
            "assigned_user_id": user_id,
            "act_as_username": username,
            "payload": note,
        }

    token, app, token_status = _ensure_token(username, accessor, urlopen)
    if not token or app is None:
        return {"status": token_status or "auth_failed"}
    base = app["discussion_base"]

    chosen = str(discussion_id or "").strip()
    if not chosen:
        url = f"{base}/v8/discussions/by-applicant?" + parse.urlencode({"applicantId": applicant})
        listed, err = _get_json(url, token, urlopen)
        if err:
            return {"status": "error", "reason": f"discussion list failed ({err})"}
        chosen, why = select_discussion(_discussion_records(listed), discussion_title)
        if not chosen and not str(discussion_title or "").strip():
            return create_robie_discussion_with_task(
                base, applicant, note, user_id, token, urlopen
            )
        if not chosen:
            logger.warning("EZLynx task API skipped: %s for applicant %s; using Zapier.", why, applicant)
            return {"status": "no_discussion", "reason": why}

    post_url = f"{base}/v8/discussions/{parse.quote(chosen, safe='')}/notes"
    req = request.Request(
        post_url,
        data=json.dumps(note).encode("utf-8"),
        headers=_headers(token, json_body=True),
        method="POST",
    )
    status, body, transport = _send(req, urlopen, [token])
    base_result = {"applicant_id": applicant, "discussion_id": chosen, "assigned_user_id": user_id}
    if transport or status is None or status >= 500:
        # The request may have reached EZLynx. Do not fall back blindly.
        reason = transport or f"HTTP {status}"
        logger.error("EZLynx task POST outcome unknown (%s %s)", reason, post_url)
        return {**base_result, "status": UNVERIFIED, "reason": f"task POST outcome unknown ({reason})"}
    if status < 200 or status >= 300:
        logger.error("EZLynx task POST rejected (HTTP %s %s) response=%s", status, post_url, body)
        return {**base_result, "status": "rejected", "reason": f"HTTP {status}"}

    try:
        posted: Any = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        posted = body
    note_id = note_id_from_create_response(posted)
    if not note_id:
        return {**base_result, "status": UNVERIFIED, "reason": "task POST returned no note id"}
    found, err = read_back_task_note(base, chosen, note_id, token, urlopen)
    task = (found or {}).get("task") if isinstance((found or {}).get("task"), dict) else {}
    if err or found is None:
        return {**base_result, "status": UNVERIFIED, "note_id": note_id, "reason": f"readback failed ({err})"}
    if str(found.get("type") or "") != "TaskCreationNote" or str(task.get("assignedUserId")) != str(user_id):
        return {
            **base_result,
            "status": UNVERIFIED,
            "note_id": note_id,
            "reason": (
                f"readback type={found.get('type')!r} "
                f"assignedUserId={task.get('assignedUserId')!r}, expected {user_id}"
            ),
        }
    task_id = _first(task, ("taskId", "TaskId", "id", "Id")) or _first(found, ("taskId", "TaskId"))
    logger.info(
        "EZLynx direct task created assignee=%s assigned_user_id=%s note_id=%s task_id=%s "
        "discussion=%s applicant=%s",
        assignee or "(id given)", user_id, note_id, task_id or "(unknown)", chosen, applicant,
    )
    return {
        **base_result,
        "status": CREATED,
        "note_id": note_id,
        "task_id": task_id,
        "due": task.get("dueDate") or task.get("due"),
    }


# ---------------------------------------------------------------------------
# "Tasks by Robie" discussion creation (exact title only)
# ---------------------------------------------------------------------------


def discussion_id_from_create_response(posted: Any) -> str:
    """The with-note response is a DiscussionId (int), a digit string, or an object."""
    if isinstance(posted, bool):
        return ""
    if isinstance(posted, int) and posted > 0:
        return str(posted)
    if isinstance(posted, str) and posted.strip().isdigit():
        return posted.strip()
    if isinstance(posted, dict):
        found = discussion_id_of(posted)
        if found:
            return found
        nested = posted.get("discussion")
        if isinstance(nested, dict):
            return discussion_id_of(nested)
    return ""


def _same_text(left: Any, right: Any) -> bool:
    return " ".join(str(left or "").split()) == " ".join(str(right or "").split())


def _find_our_task_note(
    base: str, discussion_id: str, note: dict[str, Any], user_id: int,
    token: str, urlopen: UrlOpen,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """The TaskCreationNote with our body and assignee, read back by GET."""
    quoted = parse.quote(str(discussion_id), safe="")
    document, err = _get_json(f"{base}/v8/discussions/{quoted}/with-notes", token, urlopen)
    if err:
        return None, err
    for row in _records(document, ("notes", "Notes", "items", "Items")):
        task = row.get("task") if isinstance(row.get("task"), dict) else {}
        if (
            str(row.get("type") or "") == "TaskCreationNote"
            and str(task.get("assignedUserId")) == str(user_id)
            and _same_text(row.get("body"), note.get("body"))
        ):
            return row, None
    return None, f"task note not found in discussion {discussion_id}"


def _created_result(
    applicant: str, discussion_id: str, user_id: int, found: dict[str, Any]
) -> dict[str, Any]:
    task = found.get("task") if isinstance(found.get("task"), dict) else {}
    note_id = note_id_of(found)
    task_id = _first(task, ("taskId", "TaskId", "id", "Id")) or _first(found, ("taskId", "TaskId"))
    logger.info(
        "EZLynx direct task created in new %r discussion assigned_user_id=%s note_id=%s "
        "task_id=%s discussion=%s applicant=%s",
        ROBIE_TASK_DISCUSSION_TITLE, user_id, note_id, task_id or "(unknown)",
        discussion_id, applicant,
    )
    return {
        "status": CREATED,
        "applicant_id": applicant,
        "discussion_id": discussion_id,
        "discussion_created": True,
        "assigned_user_id": user_id,
        "note_id": note_id,
        "task_id": task_id,
        "due": task.get("dueDate") or task.get("due"),
    }


def create_robie_discussion_with_task(
    base: str, applicant: str, note: dict[str, Any], user_id: int,
    token: str, urlopen: UrlOpen = _default_urlopen,
) -> dict[str, Any]:
    """Create ``Tasks by Robie`` with the task as its first note, then read back.

    Callers have already run the write allowlist and phone-number guard for
    this applicant. Only this exact title is ever created here.
    """
    url = f"{base}/v8/discussions/with-note"
    body = {
        "applicantId": int(applicant),
        "discussion": {"title": ROBIE_TASK_DISCUSSION_TITLE},
        "note": note,
    }
    req = request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers=_headers(token, json_body=True), method="POST",
    )
    status, text, transport = _send(req, urlopen, [token])
    base_result = {
        "applicant_id": applicant, "assigned_user_id": user_id,
        "discussion_title": ROBIE_TASK_DISCUSSION_TITLE,
    }
    discussion_id = ""
    if not transport and status is not None and 200 <= status < 300:
        try:
            discussion_id = discussion_id_from_create_response(json.loads(text) if text.strip() else {})
        except json.JSONDecodeError:
            discussion_id = discussion_id_from_create_response(text)
    else:
        logger.error(
            "EZLynx %r discussion create failed (%s %s) response=%s",
            ROBIE_TASK_DISCUSSION_TITLE, transport or f"HTTP {status}", url, text,
        )

    if not discussion_id:
        # Settle a failed, timed-out, or id-less create by listing again.
        listed, err = _get_json(
            f"{base}/v8/discussions/by-applicant?" + parse.urlencode({"applicantId": applicant}),
            token, urlopen,
        )
        if err:
            return {**base_result, "status": UNVERIFIED,
                    "reason": f"discussion create unclear and re-list failed ({err})"}
        discussion_id, _ = select_discussion(_discussion_records(listed))
        if not discussion_id:
            return {**base_result, "status": "rejected",
                    "reason": f"discussion create failed ({transport or f'HTTP {status}'}); nothing written"}

    record, err = _get_json(
        f"{base}/v8/discussions/{parse.quote(discussion_id, safe='')}", token, urlopen
    )
    if err or not isinstance(record, dict) or discussion_title_of(record) != ROBIE_TASK_DISCUSSION_TITLE:
        seen = discussion_title_of(record) if isinstance(record, dict) else None
        return {**base_result, "status": UNVERIFIED, "discussion_id": discussion_id,
                "reason": f"new discussion did not read back with the title (err={err}, title={seen!r})"}
    found, err = _find_our_task_note(base, discussion_id, note, user_id, token, urlopen)
    if found is None:
        return {**base_result, "status": UNVERIFIED, "discussion_id": discussion_id,
                "reason": f"readback failed ({err})"}
    return _created_result(applicant, discussion_id, user_id, found)
