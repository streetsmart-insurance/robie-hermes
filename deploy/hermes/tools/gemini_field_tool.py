"""Hermes tool: fail-closed Gemini unique-field helper after PLAYWRIGHT_BLOCKED."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tools.registry import registry


def _helper_path() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "robie_job_engine" / "gemini_field_helper.py",
        here.parents[3] / "robie_job_engine" / "gemini_field_helper.py",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError(
        "PLAYWRIGHT_BLOCKED: Gemini unique-field helper is missing; HITL Carlo"
    )


def _load_helper():
    try:
        from robie_job_engine.gemini_field_helper import (
            ask_gemini_unique_field,
            resolve_blocked_unique_write,
        )

        return ask_gemini_unique_field, resolve_blocked_unique_write
    except ImportError:
        path = _helper_path()
        spec = importlib.util.spec_from_file_location("robie_gemini_field_helper", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(
                "PLAYWRIGHT_BLOCKED: Gemini unique-field helper could not load; HITL Carlo"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.ask_gemini_unique_field, module.resolve_blocked_unique_write


def gemini_unique_field(
    dialog_title: str = "",
    visible_labels: list[str] | str | None = None,
    block_reason: str = "PLAYWRIGHT_BLOCKED",
    **_kwargs,
):
    from tools.registry import tool_error, tool_result

    labels = visible_labels
    if isinstance(labels, str):
        labels = [item.strip() for item in labels.splitlines() if item.strip()]
    labels = list(labels or [])
    try:
        _, resolve = _load_helper()
        decision = resolve(
            block_reason=block_reason or "PLAYWRIGHT_BLOCKED",
            dialog_title=dialog_title or "",
            visible_labels=labels,
        )
    except Exception as exc:
        return tool_error(
            f"PLAYWRIGHT_BLOCKED: Gemini unique-field helper failed ({type(exc).__name__}); "
            "HITL Carlo; refuse to guess"
        )
    payload = decision.to_dict()
    if payload["action"] != "APPLY":
        return tool_error(payload["reason"])
    return tool_result(payload)


GEMINI_UNIQUE_FIELD_SCHEMA = {
    "name": "gemini_unique_field",
    "description": (
        "After PLAYWRIGHT_BLOCKED or an unnamed EZLynx modal, send the dialog "
        "title and visible labels only (no passwords). Gemini may return one "
        "unique field to apply through unique-write, or the tool HITLs Carlo. "
        "Never guess. Never use .first/.nth/.last."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dialog_title": {
                "type": "string",
                "description": "Visible dialog or page title. No passwords.",
            },
            "visible_labels": {
                "description": "Visible field labels only. No passwords or secret values.",
            },
            "block_reason": {
                "type": "string",
                "description": "Exact PLAYWRIGHT_BLOCKED reason.",
            },
        },
        "required": ["dialog_title", "visible_labels"],
    },
}


registry.register(
    name="gemini_unique_field",
    toolset="playwright",
    schema=GEMINI_UNIQUE_FIELD_SCHEMA,
    handler=lambda args, **kwargs: gemini_unique_field(
        dialog_title=args.get("dialog_title", ""),
        visible_labels=args.get("visible_labels"),
        block_reason=args.get("block_reason", "PLAYWRIGHT_BLOCKED"),
        **kwargs,
    ),
    check_fn=lambda: True,
    emoji="🔎",
)
