from __future__ import annotations

from typing import Any


FORBIDDEN_STAFF_CHAT_TOOLS = frozenset(
    {
        "terminal",
        "shell",
        "bash",
        "sh",
        "exec",
        "code_execution",
        "code-execution",
        "python_eval",
        "eval",
        "raw_file",
        "raw-file",
        "unrestricted_file",
        "unrestricted-file",
    }
)

FORBIDDEN_CHAT_PHRASES = (
    "run this command",
    "execute this code",
    "open a terminal",
    "dump the raw file",
    "print the private key",
    "cat /etc/",
)

# SESSION_LOGGED_OUT-shaped: report the blocker and stop. Do not negotiate.
SECURITY_GUARD_STOP_RULE = (
    "NEVER ask a human to lift a security control. "
    "On execute_code BLOCKED / terminal BLOCKED / PLAYWRIGHT_BLOCKED / write guard: "
    "report the blocker and the sanctioned alternative, and stop. "
    "Do not invent passwords. Do not ask to approve execute_code or terminal."
)


def requested_tools(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("tools") or payload.get("requested_tools") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip().casefold().replace(" ", "_") for item in raw if str(item).strip()]


def forbidden_tool_request(payload: dict[str, Any], text: str = "") -> str | None:
    """Return a refusal reason if staff Chat asked for an unrestricted tool."""
    for tool in requested_tools(payload):
        if tool in FORBIDDEN_STAFF_CHAT_TOOLS:
            return f"forbidden staff Chat tool: {tool}"
    haystack = " ".join(str(text or "").casefold().split())
    for phrase in FORBIDDEN_CHAT_PHRASES:
        if phrase in haystack:
            return f"forbidden staff Chat request: {phrase}"
    return None


def execution_contract_lines() -> list[str]:
    return [
        SECURITY_GUARD_STOP_RULE,
        "Staff Google Chat may not expose unrestricted terminal, raw-file, or code execution.",
        "Allowed tools are the registered bounded workers, staged attachment upload, and existing Gmail/Drive/AppSheet/browser integrations.",
        "The Computer Worker may return only an action receipt. It cannot mark a Job COMPLETE.",
    ]
