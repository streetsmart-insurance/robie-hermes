"""A Chat-originated EZLynx write waits for go in that job's thread.

Playground parks its own proposals. The model can still call a write tool
on the first Chat turn. This token is the gate at the tool and HTTP boundary:
no ledger row and no POST until this thread has said go.
"""
from __future__ import annotations

from typing import Any

GO_KIND = "chat_write_go"


def awaiting_go_reason(title: str, note_text: str) -> str:
    """Read the planned note back and wait. Nothing has been sent."""
    shown = " ".join(str(note_text or "").split())
    label = " ".join(str(title or "").split()) or "that discussion"
    return (
        f'Say go in this thread and I will add this note to "{label}": {shown}'
    )


def unconfirmed_was_not_added() -> str:
    """An unconfirmed ledger row is not a note that landed."""
    return "I couldn't confirm that note was added."


def bind_chat_write_go(
    store: Any,
    job_id: str,
    text: str,
    *,
    thread_id: str = "",
    message_id: str = "",
) -> bool:
    """Checkpoint one unused go for this job. A different thread does not count."""
    from .chat_thread import read_job_chat_thread, thread_resource_name
    from .playground_guardrails import is_go

    if not str(job_id or "").strip() or store is None or not is_go(text):
        return False
    job_thread = read_job_chat_thread(store, job_id) or ""
    inbound = thread_resource_name(thread_id) or ""
    if job_thread and inbound and job_thread != inbound:
        return False
    if job_thread and not inbound:
        inbound = job_thread
    store.checkpoint(
        job_id,
        GO_KIND,
        {
            "unused": True,
            "armed": False,
            "thread_id": inbound,
            "message_id": str(message_id or ""),
        },
    )
    return True


def _chat_turn() -> tuple[str, str]:
    """Job id and database for this model turn. Empty when this is not Chat."""
    from .turn_finalization import bound_model_context

    owner, _generation, db_path = bound_model_context()
    if not owner or not db_path:
        return "", ""
    return owner, db_path


def _token(store: Any, job_id: str) -> dict[str, Any]:
    from .chat_thread import read_job_chat_thread

    try:
        note = dict(store.get_checkpoint(job_id, GO_KIND) or {})
    except Exception:
        return {}
    if not note:
        return {}
    job_thread = read_job_chat_thread(store, job_id) or ""
    token_thread = str(note.get("thread_id") or "")
    if job_thread and token_thread and job_thread != token_thread:
        return {}
    if job_thread and not token_thread:
        return {}
    return note


def _go_state(note: dict[str, Any]) -> str:
    if not note:
        return "missing"
    if note.get("unused"):
        return "unused"
    if note.get("armed"):
        return "armed"
    return "spent"


def _save(store: Any, job_id: str, note: dict[str, Any], *, unused: bool, armed: bool) -> None:
    saved = dict(note)
    saved["unused"] = unused
    saved["armed"] = armed
    store.checkpoint(job_id, GO_KIND, saved)


def authorize_chat_note_post() -> bool:
    """True when this caller may append a note.

    A system caller has no bound Chat turn and stays allowed. A Chat turn
    must hold an unused go for its thread. That go is armed here, before
    the ledger row, and spent when the HTTP write is attempted.
    """
    owner, db_path = _chat_turn()
    if not owner:
        return True
    from .store import JobStore

    try:
        store = JobStore(db_path)
        note = _token(store, owner)
    except Exception:
        return False
    state = _go_state(note)
    if state == "unused":
        _save(store, owner, note, unused=False, armed=True)
        return True
    if state == "armed":
        return True
    return False


def finish_chat_note_post() -> None:
    """The append attempt is over. An armed go cannot be reused."""
    owner, db_path = _chat_turn()
    if not owner:
        return
    from .store import JobStore

    try:
        store = JobStore(db_path)
        note = _token(store, owner)
    except Exception:
        return
    if _go_state(note) == "armed":
        _save(store, owner, note, unused=False, armed=False)


def permit_chat_http_write() -> None:
    """Raise before HTTP when this Chat turn has no go left.

    ``authorize_chat_note_post`` arms the token first. This spends it on
    the one POST. A direct HTTP write with an unused token spends it here.
    """
    owner, db_path = _chat_turn()
    if not owner:
        return
    from .store import JobStore

    try:
        store = JobStore(db_path)
        note = _token(store, owner)
    except Exception as exc:
        raise RuntimeError(
            "EZLYNX_WRITE_REFUSED: say go in this thread before a Chat write"
        ) from exc
    state = _go_state(note)
    if state == "armed":
        _save(store, owner, note, unused=False, armed=False)
        return
    if state == "unused":
        _save(store, owner, note, unused=False, armed=False)
        return
    raise RuntimeError(
        "EZLYNX_WRITE_REFUSED: say go in this thread before a Chat write"
    )
