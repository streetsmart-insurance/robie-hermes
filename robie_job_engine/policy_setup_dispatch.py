"""Hard route for the homeowners policy-setup job class (code, not prompt).

Job class: a ``hermes.email_task`` asking to create/set up a homeowners policy
on EZLynx applicant 220250093 (policy numbers TEST-HO-*).

The route works in two code steps, no prompt text involved:

1. The email runner (``scripts/robie_email_agent.py``) classifies the job in
   code and checkpoints ``policy_setup_required``. The runner never sets up
   the policy itself.
2. ``deploy/hermes/tools/playwright_tool.py`` hard-routes: when the bound job
   carries an unfulfilled ``policy_setup_required`` marker, it calls the real
   ``ezlynx_policy_setup`` handler in code and returns its result — before any
   ``playwright_exec`` browser code runs.

If the tool is missing/unregistered, the route fails closed with that error.
Job 1cfd0f3e never invoked the tool; this route makes invocation unavoidable.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

POLICY_SETUP_REQUIRED_KIND = "policy_setup_required"
POLICY_SETUP_TOOL = "ezlynx_policy_setup"
POLICY_SETUP_ACTION = "hermes.email_task"
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
    """Return ``{"policy_number": ...}`` for the homeowners-create job class."""
    raw = str(text or "")
    if not _INTENT_RE.search(raw):
        return None
    if POLICY_APPLICANT_ID not in raw and "TEST-HO-" not in raw.upper():
        return None
    match = _POLICY_NUMBER_RE.search(raw)
    if not match:
        return None
    return {"policy_number": match.group(1).upper()}


def policy_setup_tool_path(anchor_file: str | Path | None = None) -> Path:
    """Locate the policy_setup_tool module.

    The primary anchor is the calling tool file (playwright_tool.py lives next
    to policy_setup_tool.py in the worker); repo and deploy paths are
    fallbacks.
    """
    here = Path(__file__).resolve()
    candidates = []
    if anchor_file:
        candidates.append(Path(anchor_file).with_name("policy_setup_tool.py"))
    candidates += [
        here.parents[1] / "deploy" / "hermes" / "tools" / "policy_setup_tool.py",
        Path("/opt/streetsmart-hermes/.hermes/hermes-agent/tools/policy_setup_tool.py"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def load_policy_setup_handler(anchor_file: str | Path | None = None):
    """Load the real ezlynx_policy_setup handler.

    Runs inside the worker, where the real ``tools.registry`` exists — no
    stubs, no bypass. Raises PolicySetupToolMissing when the tool module
    cannot be loaded or has no handler: the caller must fail closed.
    """
    path = policy_setup_tool_path(anchor_file)
    if not path.is_file():
        raise PolicySetupToolMissing(FAIL_CLOSED_MESSAGE)
    try:
        spec = importlib.util.spec_from_file_location(
            "policy_setup_tool_route", path
        )
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
    handler = getattr(module, "ezlynx_policy_setup_handler", None)
    if not callable(handler):
        raise PolicySetupToolMissing(FAIL_CLOSED_MESSAGE)
    return handler
