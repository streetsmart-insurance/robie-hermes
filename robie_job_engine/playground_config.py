"""Configuration for the Robie Playground.

The Playground stays off unless ``ROBIE_PLAYGROUND`` is on. The Chat space
comes from ``ROBIE_PLAYGROUND_SPACE_ID``. Neither value is hard-coded.

Real clients stay closed until ``ROBIE_PLAYGROUND_REAL_CLIENTS`` is turned
on. While that switch is off, or the write allowlist is only the practice
applicant (Buster Brown, 26356199), every Playground reply is tagged
Practice mode and carrier mail is redirected.
"""

from __future__ import annotations

import os

from .ezlynx_write_scope import applicant_is_write_allowed
from .runtime_env import playground_enabled

PLAYGROUND_SPACE_ENV = "ROBIE_PLAYGROUND_SPACE_ID"
REAL_CLIENTS_ENV = "ROBIE_PLAYGROUND_REAL_CLIENTS"
PRACTICE_APPLICANTS_ENV = "ROBIE_PLAYGROUND_PRACTICE_APPLICANT_IDS"
CARRIER_SINK_ENV = "ROBIE_PLAYGROUND_CARRIER_EMAIL_SINK"
CONFIRM_TIMEOUT_ENV = "ROBIE_PLAYGROUND_CONFIRM_TIMEOUT_MINUTES"
LIVE_WRITES_ENV = "ROBIE_PLAYGROUND_LIVE_WRITES"
SOP_FOLDERS_ENV = "ROBIE_PLAYGROUND_SOP_FOLDERS"
SOP_INDEX_ENV = "ROBIE_PLAYGROUND_SOP_INDEX"
DIGEST_CHAT_USER_ENV = "ROBIE_PLAYGROUND_DIGEST_CHAT_USER"

CARRIER_SINK_DEFAULT = "carlo@streetsmart.insurance"
DIGEST_RECIPIENT = "carlo@streetsmart.insurance"
CONFIRM_TIMEOUT_MINUTES_DEFAULT = 30
BUSTER_BROWN_APPLICANT_ID = "26356199"
# Historical fail-closed test account. It is practice, not a real client.
LEGACY_TEST_APPLICANT_ID = "220250093"
PRACTICE_APPLICANT_DEFAULT = f"{BUSTER_BROWN_APPLICANT_ID},{LEGACY_TEST_APPLICANT_ID}"

# Drive folder ids Carlo named. Overridable. These are folder ids, not secrets.
DEFAULT_SOP_FOLDERS = (
    ("core", "1nwtIlz07UNNloBR8zqDOSOKEU64m6YdE"),
    ("commercial", "1rptPZ4bR6CkPysojnicrBRyDbypZpQGO"),
    ("personal", "1mYLvru6U-sM5RfeBgwmIFqlqv0uqtUwg"),
    ("trucking", "1orog25WUu6KzW4DFJ88vK_kId2ZDmYvC"),
    ("employee_hub", "1Bg3NcpIZD0bp7S8TZgdzPw7a1PSLThyb"),
)

# Hard excludes. Not configurable, so an env file cannot open them.
EXCLUDED_SHARED_DRIVES = frozenset(
    {
        "0ANbwd0py5G63Uk9PVA",  # HR
        "0AKIUMpLpYux4Uk9PVA",  # finance
    }
)

_ON = frozenset({"1", "true", "yes", "on"})


def _on(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in _ON


def normalize_space_id(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("spaces/"):
        return raw
    return f"spaces/{raw}"


def playground_space_id() -> str:
    return normalize_space_id(os.environ.get(PLAYGROUND_SPACE_ENV))


def message_in_playground(conversation_id: str | None) -> bool:
    """True only when the flag is on and this Chat space is the Playground."""
    if not playground_enabled():
        return False
    space = playground_space_id()
    if not space:
        return False
    return normalize_space_id(conversation_id) == space


def email_guardrails_enabled() -> bool:
    """robie@ mail uses the same guardrails when the Playground flag is on."""
    return playground_enabled()


def real_clients_open() -> bool:
    """Explicit switch. Default closed. The allowlist alone does not open them."""
    return _on(REAL_CLIENTS_ENV)


def practice_applicant_ids() -> frozenset[str]:
    raw = os.environ.get(PRACTICE_APPLICANTS_ENV, PRACTICE_APPLICANT_DEFAULT)
    ids = {part.strip() for part in str(raw or "").split(",") if part.strip()}
    return frozenset(ids or {BUSTER_BROWN_APPLICANT_ID, LEGACY_TEST_APPLICANT_ID})


def write_allowed(applicant_id: str) -> bool:
    return applicant_is_write_allowed(applicant_id)


def current_write_allowlist() -> frozenset[str] | None:
    """None means the compiled allowlist was explicitly cleared in-process."""
    from . import ezlynx_write_scope

    return ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS


def buster_brown_only_mode() -> bool:
    """Practice mode. Default closed until real clients are explicitly opened."""
    if not real_clients_open():
        return True
    allowed = current_write_allowlist()
    if not allowed:
        return True
    return allowed <= practice_applicant_ids()


def carrier_sink_address() -> str:
    raw = str(os.environ.get(CARRIER_SINK_ENV) or "").strip()
    return raw or CARRIER_SINK_DEFAULT


def confirm_timeout_minutes() -> int:
    raw = str(os.environ.get(CONFIRM_TIMEOUT_ENV) or "").strip()
    if not raw:
        return CONFIRM_TIMEOUT_MINUTES_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return CONFIRM_TIMEOUT_MINUTES_DEFAULT
    if value < 1:
        return CONFIRM_TIMEOUT_MINUTES_DEFAULT
    return value


def live_writes_enabled() -> bool:
    """Second switch for a real EZLynx or carrier send. Default off."""
    return _on(LIVE_WRITES_ENV)


def carrier_must_redirect(applicant_id: str) -> bool:
    """Practice clients, and anyone who is not a real allowlisted client, redirect."""
    applicant = str(applicant_id or "").strip()
    if not applicant or applicant in practice_applicant_ids():
        return True
    if not real_clients_open():
        return True
    if not write_allowed(applicant):
        return True
    return False


def sop_folders() -> tuple[tuple[str, str], ...]:
    """Configured Drive folders. Defaults are the StreetSmart SOP set."""
    raw = str(os.environ.get(SOP_FOLDERS_ENV) or "").strip()
    if not raw:
        return DEFAULT_SOP_FOLDERS
    parsed: list[tuple[str, str]] = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece or ":" not in piece:
            continue
        label, folder_id = piece.split(":", 1)
        label = label.strip()
        folder_id = folder_id.strip()
        if label and folder_id:
            parsed.append((label, folder_id))
    return tuple(parsed) if parsed else DEFAULT_SOP_FOLDERS


def sop_index_path() -> str:
    return str(os.environ.get(SOP_INDEX_ENV) or "").strip()


def digest_chat_user() -> str:
    raw = str(os.environ.get(DIGEST_CHAT_USER_ENV) or "").strip()
    return raw or DIGEST_RECIPIENT
