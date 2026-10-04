"""EZLynx safety checks the agent interpreter cannot rewrite.

Playwright user code runs in a child process and can rebind
``ALLOWED_EZLYNX_WRITE_APPLICANT_IDS``, ``note_id_in_discussion``,
``_note_id_of``, the driver gate, and the hard-block check. Those names
are captured when :func:`install_agent_seal` runs, which the Playwright
wrapper does before the agent's code. A later write compares the live
objects to that snapshot and refuses the call if any of them changed.
The snapshot is not the module global the agent assigns.

The same child installs an audit hook that refuses recognizable EZLynx
hostnames during connection and resolution. This also stops ordinary direct
HTTP clients before DNS yields a resolved IP. It does not attribute arbitrary
IP addresses to domains, block requests relayed through a permitted proxy,
or isolate native code or subprocesses; it is not a complete egress sandbox.
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
    """Freeze checks and refuse recognized EZLynx hostname audit events.

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
        address = address[0]
    if isinstance(address, bytes):
        try:
            address = address.decode("ascii")
        except UnicodeDecodeError:
            return ""
    if isinstance(address, str):
        return address.strip().casefold().rstrip(".")
    return str(getattr(address, "host", "") or "").strip().casefold().rstrip(".")


def _is_ezlynx_api_host(host: str) -> bool:
    if not host or host in {"127.0.0.1", "localhost", "::1"}:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in _EZLYNX_API_SUFFIXES)


def _audit_hook(event: str, args: tuple[object, ...]) -> None:
    if not _STATE["installed"]:
        return
    # CPython socket.connect and http.client.connect use (self, address/host,
    # ...), while the resolver events start with host. Refuse named hosts
    # before resolution; the subsequent connect may contain only an IP.
    if event in {"socket.connect", "http.client.connect"}:
        index = 1
    elif event in {"socket.getaddrinfo", "socket.gethostbyname"}:
        index = 0
    else:
        return
    if len(args) <= index or not _is_ezlynx_api_host(_host_of(args[index])):
        return
    raise SafetySealError(
        f"{TAMPERED}: EZLynx hostname connection or resolution from the agent "
        "interpreter is refused. The write was not sent."
    )


def agent_code_env_enabled() -> bool:
    value = os.environ.get(AGENT_ENV, "").strip().casefold()
    return value in {"1", "true", "yes", "on"}
