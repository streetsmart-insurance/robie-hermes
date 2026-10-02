"""EZLynx safety checks the agent interpreter cannot rewrite.

Playwright user code runs in a child process and can rebind
``ALLOWED_EZLYNX_WRITE_APPLICANT_IDS``, ``note_id_in_discussion``,
``_note_id_of``, the driver gate, and the hard-block check. Those names
are captured when :func:`install_agent_seal` runs, which the Playwright
wrapper does before the agent's code. A later write compares the live
objects to that snapshot and refuses the call if any of them changed.
The snapshot is not the module global the agent assigns.

The same child installs an audit hook. Agent code cannot remove an audit
hook. A socket to the EZLynx API from that interpreter is refused, so a
patched ``urlopen`` still cannot post the note.
"""

from __future__ import annotations

import os
import sys
from typing import Any

TAMPERED = "SAFETY_SEAL_TAMPERED"
AGENT_ENV = "ROBIE_AGENT_CODE"

_EZLYNX_API_SUFFIXES = ("ezlynx.com", "uatezlynx.com")


class SafetySealError(RuntimeError):
    """A frozen check was replaced, or the agent interpreter tried to write."""


_STATE: dict[str, Any] = {
    "installed": False,
    "allow_obj": None,
    "codes": {},
    "require_driver": None,
    "hard_block": None,
}


def agent_interpreter() -> bool:
    """True after the Playwright child has frozen its checks."""
    return bool(_STATE["installed"])


def install_agent_seal() -> None:
    """Freeze the checks and refuse EZLynx API sockets from this process.

    Idempotent. Called once, before agent code runs. A second call does
    not take a new snapshot and does not add another hook.
    """
    if _STATE["installed"]:
        return
    from . import chat_job_controls, ezlynx_api_only_writes, ezlynx_discussions
    from . import ezlynx_driver_gate, ezlynx_write_scope, live_turn_guard

    scope = ezlynx_write_scope
    _STATE["allow_obj"] = scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS
    _STATE["codes"] = {
        "applicant_is_write_allowed": scope.applicant_is_write_allowed.__code__,
        "require_allowed_ezlynx_write_applicant": (
            scope.require_allowed_ezlynx_write_applicant.__code__
        ),
        "note_id_in_discussion": ezlynx_api_only_writes.note_id_in_discussion.__code__,
        "_note_id_of": ezlynx_discussions._note_id_of.__code__,
        "require_driver_in": ezlynx_driver_gate.require_driver_in.__code__,
        "check_driver_gate": ezlynx_driver_gate.check_driver_gate.__code__,
        "hard_block_reply": chat_job_controls.hard_block_reply.__code__,
        "assert_live_write_allowed": live_turn_guard.assert_live_write_allowed.__code__,
    }
    _STATE["require_driver"] = ezlynx_driver_gate.require_driver_in
    _STATE["hard_block"] = chat_job_controls.hard_block_reply
    _STATE["installed"] = True
    sys.addaudithook(_audit_hook)


def assert_write_checks_intact() -> None:
    """Raise when this process's agent seal sees a replaced check.

    No-op until :func:`install_agent_seal`. Unit tests that rebind the
    allowlist do not install the seal, so they keep the test double.
    """
    if not _STATE["installed"]:
        return
    from . import chat_job_controls, ezlynx_api_only_writes, ezlynx_discussions
    from . import ezlynx_driver_gate, ezlynx_write_scope, live_turn_guard

    live = {
        "applicant_is_write_allowed": ezlynx_write_scope.applicant_is_write_allowed,
        "require_allowed_ezlynx_write_applicant": (
            ezlynx_write_scope.require_allowed_ezlynx_write_applicant
        ),
        "note_id_in_discussion": ezlynx_api_only_writes.note_id_in_discussion,
        "_note_id_of": ezlynx_discussions._note_id_of,
        "require_driver_in": ezlynx_driver_gate.require_driver_in,
        "check_driver_gate": ezlynx_driver_gate.check_driver_gate,
        "hard_block_reply": chat_job_controls.hard_block_reply,
        "assert_live_write_allowed": live_turn_guard.assert_live_write_allowed,
    }
    for name, fn in live.items():
        code = getattr(fn, "__code__", None)
        if code is not _STATE["codes"].get(name):
            raise SafetySealError(
                f"{TAMPERED}: {name} was replaced in the agent interpreter. "
                "The write was not sent."
            )
    if ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS is not _STATE["allow_obj"]:
        raise SafetySealError(
            f"{TAMPERED}: the EZLynx write allowlist was changed in the agent "
            "interpreter. The write was not sent."
        )


def driver_gate_for_write() -> None:
    """The lease check captured at seal install, or the live one before that."""
    assert_write_checks_intact()
    frozen = _STATE.get("require_driver")
    if _STATE["installed"] and callable(frozen):
        frozen()
        return
    from .ezlynx_driver_gate import require_driver_in

    require_driver_in()


def hard_block_for_write(text: str) -> str | None:
    """The hard-block function captured at seal install."""
    assert_write_checks_intact()
    frozen = _STATE.get("hard_block")
    if _STATE["installed"] and callable(frozen):
        return frozen(text)
    from .chat_job_controls import hard_block_reply

    return hard_block_reply(text)


def _host_of(address: object) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0] or "").strip().casefold().rstrip(".")
    if isinstance(address, str):
        return address.strip().casefold().rstrip(".")
    text = str(getattr(address, "host", "") or "").strip().casefold().rstrip(".")
    return text


def _is_ezlynx_api_host(host: str) -> bool:
    if not host or host in {"127.0.0.1", "localhost", "::1"}:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in _EZLYNX_API_SUFFIXES)


def _audit_hook(event: str, args: tuple[object, ...]) -> None:
    if not _STATE["installed"] or event != "socket.connect" or not args:
        return
    host = _host_of(args[0])
    if not _is_ezlynx_api_host(host):
        return
    raise SafetySealError(
        f"{TAMPERED}: EZLynx API connect from the agent interpreter is refused. "
        "The write was not sent."
    )


def agent_code_env_enabled() -> bool:
    value = os.environ.get(AGENT_ENV, "").strip().casefold()
    return value in {"1", "true", "yes", "on"}
