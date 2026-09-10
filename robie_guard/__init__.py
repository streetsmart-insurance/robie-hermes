"""robie_guard -- cardinal safety rules for StreetSmart ROBIE automations.

Usage at any Python call site that is about to change EZLynx:

    from robie_guard import ActionIntent, assert_allowed

    assert_allowed(ActionIntent(
        kind="api", intent="post_note", object_class="note",
        applicant_id=applicant_id, policy_number=policy_number,
    ))

Usage for the browser layer:

    from robie_guard import SafePage
    page = await SafePage.attach(page)      # installs shim + route interception
    await page.safe_click("button:has-text('Save')")
"""

from .cardinal import ALL_RULES, CardinalViolation, TERMINAL_OUTCOMES
from .destructive_guard import (
    ActionIntent,
    Decision,
    assert_allowed,
    classify_intent,
    guarded_transaction_delete,
    looks_destructive,
)
from .receipt import JobReceipt, GATES
from .write_gate import (
    WriteNotAuthorized, add_write_gate_args, assert_write_allowed,
    current_authorization, disable_writes, enable_writes,
    enable_writes_from_env, resolve_write_gate, writes_enabled,
)

__all__ = [
    "ALL_RULES", "CardinalViolation", "TERMINAL_OUTCOMES",
    "ActionIntent", "Decision", "assert_allowed", "classify_intent",
    "guarded_transaction_delete", "looks_destructive",
    "JobReceipt", "GATES",
    "WriteNotAuthorized", "add_write_gate_args", "assert_write_allowed",
    "current_authorization", "disable_writes", "enable_writes",
    "enable_writes_from_env", "resolve_write_gate", "writes_enabled",
]

try:  # SafePage needs playwright; the rest of the package must import without it.
    from .safe_page import SafePage  # noqa: F401
    __all__.append("SafePage")
except ImportError:  # pragma: no cover
    pass
