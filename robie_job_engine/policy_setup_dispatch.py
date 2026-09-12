"""Deterministic routing for the homeowners policy-setup job class.

Job class: a request to create/set up a homeowners policy on EZLynx applicant
220250093 (policy numbers TEST-HO-*).

Rule: the email/Chat runner must invoke ``ezlynx_policy_setup`` as a real tool
call before any ``playwright_exec``. If the tool is missing/unregistered, fail
closed with that error. Never fall through to ``playwright_exec`` for this
job class.

Live failures (jobs c1ffb79a, 1cfd0f3e — 2026-09-12): the email worker never
called the tool, wandered with playwright_exec, timed out, and the policy was
never created. Prompt guidance was not enough; this module makes the routing
deterministic.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

POLICY_SETUP_REQUIRED_KIND = "policy_setup_required"
POLICY_SETUP_TOOL = "ezlynx_policy_setup"
POLICY_APPLICANT_ID = "220250093"

FAIL_CLOSED_MESSAGE = (
    "ezlynx_policy_setup is not registered; failing closed — "
    "will not fall through to playwright_exec for policy setup."
)

_INTENT_RE = re.compile(
    r"\b(creat\w*|set\s*up|setup|complet\w*|mint\w*|generat\w*)\b"
    r".{0,80}?\b(homeowners?|home\s*owners?|\bHO\b|home\s*policy)\b",
    re.IGNORECASE | re.DOTALL,
)
_POLICY_NUMBER_RE = re.compile(r"\b(TEST-HO-[A-Z0-9][A-Z0-9-]*)\b", re.IGNORECASE)


class PolicySetupToolMissing(RuntimeError):
    """The ezlynx_policy_setup tool could not be loaded. Fail closed."""


def detect_policy_setup_request(text: str) -> dict | None:
    """Return tool args if text asks to create/set up a homeowners policy.

    Matches the job class: create/setup intent + homeowners + applicant
    220250093 or a TEST-HO-* policy number. Returns ``{"policy_number": ...}``
    or ``None``.
    """
    raw = str(text or "")
    if not _INTENT_RE.search(raw):
        return None
    if POLICY_APPLICANT_ID not in raw and "TEST-HO-" not in raw.upper():
        return None
    match = _POLICY_NUMBER_RE.search(raw)
    if not match:
        return None
    return {"policy_number": match.group(1).upper()}


def _install_hermes_registry_stub() -> dict:
    """The tool module imports ``tools.registry``; stub it outside the agent."""
    tools_pkg = ModuleType("tools")
    tools_pkg.__path__ = []
    registry_mod = ModuleType("tools.registry")

    class DummyRegistry:
        def register(self, **_kwargs):
            return None

    def tool_error(message):
        return {"ok": False, "error": message}

    def tool_result(payload):
        return payload

    registry_mod.registry = DummyRegistry()
    registry_mod.tool_error = tool_error
    registry_mod.tool_result = tool_result
    previous = {name: sys.modules.get(name) for name in ("tools", "tools.registry")}
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = registry_mod
    return previous


def _restore_modules(previous: dict) -> None:
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def tool_module_path() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "deploy" / "hermes" / "tools" / "policy_setup_tool.py",
        Path("/opt/streetsmart-hermes/.hermes/hermes-agent/tools/policy_setup_tool.py"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def load_policy_setup_handler():
    """Load the real ezlynx_policy_setup tool handler.

    Raises PolicySetupToolMissing with the fail-closed message when the tool
    module cannot be imported or has no handler — the caller must not fall
    through to playwright_exec.
    """
    path = tool_module_path()
    if not path.is_file():
        raise PolicySetupToolMissing(FAIL_CLOSED_MESSAGE)
    previous = _install_hermes_registry_stub()
    try:
        spec = importlib.util.spec_from_file_location("policy_setup_tool", path)
        if spec is None or spec.loader is None:
            raise PolicySetupToolMissing(FAIL_CLOSED_MESSAGE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except PolicySetupToolMissing:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on any load error
        raise PolicySetupToolMissing(
            f"{FAIL_CLOSED_MESSAGE} (load error: {type(exc).__name__}: {exc})"
        ) from exc
    finally:
        _restore_modules(previous)
    handler = getattr(module, "ezlynx_policy_setup_handler", None)
    if not callable(handler):
        raise PolicySetupToolMissing(FAIL_CLOSED_MESSAGE)
    return handler


def invoke_policy_setup_tool(args: dict) -> dict:
    """Invoke the real tool handler. Fail closed when it is missing."""
    handler = load_policy_setup_handler()
    return handler(dict(args or {}))
