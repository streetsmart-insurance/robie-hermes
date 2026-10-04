"""Ascend notice driver stall check.

The driver appends one counts-only record per run. The hourly health check
reads that file and alerts when the last four completed live runs each saw
actionable notices and filed nothing (``done == 0``).

Actionable notices exclude informational mail (status ``ignored``) and
unrecognized mail (``Unrecognized Ascend notice type`` / notice type
``unknown``). Dry runs and fatal starts are recorded and then ignored by
the stall window.

The file is mode 0644 under a 0755 directory so the health-check user can
read it. On Production that user is ``carlo_streetsmart_insurance``, who
cannot read the system journal. Records are counts only: no subject, body,
applicant id, or secret.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

DEFAULT_STATE_DIR = "/var/lib/robie-ascend-notice-driver"
RUN_LOG_NAME = "runs.jsonl"
LAST_RUN_NAME = "last-run.json"
STALL_LIVE_RUNS = 4
UNRECOGNIZED_MARK = "Unrecognized Ascend notice type"
_UNRECOGNIZED_REASON_KEYS = frozenset(
    {"unknown_notice_type", "unrecognized", UNRECOGNIZED_MARK}
)


def run_log_path() -> str:
    """Path of the append-only run log.

    ``ASCEND_DRIVER_RUN_LOG`` wins. Otherwise the file is ``runs.jsonl``
    inside ``ASCEND_DRIVER_STATE_DIR`` or :data:`DEFAULT_STATE_DIR`.
    """
    override = os.environ.get("ASCEND_DRIVER_RUN_LOG", "").strip()
    if override:
        return override
    state = os.environ.get("ASCEND_DRIVER_STATE_DIR", "").strip() or DEFAULT_STATE_DIR
    return os.path.join(state, RUN_LOG_NAME)


def _detail(item: Mapping[str, Any]) -> Mapping[str, Any]:
    detail = item.get("detail")
    if isinstance(detail, Mapping):
        return detail
    return {}


def _is_unrecognized(item: Mapping[str, Any]) -> bool:
    if str(item.get("status") or "") == "ignored":
        return False
    reason = str(item.get("reason") or "")
    notice_type = str(_detail(item).get("notice_type") or "")
    return notice_type == "unknown" or UNRECOGNIZED_MARK in reason


def unrecognized_from_reasons(reasons: Mapping[str, Any] | None) -> int:
    """Fallback when a record has reason counts and no per-notice rows.

    ``needs_human_review`` is not subtracted. That bucket also holds
    applicant and API failures, which stay actionable.
    """
    total = 0
    for key, value in (reasons or {}).items():
        text = str(key)
        if text in _UNRECOGNIZED_REASON_KEYS or UNRECOGNIZED_MARK in text:
            total += int(value or 0)
    return total


def count_actionable(summary: Mapping[str, Any]) -> tuple[int, int]:
    """Return ``(actionable_seen, unrecognized)`` for one driver summary."""
    results = summary.get("results")
    if isinstance(results, list):
        actionable = 0
        unrecognized = 0
        for item in results:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("status") or "") == "ignored":
                continue
            if _is_unrecognized(item):
                unrecognized += 1
                continue
            actionable += 1
        return actionable, unrecognized
    seen = int(summary.get("notices_seen") or 0)
    ignored = int(summary.get("ignored") or 0)
    if "unrecognized" in summary and "results" not in summary:
        unrecognized = int(summary.get("unrecognized") or 0)
    else:
        unrecognized = unrecognized_from_reasons(summary.get("skipped_by_reason") or {})
    return max(0, seen - ignored - unrecognized), unrecognized


def annotate_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Add ``actionable_seen`` and ``unrecognized`` onto a driver summary."""
    if summary.get("fatal"):
        return summary
    actionable, unrecognized = count_actionable(summary)
    summary["unrecognized"] = unrecognized
    summary["actionable_seen"] = actionable
    return summary


def compact_run_record(
    summary: Mapping[str, Any],
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """Counts-only record safe to leave world-readable."""
    stamp = at or datetime.now(timezone.utc).isoformat()
    dry_run = bool(summary.get("dry_run"))
    fatal = summary.get("fatal")
    if fatal:
        return {
            "at": stamp,
            "dry_run": dry_run,
            "fatal": str(fatal)[:300],
        }
    actionable, unrecognized = count_actionable(summary)
    if "actionable_seen" in summary:
        actionable = int(summary.get("actionable_seen") or 0)
    if "unrecognized" in summary:
        unrecognized = int(summary.get("unrecognized") or 0)
    reasons = summary.get("skipped_by_reason") or {}
    return {
        "at": stamp,
        "dry_run": dry_run,
        "notices_seen": int(summary.get("notices_seen") or 0),
        "done": int(summary.get("done") or 0),
        "ignored": int(summary.get("ignored") or 0),
        "unrecognized": unrecognized,
        "actionable_seen": actionable,
        "skipped_by_reason": dict(reasons) if isinstance(reasons, Mapping) else {},
    }


def _chmod_world_readable(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def append_run_record(summary: Mapping[str, Any]) -> str | None:
    """Append one record and refresh ``last-run.json``.

    Failures are logged by the caller only through the returned ``None``.
    A missing state directory must not fail a driver run that already filed
    notes. The log and the directory are chmodded so a health check running
    as another user can read the counts. UMask inside the unit is 0077.
    """
    path = run_log_path()
    record = compact_run_record(summary)
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
            _chmod_world_readable(directory, 0o755)
        line = json.dumps(record, default=str, sort_keys=True) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
        _chmod_world_readable(path, 0o644)
        if directory:
            last = os.path.join(directory, LAST_RUN_NAME)
            with open(last, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, sort_keys=True)
                handle.write("\n")
            _chmod_world_readable(last, 0o644)
        return path
    except OSError:
        return None


def load_run_records(path: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except ValueError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def _actionable_of(record: Mapping[str, Any]) -> int:
    if "actionable_seen" in record:
        return int(record.get("actionable_seen") or 0)
    seen = int(record.get("notices_seen") or 0)
    ignored = int(record.get("ignored") or 0)
    if "unrecognized" in record:
        unrecognized = int(record.get("unrecognized") or 0)
    else:
        unrecognized = unrecognized_from_reasons(record.get("skipped_by_reason") or {})
    return max(0, seen - ignored - unrecognized)


def completed_live_run(record: Mapping[str, Any]) -> bool:
    """A finished live run. Dry runs and fatal starts do not count."""
    if record.get("fatal"):
        return False
    if record.get("dry_run") is not False:
        return False
    return isinstance(record.get("notices_seen"), int)


def evaluate_stall(
    records: list[Mapping[str, Any]],
    *,
    need: int = STALL_LIVE_RUNS,
) -> dict[str, Any]:
    """Quiet unless the last ``need`` live runs all filed nothing.

    Fewer than ``need`` completed live runs is quiet. A later success or a
    live run whose notices were all ignored or unrecognized breaks the streak.
    """
    live = [record for record in records if completed_live_run(record)]
    window = live[-need:]
    rows = [
        {
            "at": record.get("at"),
            "actionable_seen": _actionable_of(record),
            "done": int(record.get("done") or 0),
            "ignored": int(record.get("ignored") or 0),
            "unrecognized": int(record.get("unrecognized") or 0),
        }
        for record in window
    ]
    base = {"need": need, "runs_found": len(window), "last_runs": rows}
    if len(window) < need:
        return {
            "status": "OK",
            "detail": (
                f"ascend driver stall quiet: {len(window)} live run(s), need {need}"
            ),
            **base,
        }
    stalled = all(row["actionable_seen"] > 0 and row["done"] == 0 for row in rows)
    if stalled:
        return {
            "status": "ALERT",
            "detail": (
                f"ascend notices arrived but done=0 for {need} consecutive live runs"
            ),
            **base,
        }
    return {"status": "OK", "detail": "ascend driver not stalled", **base}


def evaluate_stall_file(
    path: str | None = None,
    *,
    need: int = STALL_LIVE_RUNS,
) -> dict[str, Any]:
    """Read the run log. Missing is quiet. Unreadable alerts."""
    target = path or run_log_path()
    if not os.path.exists(target):
        return {
            "status": "OK",
            "detail": "ascend driver run log not present yet",
            "path": target,
            "need": need,
            "runs_found": 0,
            "last_runs": [],
        }
    try:
        records = load_run_records(target)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        return {
            "status": "ALERT",
            "detail": f"ascend driver run log unreadable: {reason}",
            "path": target,
            "need": need,
            "runs_found": 0,
            "last_runs": [],
        }
    verdict = evaluate_stall(records, need=need)
    verdict["path"] = target
    return verdict
