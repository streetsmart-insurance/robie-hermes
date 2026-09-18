"""Hook hermes-email-watcher (robie@ OAuth) into Ascend notice triage.

Ascend has no cancellation webhook. Cancel / late-pay / return-premium
notice emails land in the watched mailbox. This module is the smallest
call from ``scripts/robie_email_agent.py`` into the already-tested
:mod:`robie_job_engine.ascend_notice_driver`.

It does not rewrite triage, does not bind, does not email the insured,
and does not replace ``robie-ascend-sync`` ``/v1/cancelation_returns``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from . import ascend_notice_driver as driver

logger = logging.getLogger(__name__)

# Watcher writes EZLynx notes unless this flag is set. The standalone
# driver CLI stays dry-run-by-default; do not reuse ASCEND_DRIVER_LIVE here.
ASCEND_WATCHER_DRY_RUN_ENV = "ASCEND_WATCHER_DRY_RUN"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def is_ascend_notice_sender(sender: str) -> bool:
    """True when the From address is an Ascend mailbox.

    Accepts both a bare address and ``Name <user@useascend.com>`` so the
    watcher can classify before or after header parsing.
    """
    raw = (sender or "").strip().lower()
    if not raw:
        return False
    match = re.search(r"<([^>]+)>", raw)
    email = (match.group(1) if match else raw).strip()
    host = email.rsplit("@", 1)[-1].strip().rstrip(">").strip()
    return host == "useascend.com" or host.endswith(".useascend.com")


def watcher_notice_dry_run() -> bool:
    """Mailbox path writes notes; opt out with ASCEND_WATCHER_DRY_RUN=1."""
    return _truthy(os.environ.get(ASCEND_WATCHER_DRY_RUN_ENV))


def try_process_ascend_notice(
    *,
    sender: str,
    subject: str,
    body: str,
    message_id: str,
    ctx: driver.DriverContext | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any] | None:
    """Triage one watcher email if it is from Ascend. Never raises.

    Returns ``None`` when the sender is not Ascend (caller continues the
    normal email-agent path). Otherwise returns a result dict:

    - ``consumed``: add to processed-ids; do not reply or run the LLM
    - ``mark_read``: True only after a live ``done`` filing
    - ``status`` / ``reason`` / ``detail`` from :func:`process_notice`
    """
    if not is_ascend_notice_sender(sender):
        return None
    if dry_run is None:
        dry_run = watcher_notice_dry_run()
    notice = driver.EmailNotice(
        message_id=str(message_id or ""),
        subject=subject or "",
        body=body or "",
    )
    context = ctx
    if context is None:
        try:
            context = driver.build_processing_context(dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - fail closed, retry next tick
            logger.warning(
                "Ascend notice clients unavailable for %s: %s",
                message_id,
                type(exc).__name__,
            )
            return {
                "consumed": False,
                "mark_read": False,
                "status": "skipped",
                "reason": f"clients_unavailable: {type(exc).__name__}",
                "detail": {},
                "subject": subject or "",
                "message_id": str(message_id or ""),
            }
    result = driver.process_notice(notice, context)
    return {
        "consumed": True,
        "mark_read": result.status == "done",
        "status": result.status,
        "reason": result.reason,
        "detail": result.detail,
        "subject": result.subject,
        "message_id": result.message_id,
    }
