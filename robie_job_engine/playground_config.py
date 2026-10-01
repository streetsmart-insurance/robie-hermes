"""Configuration for the Robie Playground.

The Playground stays off unless ``ROBIE_PLAYGROUND`` is on. The Chat space
comes from ``ROBIE_PLAYGROUND_SPACE_ID``. Neither value is hard-coded.

Real clients stay closed until ``ROBIE_EZLYNX_WRITE_SCOPE=all``. That
switch is honored only while the Playground hard blocks, the go step,
and the undo log are active. Practice-mode tags and carrier redirects
follow the client: Buster Brown and the other test ids still redirect.
A real client is emailed at the carrier only after go, and only in
all-clients mode.
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

# Procedure excludes live in playground_sop_excludes.txt. An env file
# cannot turn a rule off. Edit that file to change the list.

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
    """Older switch. All-clients is ``ROBIE_EZLYNX_WRITE_SCOPE=all`` now."""
    return _on(REAL_CLIENTS_ENV)


def hard_blocks_enforced() -> bool:
    """The Playground block list refuses the actions Carlo named."""
    from .playground_guardrails import classify_playground_request

    required = (
        ("delete the policy", "delete_or_cancel"),
        ("cancel the client", "delete_or_cancel"),
        ("bind the policy", "bind_issue_reinstate_nonrenew"),
        ("issue the policy", "bind_issue_reinstate_nonrenew"),
        ("reinstate the policy", "bind_issue_reinstate_nonrenew"),
        ("non-renew this policy", "bind_issue_reinstate_nonrenew"),
        ("change the billing to monthly", "payment_billing_mortgagee"),
        ("change the mortgagee to Wells Fargo", "payment_billing_mortgagee"),
        ("change the premium to 100", "premium_effective_limit_coverage"),
        ("change the effective date to 10/1", "premium_effective_limit_coverage"),
        ("change the limit to 1000000", "premium_effective_limit_coverage"),
        ("update the coverage to full", "premium_effective_limit_coverage"),
        ("email the client about the change", "client_email_or_text"),
        ("text the insured", "client_email_or_text"),
    )
    for text, code in required:
        decision = classify_playground_request(text)
        if not decision.blocked or decision.code != code:
            return False
    return True


def confirmation_gate_enforced() -> bool:
    """A write is not a go-word, and go is a whole message."""
    from .playground_guardrails import is_go
    from .playground_service import CONFIRM_KIND, PLAYGROUND_ACTION

    return bool(
        CONFIRM_KIND
        and PLAYGROUND_ACTION
        and is_go("go")
        and is_go("yes")
        and not is_go("go ahead and delete the policy")
    )


def undo_log_enforced() -> bool:
    from .playground_undo import APPEND_ONLY_TRIGGERS

    required = {"playground_undo_log_no_delete", "playground_undo_log_no_update"}
    return required <= set(APPEND_ONLY_TRIGGERS)


def playground_guardrails_active() -> bool:
    """Hard blocks, read-back-then-go, and the undo log, and the flag is on."""
    if not playground_enabled():
        return False
    return hard_blocks_enforced() and confirmation_gate_enforced() and undo_log_enforced()


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


def is_practice_subject(applicant_id: str = "", client_name: str = "") -> bool:
    """Buster Brown and the other test applicant ids. Not the allowlist."""
    applicant = str(applicant_id or "").strip()
    if applicant:
        return applicant in practice_applicant_ids()
    name = " ".join(str(client_name or "").casefold().split())
    return name == "buster brown"


def should_tag_practice(applicant_id: str = "", client_name: str = "") -> bool:
    """Practice mode is about the client, not how wide the allowlist is.

    While all-clients is closed, every Playground reply is practice.
    While it is open, only a test client is tagged.
    """
    from .ezlynx_write_scope import all_clients_scope_honored

    if is_practice_subject(applicant_id, client_name):
        return True
    if str(applicant_id or "").strip() or str(client_name or "").strip():
        return False
    return not all_clients_scope_honored()


def buster_brown_only_mode(applicant_id: str = "", client_name: str = "") -> bool:
    """Whether this reply should say Practice mode."""
    return should_tag_practice(applicant_id, client_name)


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
    """Real carrier mail only for a real client in honored all-clients mode.

    Test clients always go to the sink. A real client also goes to the sink
    until write scope is all and the Playground guardrails are active.
    The send itself still waits for go.
    """
    applicant = str(applicant_id or "").strip()
    if not applicant or applicant in practice_applicant_ids():
        return True
    from .ezlynx_write_scope import all_clients_scope_honored

    if not all_clients_scope_honored():
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
