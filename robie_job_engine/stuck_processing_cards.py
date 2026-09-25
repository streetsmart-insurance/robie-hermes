"""Clear Chat Approve cards left on "Processing" with no terminal update.

The Chat bridge replaces the buttons with a processing card before any
gateway has finished the click. If this gateway dies after the click, or
the in-place patch fails, that card stays on Processing. This ledger
remembers those message names and a later tick patches a safe terminal
card:

- confirmation still pending (no terminal decision): "This card is no
  longer active." The confirmation stays pending. Nothing is approved.
- confirmation already approved, rejected, or expired: the already-decided
  sentence, so the card does not keep saying Processing.

The sweeper never decides a confirmation and never enables policy-change
execution. ``POLICY_CHANGE_ENABLED`` stays false.

Default age is 8 minutes (``ROBIE_STUCK_PROCESSING_CARD_MINUTES``).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .confirmation_cards import INACTIVE_CARD_TEXT, already_decided_text
from .confirmations import read_confirmation_status

logger = logging.getLogger(__name__)

TABLE = "chat_processing_cards"
DEFAULT_STUCK_MINUTES = 8
TERMINAL_STATUSES = frozenset({"APPROVED", "REJECTED", "EXPIRED"})


def stuck_processing_minutes() -> int:
    """How long a processing card may sit before the sweeper clears it.

    A missing or unusable setting falls back to 8 minutes so one bad
    value cannot disable the sweep.
    """
    raw = os.environ.get(
        "ROBIE_STUCK_PROCESSING_CARD_MINUTES", str(DEFAULT_STUCK_MINUTES)
    )
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_STUCK_MINUTES
    if value < 1:
        return DEFAULT_STUCK_MINUTES
    return value


def terminal_patch_body(text: str) -> dict[str, Any]:
    """In-place Chat update: outcome text, buttons removed."""
    shown = str(text or "").strip() or INACTIVE_CARD_TEXT
    if not shown.startswith("✓ "):
        shown = f"✓ {shown}"
    return {"text": shown, "cardsV2": []}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            message_name TEXT PRIMARY KEY,
            confirmation_id TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            cleared_at TEXT
        )
        """
    )


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (TABLE,),
    ).fetchone()
    return row is not None


def note_processing_card(
    db_path: str,
    *,
    message_name: str,
    confirmation_id: str = "",
    started_at: datetime | None = None,
) -> None:
    """Remember that this Chat message is showing Processing.

    A later click on the same message resets the clock: the bridge puts
    Processing back up on every click.
    """
    name = str(message_name or "").strip()
    if not name:
        return
    started = started_at or datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_table(conn)
        conn.execute(
            f"""
            INSERT INTO {TABLE} (message_name, confirmation_id, started_at, cleared_at)
            VALUES (?, ?, ?, NULL)
            ON CONFLICT(message_name) DO UPDATE SET
                confirmation_id = excluded.confirmation_id,
                started_at = excluded.started_at,
                cleared_at = NULL
            """,
            (name, str(confirmation_id or "").strip(), started.isoformat()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_processing_card(
    db_path: str,
    message_name: str,
    *,
    cleared_at: datetime | None = None,
) -> None:
    """Mark a processing card finished. Missing table is a no-op."""
    name = str(message_name or "").strip()
    if not name or not os.path.isfile(db_path):
        return
    when = cleared_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    conn = _connect(db_path)
    try:
        if not _table_exists(conn):
            return
        conn.execute(
            f"UPDATE {TABLE} SET cleared_at = ? WHERE message_name = ?",
            (when.isoformat(), name),
        )
        conn.commit()
    finally:
        conn.close()


def _parse_started(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def stuck_processing_actions(
    db_path: str,
    *,
    now: datetime | None = None,
    older_than_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Cards due for a terminal patch. Does not patch and does not decide.

    Each item has ``message_name``, ``confirmation_id``, ``text``, and
    ``body`` (the Chat patch). A missing ledger table yields no actions
    and creates nothing.
    """
    if not db_path or not os.path.isfile(db_path):
        return []
    minutes = (
        older_than_minutes
        if older_than_minutes is not None
        else stuck_processing_minutes()
    )
    if minutes < 1:
        minutes = DEFAULT_STUCK_MINUTES
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    cutoff = moment - timedelta(minutes=minutes)
    conn = _connect(db_path)
    try:
        if not _table_exists(conn):
            return []
        rows = conn.execute(
            f"""
            SELECT message_name, confirmation_id, started_at
            FROM {TABLE}
            WHERE cleared_at IS NULL
            ORDER BY started_at, message_name
            """
        ).fetchall()
    finally:
        conn.close()

    actions: list[dict[str, Any]] = []
    for row in rows:
        started = _parse_started(str(row["started_at"] or ""))
        # Unreadable clocks are treated as already due so a bad stamp
        # cannot leave the card on Processing forever.
        if started is not None and started > cutoff:
            continue
        confirmation_id = str(row["confirmation_id"] or "").strip()
        status = (
            read_confirmation_status(db_path, confirmation_id)
            if confirmation_id
            else ""
        )
        if status in TERMINAL_STATUSES:
            text = already_decided_text({"status": status})
        else:
            text = INACTIVE_CARD_TEXT
        message_name = str(row["message_name"])
        actions.append(
            {
                "message_name": message_name,
                "confirmation_id": confirmation_id,
                "text": text,
                "body": terminal_patch_body(text),
            }
        )
    return actions


def mark_processing_card_cleared(db_path: str, message_name: str) -> None:
    """Alias used by the gateway after a successful patch."""
    clear_processing_card(db_path, message_name)


def sweep_stuck_processing_cards(
    db_path: str,
    patcher: Callable[[str, dict[str, Any]], Any],
    *,
    now: datetime | None = None,
    older_than_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Patch due cards, then mark only the successful patches cleared.

    ``patcher`` is ``(message_name, body) -> None``. A patch that raises
    leaves the row open for the next tick. Confirmation rows are not
    updated.
    """
    cleared: list[dict[str, Any]] = []
    for action in stuck_processing_actions(
        db_path, now=now, older_than_minutes=older_than_minutes
    ):
        try:
            patcher(action["message_name"], action["body"])
        except Exception:
            logger.warning(
                "stuck processing card patch failed ref=%s",
                (action.get("confirmation_id") or "-")[:8],
            )
            continue
        clear_processing_card(db_path, action["message_name"], cleared_at=now)
        cleared.append(action)
    return cleared


def run_stuck_processing_sweep(db_path: str) -> int:
    """Scheduler tick. Does not open Chat when nothing is due."""
    actions = stuck_processing_actions(db_path)
    if not actions:
        return 0
    from .chat_app_post import patch_message_as_chat_app

    def patcher(message_name: str, body: dict[str, Any]) -> None:
        patch_message_as_chat_app(message_name, body)

    return len(sweep_stuck_processing_cards(db_path, patcher))
