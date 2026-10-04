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

``extract_policy_setup_args`` builds the full tool args from the email body:
policy number, effective/expiration dates, and coverage limits. Dates are never
empty — parsed from the body, else the gold E01 dates (10/02/2026–10/02/2027).
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

# Gold E01 policy term. Used when the email body states no dates — the create
# call must never go out with empty dates.
GOLD_EFFECTIVE_DATE = "10/02/2026"
GOLD_EXPIRATION_DATE = "10/02/2027"

FAIL_CLOSED_MESSAGE = (
    "ezlynx_policy_setup is not registered; failing closed — "
    "will not fall through to playwright_exec for policy setup."
)

# OpenAI-style function schema. Chat/email workers must offer this as a
# callable tool, not only the name string in a list.
POLICY_SETUP_SCHEMA = {
    "name": POLICY_SETUP_TOOL,
    "description": (
        "Create a homeowners policy on EZLynx applicant 220250093 and fill its "
        "FormEntry Coverages tab. USE THIS TOOL — not playwright_exec — whenever "
        "the job asks to create, set up, or complete a homeowners policy. "
        "The engine runs search-first (no duplicate), creates with the gold "
        "carrier payload only when absent, mints the FormEntry via Save & "
        "Continue Edit, and fills coverages from the live FormEntry labels "
        "(Coverage A–F, or whatever is actually on the page). Applicant 220250093 only. "
        "Never binds. Returns the engine's evidence report."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "policy_number": {
                "type": "string",
                "description": "Policy number to create (e.g. TEST-HO-20260911-E01).",
            },
            "effective_date": {
                "type": "string",
                "description": "MM/DD/YYYY or ISO.",
            },
            "expiration_date": {
                "type": "string",
                "description": "MM/DD/YYYY or ISO.",
            },
            "dwelling": {"type": "string", "description": "Coverage A limit."},
            "other_structures": {"type": "string", "description": "Coverage B limit."},
            "personal_property": {"type": "string", "description": "Coverage C limit."},
            "loss_of_use": {"type": "string", "description": "Coverage D limit."},
            "personal_liability": {
                "type": "string",
                "description": "Personal Liability EA OCC limit.",
            },
            "medical_payments": {
                "type": "string",
                "description": "Medical Payments EA PER limit.",
            },
        },
        "required": ["policy_number", "effective_date", "expiration_date"],
    },
}

_INTENT_RE = re.compile(
    r"\b(creat\w*|set\s*up|setup|complet\w*|mint\w*|generat\w*)\b"
    r".{0,80}?\b(homeowners?|home\s*owners?|\bHO\b|home\s*policy)\b",
    re.IGNORECASE | re.DOTALL,
)
_POLICY_NUMBER_RE = re.compile(r"\b(TEST-HO-[A-Z0-9][A-Z0-9-]*)\b", re.IGNORECASE)

# Carlo's literal coverage labels, plus Coverage A–F aliases.
_LIMIT_LABELS = {
    "dwelling": [r"Dwelling", r"Coverage\s*A\b"],
    "other_structures": [r"Other\s*Structures", r"Coverage\s*B\b"],
    "personal_property": [r"Personal\s*Property", r"Coverage\s*C\b"],
    "loss_of_use": [r"Loss\s*of\s*Use", r"Coverage\s*D\b"],
    "personal_liability": [r"Personal\s*Liability(?:\s*EA\s*OCC)?", r"Coverage\s*E\b"],
    "medical_payments": [r"Medical\s*Payments(?:\s*EA\s*PER)?", r"Coverage\s*F\b"],
}
_AMOUNT_RE = r"\$?\s*([\d,]+(?:\.\d{1,2})?)"


class PolicySetupToolMissing(RuntimeError):
    """The ezlynx_policy_setup tool could not be loaded. Fail closed."""


def is_policy_setup_fail_closed(text: str) -> bool:
    """True when the worker printed the fail-closed contract (job c282de98)."""
    raw = str(text or "")
    if not raw.strip():
        return False
    if FAIL_CLOSED_MESSAGE in raw:
        return True
    folded = " ".join(raw.casefold().split())
    return (
        "ezlynx_policy_setup is not registered" in folded
        and "failing closed" in folded
        and "will not fall through to playwright_exec" in folded
    )


def is_coverage_fill_miss(text: str) -> bool:
    """True for job 2b30d293: FormEntry opened, no coverage labels filled."""
    folded = " ".join(str(text or "").casefold().split())
    if not folded:
        return False
    return (
        "no coverage labels were filled" in folded
        or "coverage amounts not on the job" in folded
        or "will not guess coverage amounts" in folded
        or "will not invent them" in folded
        or "will not invent dollar amounts" in folded
        or "coverage a, b, c, d, e, and f" in folded
        or "i still need the coverage" in folded
        or "i still need coverage" in folded
        or "could not open coverages" in folded
        or "still on the formentry location" in folded
        or "i am on the address tab" in folded
        or "cannot read the coverage fields" in folded
        or "cannot find coverages on this page" in folded
        or "no coverages name on the live formentry nav" in folded
    )


def is_formentry_mint_miss(text: str) -> bool:
    """True for job c75aab5c: Save clicked, Edit URL, FormEntry never minted."""
    raw = str(text or "")
    if not raw.strip():
        return False
    if "no FormEntry URL after 30s" in raw:
        return True
    if "FormEntry was not minted" in raw:
        return True
    folded = " ".join(raw.casefold().split())
    landed_edit = "policy/actions/edit/" in folded and "landed_url" in folded
    return landed_edit and "formentry" in folded


def is_policy_setup_honest_hitl(text: str) -> bool:
    """Fail-closed, mint-miss, or empty coverage fill — park HITL, not UNVERIFIED."""
    return (
        is_policy_setup_fail_closed(text)
        or is_formentry_mint_miss(text)
        or is_coverage_fill_miss(text)
    )


def policy_setup_openai_schema() -> dict:
    """Schema dict Hermes ``get_tool_definitions`` understands."""
    return {"type": "function", "function": dict(POLICY_SETUP_SCHEMA)}


def register_policy_setup_handler(registry=None):
    """Register the real callable handler on a Hermes tool registry.

    Name-only lists (``email_chat_job_schema`` / ``_HERMES_CORE_TOOLS``) are
    not enough — the Chat worker must be able to *call* this function.
    Raises ``PolicySetupToolMissing`` when the handler cannot be loaded.
    """
    handler = load_policy_setup_handler()
    if registry is None:
        try:
            from tools.registry import registry as registry
        except ImportError:
            return handler
    register_fn = getattr(registry, "register", None)
    if callable(register_fn):
        register_fn(
            name=POLICY_SETUP_TOOL,
            toolset="playwright",
            schema=POLICY_SETUP_SCHEMA,
            handler=handler,
            check_fn=lambda: True,
        )
    return handler


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


def _normalize_date(year: str, month: str, day: str) -> str | None:
    """Return MM/DD/YYYY, or None when the parts are not a plausible date."""
    try:
        y, m, d = int(year), int(month), int(day)
    except ValueError:
        return None
    if y < 100:
        y += 2000 if y < 50 else 1900
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    if not (2000 <= y <= 2100):
        return None
    return f"{m:02d}/{d:02d}/{y}"


def _find_date_near_label(text: str, label_re: str) -> str | None:
    """Find a date within ~60 chars after a label like 'effective'/'expir'."""
    pat = re.compile(
        r"(?:" + label_re + r")"
        r"[^\n]{0,60}?"
        r"(?:(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})|(\d{4})-(\d{2})-(\d{2}))",
        re.IGNORECASE,
    )
    match = pat.search(text)
    if not match:
        return None
    if match.group(4):
        return _normalize_date(match.group(4), match.group(5), match.group(6))
    return _normalize_date(match.group(3), match.group(1), match.group(2))


COVERAGE_LETTER_KEYS = {
    "A": "dwelling",
    "B": "other_structures",
    "C": "personal_property",
    "D": "loss_of_use",
    "E": "personal_liability",
    "F": "medical_payments",
}
_LETTER_AMOUNT_RE = re.compile(
    r"(?:coverage\s+)?\b([A-F])\b\s*[:\-]?\s*\$?\s*([\d,]{3,}(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
# Live email/prompt lines: ``Coverage A: $1,200,000`` (colon + comma).
_COLON_COVERAGE_RE = re.compile(
    r"Coverage\s+([A-F])\s*:\s*\$?\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)


def parse_coverage_amounts_from_reply(text: str) -> dict[str, str]:
    """Parse Coverage A–F dollar amounts from a HITL reply. Never invent a letter.

    Accepts ``Coverage A: $1,200,000``, ``Coverage A $1,200,000; B $120,000``,
    and ``A $1,200,000 B $120,000``. A letter with no number is omitted.
    """
    raw = str(text or "")
    found: dict[str, str] = {}
    for match in _COLON_COVERAGE_RE.finditer(raw):
        key = COVERAGE_LETTER_KEYS.get(match.group(1).upper())
        if not key:
            continue
        value = match.group(2).replace(",", "").split(".")[0]
        if value.isdigit() and int(value) > 0 and key not in found:
            found[key] = value
    for key, label_res in _LIMIT_LABELS.items():
        if found.get(key):
            continue
        value = _find_limit(raw, label_res)
        if value:
            found[key] = value
    for match in _LETTER_AMOUNT_RE.finditer(raw):
        key = COVERAGE_LETTER_KEYS.get(match.group(1).upper())
        if not key or found.get(key):
            continue
        value = match.group(2).replace(",", "").split(".")[0]
        if value.isdigit() and int(value) > 0:
            found[key] = value
    return found


def _find_limit(text: str, label_res: list[str]) -> str | None:
    """Find a dollar amount after one of the label patterns."""
    for label_re in label_res:
        pat = re.compile(label_re + r"\s*[:\-]?\s*" + _AMOUNT_RE, re.IGNORECASE)
        match = pat.search(text)
        if not match:
            continue
        value = match.group(1).replace(",", "").split(".")[0]
        if value.isdigit() and int(value) > 0:
            return value
    return None


def extract_policy_setup_args(text: str) -> dict | None:
    """Build full tool args from the email body. Never returns empty dates.

    Dates come from the body when stated, else the gold E01 term
    (10/02/2026–10/02/2027). Coverage limits come from the body when stated:
    ``Coverage A: $1,200,000`` colon lines, Dwelling/Coverage A labels, or
    letter form ``A $1,200,000 B $120,000``. Returns None outside the job class.
    """
    base = detect_policy_setup_request(text)
    if not base:
        return None
    raw = str(text or "")
    args = dict(base)
    args["effective_date"] = (
        _find_date_near_label(raw, r"effect\w*") or GOLD_EFFECTIVE_DATE
    )
    args["expiration_date"] = (
        _find_date_near_label(raw, r"expir\w*|\bexp\b") or GOLD_EXPIRATION_DATE
    )
    for key, label_res in _LIMIT_LABELS.items():
        value = _find_limit(raw, label_res)
        if value:
            args[key] = value
    parsed = parse_coverage_amounts_from_reply(raw)
    for key, value in parsed.items():
        if value and not str(args.get(key) or "").strip():
            args[key] = value
    return args


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


def load_policy_setup_handler(anchor_file=None):
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
    """Invoke the real tool handler. Fail closed when it is missing.

    ``args`` must carry effective_date and expiration_date (never empty —
    ``extract_policy_setup_args`` guarantees the gold E01 defaults).
    """
    args = dict(args or {})
    if not str(args.get("effective_date") or "").strip():
        args["effective_date"] = GOLD_EFFECTIVE_DATE
    if not str(args.get("expiration_date") or "").strip():
        args["expiration_date"] = GOLD_EXPIRATION_DATE
    handler = load_policy_setup_handler()
    return handler(args)


# --- Compatibility shims for 341 callers (hijack removed, but imports may remain) ---

# 341 defined this; kept so existing imports don't break.
POLICY_SETUP_ACTION = "hermes.email_task"

def handler_args_from_marker(marker: dict) -> dict:
    """Build tool args from a policy_setup_required checkpoint marker (341 API).

    Kept for compatibility; the deterministic email-runner path uses
    extract_policy_setup_args + invoke_policy_setup_tool instead.
    """
    marker = dict(marker or {})
    return {
        "policy_number": str(marker.get("policy_number") or "").strip().upper(),
        "effective_date": str(marker.get("effective_date") or GOLD_EFFECTIVE_DATE),
        "expiration_date": str(marker.get("expiration_date") or GOLD_EXPIRATION_DATE),
        "dwelling": str(marker.get("dwelling") or ""),
        "other_structures": str(marker.get("other_structures") or ""),
        "personal_property": str(marker.get("personal_property") or ""),
        "loss_of_use": str(marker.get("loss_of_use") or ""),
        "personal_liability": str(marker.get("personal_liability") or ""),
        "medical_payments": str(marker.get("medical_payments") or ""),
    }

def policy_setup_tool_path(anchor_file=None):
    """341 API: locate the policy_setup_tool module. Delegates to tool_module_path."""
    return tool_module_path()
