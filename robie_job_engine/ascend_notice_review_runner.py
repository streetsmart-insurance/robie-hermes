"""Always-on runner for the Ascend notice review queue. Propose-only.

Wraps :mod:`robie_job_engine.ascend_notice_discovery` for a systemd timer:

- Test only: refuses unless ``ROBIE_ENV=TEST`` and
  ``ROBIE_ASCEND_NOTICE_REVIEW=1``.
- Incremental: each mailbox has a durable checkpoint (the end of its last
  *complete* scan). A run scans from ``checkpoint - overlap`` to tomorrow,
  so a late-indexed message is seen again; the queue is idempotent per
  (mailbox, message id), so the overlap never makes duplicates. The
  checkpoint moves only after a complete scan. An incomplete scan keeps the
  old checkpoint and the next run covers the same window again.
- First run per mailbox starts at ``ROBIE_ASCEND_NOTICE_REVIEW_START`` (an
  explicit ISO date). There is no implicit backfill.
- Single instance: a non-blocking file lock; a second run exits at once.
- Read-only by construction: the Gmail client is built with readonly scope
  (``modify=False``) and wrapped in :class:`ReadOnlyGmail`, which exposes
  only ``messages().list`` and ``messages().get``. Anything else raises
  before a request is built.
- Propose-only: each queued item gets a ``proposal`` describing what a
  human might do, with ``propose_only=True``, ``may_file=False``,
  ``may_send_task=False``. Nothing here files a note, creates a task,
  labels, or marks mail read.
- Status: a JSON status file (0600) records the last run per mailbox for
  health checks.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .ascend_notice_discovery import ReviewQueue, scan

ENABLE_ENV = "ROBIE_ASCEND_NOTICE_REVIEW"
MAILBOXES_ENV = "ROBIE_ASCEND_NOTICE_REVIEW_MAILBOXES"
START_ENV = "ROBIE_ASCEND_NOTICE_REVIEW_START"
STATE_DIR_ENV = "ROBIE_ASCEND_NOTICE_REVIEW_STATE_DIR"
DELEGATION_ENV = "ROBIE_ASCEND_NOTICE_DELEGATION_SA"
OVERLAP_DAYS = 2

PROPOSALS = {
    "past_due": "Review the past-due notice; if confirmed, a CSR follow-up task for the account.",
    "payment_failed": "Review the failed payment; if confirmed, a CSR follow-up task for the account.",
    "cancellation": "Review the cancellation notice; if confirmed, a note and a CSR task on the account.",
    "return_premium": "Review the return premium; if confirmed, an accounting note on the account.",
    "new_program": "Review the new program; if confirmed, link it to the EZLynx account.",
    "ambiguous": "Several notice types matched; a person decides which applies.",
    "unknown": "Ascend-looking message with no known notice type; a person decides.",
}


class RefusedGmailCall(RuntimeError):
    """A non-read Gmail call was attempted through the read-only wrapper."""


class _Messages:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def list(self, **kwargs: Any) -> Any:
        return self._inner.list(**kwargs)

    def get(self, **kwargs: Any) -> Any:
        if kwargs.get("format") not in (None, "full", "metadata", "minimal"):
            raise RefusedGmailCall(f"messages.get format {kwargs.get('format')!r}")
        return self._inner.get(**kwargs)

    def __getattr__(self, name: str) -> Any:
        raise RefusedGmailCall(f"messages.{name} is not allowed (read-only runner)")


class _Users:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def messages(self) -> _Messages:
        return _Messages(self._inner.messages())

    def __getattr__(self, name: str) -> Any:
        raise RefusedGmailCall(f"users.{name} is not allowed (read-only runner)")


class ReadOnlyGmail:
    """Gmail service wrapper exposing users().messages().list/get only."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def users(self) -> _Users:
        return _Users(self._service.users())

    def __getattr__(self, name: str) -> Any:
        raise RefusedGmailCall(f"{name} is not allowed (read-only runner)")


class ProposingQueue:
    """ReviewQueue that attaches a propose-only proposal to every item."""

    def __init__(self, queue: ReviewQueue) -> None:
        self._queue = queue

    def put(self, mailbox: str, message_id: str, item: dict[str, Any]) -> None:
        family = item.get("notice_family") or ("unknown" if item.get("reason") != "fetch_or_decode_failed" else "")
        item = dict(item)
        item["proposal"] = {
            "summary": PROPOSALS.get(family, "Fetch failed; a person re-checks this message."),
            "propose_only": True,
        }
        item["may_file"] = False
        item["may_send_task"] = False
        item["source_verified"] = False
        self._queue.put(mailbox, message_id, item)

    def rows(self) -> list[dict[str, Any]]:
        return self._queue.rows()

    def close(self) -> None:
        self._queue.close()


def _state_dir() -> Path:
    raw = os.getenv(STATE_DIR_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"{STATE_DIR_ENV} is required")
    path = Path(raw)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return path


def _write_json(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(data, indent=2, sort_keys=True).encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def single_instance(lock_path: Path) -> Iterator[bool]:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def scan_window(checkpoint: str | None, first_start: str | None, today: date) -> tuple[str, str]:
    """(start, end) for this run. End is tomorrow so today is included."""
    if checkpoint:
        start = date.fromisoformat(checkpoint) - timedelta(days=OVERLAP_DAYS)
    elif first_start:
        start = date.fromisoformat(first_start)
    else:
        raise RuntimeError(f"no checkpoint and {START_ENV} is unset: refusing an implicit backfill")
    end = today + timedelta(days=1)
    if start >= end:
        start = end - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def run_once(
    *,
    service_factory: Callable[[str, str], Any],
    mailboxes: list[str],
    today: date | None = None,
    first_start: str | None = None,
) -> dict[str, Any]:
    """One pass over every mailbox. Returns a JSON-serializable report."""
    if os.environ.get("ROBIE_ENV", "").upper() != "TEST":
        raise RuntimeError("TEST_ONLY: refusing outside ROBIE_ENV=TEST")
    if os.environ.get(ENABLE_ENV) != "1":
        return {"status": "disabled", "reason": f"{ENABLE_ENV} is not 1"}
    if not mailboxes:
        raise RuntimeError(f"{MAILBOXES_ENV} is empty")
    today = today or datetime.now(timezone.utc).date()
    state_dir = _state_dir()
    checkpoints_path = state_dir / "checkpoints.json"
    status_path = state_dir / "status.json"
    with single_instance(state_dir / "runner.lock") as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "another run holds the lock"}
        checkpoints = _read_json(checkpoints_path)
        report: dict[str, Any] = {"status": "ok", "ran_at": datetime.now(timezone.utc).isoformat(),
                                  "mode": "propose_only", "mailboxes": {}}
        queue = ProposingQueue(ReviewQueue(str(state_dir / "review_queue.db")))
        try:
            for mailbox in mailboxes:
                start, end = scan_window(checkpoints.get(mailbox), first_start, today)
                try:
                    service = ReadOnlyGmail(service_factory(mailbox))
                    result = scan(service, mailbox, queue, start_date=start, end_date=end)
                except Exception as exc:  # noqa: BLE001 - keep other mailboxes going
                    result = {"complete": False, "errors": [f"run:{type(exc).__name__}"],
                              "destination_writes": 0, "gmail_label_changes": 0}
                if result.get("complete"):
                    # The window ended at tomorrow; today is the next start point.
                    checkpoints[mailbox] = today.isoformat()
                else:
                    report["status"] = "incomplete"
                report["mailboxes"][mailbox] = {
                    "window": [start, end], "complete": bool(result.get("complete")),
                    "pages": result.get("pages"), "fetched": result.get("fetched"),
                    "review": result.get("review"), "unrelated": result.get("unrelated"),
                    "errors": result.get("errors", [])[:20],
                    "checkpoint": checkpoints.get(mailbox),
                    "destination_writes": result.get("destination_writes", 0),
                    "gmail_label_changes": result.get("gmail_label_changes", 0),
                }
            report["queue_rows"] = len(queue.rows())
        finally:
            queue.close()
        _write_json(checkpoints_path, checkpoints)
        _write_json(status_path, report)
        return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Ascend notice review runner (Test, propose-only)")
    parser.add_argument("--json", action="store_true")
    parser.parse_args(argv)
    mailboxes = [m.strip() for m in os.getenv(MAILBOXES_ENV, "").split(",") if m.strip()]
    delegation = os.getenv(DELEGATION_ENV, "").strip()
    if not delegation:
        print(json.dumps({"status": "refused", "reason": f"{DELEGATION_ENV} is unset"}))
        return 2

    def factory(mailbox: str) -> Any:
        from .gmail_accountability import build_notice_gmail_service

        return build_notice_gmail_service(delegation, mailbox, modify=False)

    report = run_once(service_factory=factory, mailboxes=mailboxes,
                      first_start=os.getenv(START_ENV, "").strip() or None)
    print(json.dumps(report, indent=2))
    return 0 if report.get("status") in ("ok", "disabled", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
