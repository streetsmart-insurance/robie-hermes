"""ROBIE_ENV helpers shared by Test/Production guards.

Memory destinations and Test-only wiring read this module so they do not
import the Chat runtime (and create a cycle).
"""

from __future__ import annotations

import os


TEST_ENV_NAME = "TEST"
PRODUCTION_ENV_NAMES = frozenset({"PRODUCTION", "PROD", "LIVE"})
SANDBOX_ENV_FLAG = "ROBIE_CHAT_SANDBOX"
PLAYGROUND_FLAG = "ROBIE_PLAYGROUND"
_PLAYGROUND_ON = frozenset({"1", "true", "yes", "on"})


class ProductionGuardError(RuntimeError):
    """Raised when Test-only or memory wiring is asked to run in Production."""


def current_robie_env() -> str:
    return str(os.environ.get("ROBIE_ENV") or "").strip().upper()


def chat_routing_env() -> str | None:
    """Pub/Sub routing value stamped on card buttons, or None when unset.

    ``TEST`` → ``test``. ``PRODUCTION`` / ``PROD`` / ``LIVE`` → ``prod``.
    Any other value, including unset, is omitted so the click stays untagged.
    """
    current = current_robie_env()
    if current == TEST_ENV_NAME:
        return "test"
    if current in PRODUCTION_ENV_NAMES:
        return "prod"
    return None


def playground_enabled() -> bool:
    """Test-only loosening. Off by default. Ignored on Production.

    Both must hold: ``ROBIE_PLAYGROUND`` is on, and ``ROBIE_ENV`` is not
    PRODUCTION, PROD, or LIVE. An unset ``ROBIE_ENV`` is not Production.
    """
    flag = str(os.environ.get(PLAYGROUND_FLAG) or "").strip().lower()
    if flag not in _PLAYGROUND_ON:
        return False
    return current_robie_env() not in PRODUCTION_ENV_NAMES


def chat_path_is_sandbox(*, conversation_id: str | None = None) -> bool:
    """Hermes/cua-driver is allowed only on an explicit sandbox path."""
    flag = str(os.environ.get(SANDBOX_ENV_FLAG) or "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    return str(conversation_id or "").strip().startswith("sandbox:")


def forbid_memory_destination(name: str) -> None:
    if current_robie_env() in PRODUCTION_ENV_NAMES:
        raise ProductionGuardError(
            f"{name} is forbidden when ROBIE_ENV is Production"
        )
