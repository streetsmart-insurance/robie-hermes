"""Keep the guarded browser schema direct on the installed Hermes interface.

Email and Chat workers see ``playwright_exec`` and ``ezlynx_policy_setup``
and never ``execute_code`` or ``terminal``.
Interactive desktop Hermes is unchanged: this module does not strip
``execute_code`` or ``terminal`` unless the process is an email/chat job worker.
``expose_guarded_browser`` still pins ``playwright_exec`` for hermes-gateway.
"""
from __future__ import annotations

import os
import sys

EMAIL_CHAT_ACTIONS = frozenset({"hermes.email_task", "hermes.google_chat_task"})
EXECUTE_CODE_TOOLS = frozenset({"execute_code", "code_execution", "code-execution"})
TERMINAL_TOOLS = frozenset({"terminal", "shell", "bash", "sh"})
FORBIDDEN_WORKER_TOOLS = EXECUTE_CODE_TOOLS | TERMINAL_TOOLS
_FILTER_MARK = "_robie_email_chat_filter"


def is_email_or_chat_worker(action_type=None, env=None, argv=None) -> bool:
    """True only for hermes.email_task / hermes.google_chat_task workers.

    Interactive desktop ``hermes chat`` has no job action, no single-query
    email job, and is not ``gateway run``.
    """
    env = os.environ if env is None else env
    argv = sys.argv if argv is None else argv
    action = action_type or env.get("ROBIE_JOB_ACTION") or ""
    if action in EMAIL_CHAT_ACTIONS:
        return True
    if env.get("ROBIE_JOB_ID") and env.get("HERMES_SINGLE_QUERY_SESSION") == "1":
        return True
    return any(str(part) == "gateway" for part in argv)


def email_chat_job_schema(tool_names):
    """Email/chat job schema: ``playwright_exec`` + ``ezlynx_policy_setup`` in,
    ``execute_code`` and ``terminal`` out."""
    names = [name for name in list(tool_names or []) if name not in FORBIDDEN_WORKER_TOOLS]
    if "playwright_exec" not in names:
        names.append("playwright_exec")
    if "ezlynx_policy_setup" not in names:
        names.append("ezlynx_policy_setup")
    return names


def _schema_name(item) -> str:
    if isinstance(item, dict):
        function = item.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        return str(item.get("name") or "")
    return str(getattr(item, "name", item) or "")


def filter_email_chat_schemas(schemas):
    """Drop execute_code and terminal from a Hermes tool-schema list."""
    return [item for item in list(schemas or []) if _schema_name(item) not in FORBIDDEN_WORKER_TOOLS]


def _hide_execute_code_from_core(core) -> None:
    if not isinstance(core, list):
        return
    core[:] = [name for name in core if name not in FORBIDDEN_WORKER_TOOLS]


def install_email_chat_schema_filter():
    """Wrap Hermes schema assembly so email/chat jobs never see execute_code or terminal.

    Safe when Hermes is absent (CI). Interactive calls are left unchanged.
    """
    try:
        import model_tools
    except ImportError:
        model_tools = None
    if model_tools is not None:
        original = getattr(model_tools, "get_tool_definitions", None)
        if callable(original) and not getattr(original, _FILTER_MARK, False):
            def wrapped(*args, **kwargs):
                if is_email_or_chat_worker():
                    disabled = list(kwargs.get("disabled_toolsets") or [])
                    for toolset in ("code_execution", "terminal"):
                        if toolset not in disabled:
                            disabled.append(toolset)
                    kwargs["disabled_toolsets"] = disabled
                schemas = original(*args, **kwargs)
                if is_email_or_chat_worker():
                    return filter_email_chat_schemas(schemas)
                return schemas

            setattr(wrapped, _FILTER_MARK, True)
            model_tools.get_tool_definitions = wrapped
    try:
        from tools import tool_search
    except ImportError:
        return
    classify = getattr(tool_search, "classify_tools", None)
    if not callable(classify) or getattr(classify, _FILTER_MARK, False):
        return

    def classified(*args, **kwargs):
        visible, deferred = classify(*args, **kwargs)
        if not is_email_or_chat_worker():
            return visible, deferred
        return filter_email_chat_schemas(visible), filter_email_chat_schemas(deferred)

    setattr(classified, _FILTER_MARK, True)
    tool_search.classify_tools = classified


def expose_guarded_browser(toolsets=None, *, action_type=None, env=None, argv=None):
    if toolsets is None:
        import toolsets
    core = getattr(toolsets, "_HERMES_CORE_TOOLS", None)
    # This installed interface was measured on Production. Do not silently
    # disable the repair if an upstream update changes its contract.
    if not isinstance(core, list) or not all(isinstance(name, str) for name in core):
        raise RuntimeError("Unsupported Hermes direct-tool visibility interface")
    if "playwright_exec" not in core:
        core.append("playwright_exec")
    # Email/Chat jobs that create a homeowners policy must use the engine's
    # structured policy-setup tool, not wander with playwright_exec.
    if "ezlynx_policy_setup" not in core:
        core.append("ezlynx_policy_setup")
    if is_email_or_chat_worker(action_type=action_type, env=env, argv=argv):
        _hide_execute_code_from_core(core)
    install_email_chat_schema_filter()
