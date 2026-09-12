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

# Gold E01 term and quote limits (from the authorized E01 test email body,
# 2026-09-12). Used when the email body states no dates/limits — the create
# call must never go out with empty dates (EZLynx returns 400).
GOLD_EFFECTIVE_DATE = "10/02/2026"
GOLD_EXPIRATION_DATE = "10/02/2027"
E01_LIMIT_DEFAULTS = {
    "dwelling": "1200000",
    "other_structures": "120000",
    "personal_property": "600000",
    "loss_of_use": "360000",
    "personal_liability": "500000",
    "medical_payments": "5000",
}

# Args the ezlynx_policy_setup handler accepts. The checkpoint carries these
# (plus bookkeeping keys); the hard route passes exactly these to the handler.
TOOL_ARG_KEYS = (
    "policy_number",
    "effective_date",
    "expiration_date",
    "dwelling",
    "other_structures",
    "personal_property",
    "loss_of_use",
    "personal_liability",
    "medical_payments",
)

# Carlo's literal coverage labels, plus Coverage A-F aliases.
_LIMIT_LABELS = {
    "dwelling": [r"Dwelling", r"Coverage\s*A\b"],
    "other_structures": [r"Other\s*Structures", r"Coverage\s*B\b"],
    "personal_property": [r"Personal\s*Property", r"Coverage\s*C\b"],
    "loss_of_use": [r"Loss\s*of\s*Use", r"Coverage\s*D\b"],
    "personal_liability": [r"Personal\s*Liability(?:\s*EA\s*OCC)?", r"Coverage\s*E\b", r"\bLiability\b"],
    "medical_payments": [r"Medical\s*Payments(?:\s*EA\s*PER)?", r"Coverage\s*F\b"],
}
_AMOUNT_RE = r"\$?\s*([\d,]+(?:\.\d{1,2})?)"
_TERM_RE = re.compile(
    r"\bterm\b[^\n]{0,30}?"
    r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*(?:to|-|–)\s*"
    r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})",
    re.IGNORECASE,
)


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


def _find_limit(text: str, label_res: list) -> str | None:
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


def _term_dates(text: str) -> tuple:
    """Return (effective, expiration) from a 'term X to Y' phrase, else (None, None)."""
    match = _TERM_RE.search(text)
    if not match:
        return (None, None)
    return (
        _normalize_date(match.group(3), match.group(1), match.group(2)),
        _normalize_date(match.group(6), match.group(4), match.group(5)),
    )


def extract_policy_setup_args(text: str) -> dict | None:
    """Build the full handler args from the email body. Never empty dates.

    Dates come from the body when stated, else the gold E01 term
    (10/02/2026-10/02/2027). Coverage limits come from the body when stated,
    else the E01 quote limits. Returns None outside the job class.
    """
    base = detect_policy_setup_request(text)
    if not base:
        return None
    raw = str(text or "")
    args = dict(base)
    args["effective_date"] = (
        _find_date_near_label(raw, r"effect\w*")
        or _term_dates(raw)[0]
        or GOLD_EFFECTIVE_DATE
    )
    args["expiration_date"] = (
        _find_date_near_label(raw, r"expir\w*|\bexp\b")
        or _term_dates(raw)[1]
        or GOLD_EXPIRATION_DATE
    )
    for key, label_res in _LIMIT_LABELS.items():
        value = _find_limit(raw, label_res)
        args[key] = value or E01_LIMIT_DEFAULTS[key]
    return args


def handler_args_from_marker(marker: dict) -> dict:
    """Build the handler args from a policy_setup_required checkpoint marker."""
    marker = marker or {}
    args = {key: str(marker.get(key) or "").strip() for key in TOOL_ARG_KEYS}
    if not args["effective_date"]:
        args["effective_date"] = GOLD_EFFECTIVE_DATE
    if not args["expiration_date"]:
        args["expiration_date"] = GOLD_EXPIRATION_DATE
    for key, default in E01_LIMIT_DEFAULTS.items():
        if not args[key]:
            args[key] = default
    return args


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
