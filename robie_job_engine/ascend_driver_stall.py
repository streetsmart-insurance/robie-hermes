"""Ascend notice driver stall check.

The driver appends one counts-only record per run. The hourly health check
reads that file and alerts when the last four judged live runs each saw
actionable notices that were neither filed nor intentionally deduped.

Intentional dedupe is ``api_already_filed``, ``existing_note_duplicate``,
``duplicate_in_run``, and ``recent_same_notice``. Those notices were
already handled, so a live run whose actionable mail was all already
filed is healthy. Legitimate human deferrals (``needs_human_review``,
``applicant_unresolved``) are likewise not a stall: the driver triaged
them and handed them to a human, which is correct behavior. A run that
still has actionable notices outside those sets, and filed none of
them, is a stall. The human-deferred backlog is reported as
``human_deferred_backlog`` for visibility, not as an alert.

Actionable notices exclude informational mail (status ``ignored``) and
unrecognized mail (``Unrecognized Ascend notice type`` / notice type
``unknown``). Dry runs and fatal starts are recorded and then ignored by
the stall window.

Records written by this module include ``deduped``. Older records are
judged from ``skipped_by_reason`` when that map is present, so a log that
already counted ``api_already_filed`` goes quiet on the next health
check. A record with neither field is not judged. It cannot hold the
alert red. It ages out of the window: only the last four judged live
runs are read.

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
# Skips that mean the notice was already filed or collapsed. They are
# handled, not a stall. Prefixes match ``skipped_by_reason`` keys.
DEDUPE_REASONS = frozenset(
    {
        "api_already_filed",
        "existing_note_duplicate",
        "duplicate_in_run",
        "recent_same_notice",
    }
)
# Skips that mean the driver triaged the notice and legitimately handed it
# to a human (needs_human_review) or could not resolve the applicant
# (applicant_unresolved). The driver did its job; these are a review
# backlog, not a driver stall. Prefixes match ``skipped_by_reason`` keys.
HUMAN_DEFERRED_REASONS = frozenset(
    {
        "needs_human_review",
        "applicant_unresolved",
    }
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


def _reason_prefix(key: Any) -> str:
    return str(key or "").split(":", 1)[0].strip()


def deduped_from_reasons(reasons: Mapping[str, Any] | None) -> int:
    """How many skips were intentional dedupe, from a reason-count map."""
    total = 0
    for key, value in (reasons or {}).items():
        if _reason_prefix(key) in DEDUPE_REASONS:
            total += int(value or 0)
    return total


def human_deferred_from_reasons(reasons: Mapping[str, Any] | None) -> int:
    """How many skips were legitimate human deferrals, from a reason-count map."""
    total = 0
    for key, value in (reasons or {}).items():
        if _reason_prefix(key) in HUMAN_DEFERRED_REASONS:
            total += int(value or 0)
    return total


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


def count_deduped(summary: Mapping[str, Any]) -> int:
    """Intentional dedupe skips on one driver summary."""
    if "deduped" in summary:
        return int(summary.get("deduped") or 0)
    reasons = summary.get("skipped_by_reason")
    if isinstance(reasons, Mapping):
        return deduped_from_reasons(reasons)
    results = summary.get("results")
    if not isinstance(results, list):
        return 0
    total = 0
    for item in results:
        if isinstance(item, Mapping) and _reason_prefix(item.get("reason")) in DEDUPE_REASONS:
            total += 1
    return total


def annotate_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Add ``actionable_seen``, ``unrecognized``, and ``deduped``."""
    if summary.get("fatal"):
        return summary
    actionable, unrecognized = count_actionable(summary)
    summary["unrecognized"] = unrecognized
    summary["actionable_seen"] = actionable
    summary["deduped"] = count_deduped(summary)
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
        "deduped": count_deduped(summary),
        "human_deferred": human_deferred_from_reasons(
            reasons if isinstance(reasons, Mapping) else {}
        ),
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


def _deduped_of(record: Mapping[str, Any]) -> int | None:
    """Dedupe count, or None when this record cannot be judged.

    ``deduped`` is the counter this module writes. A record from before
    that field still counts when it stored ``skipped_by_reason``. A record
    with neither is left out of the stall window.
    """
    if "deduped" in record:
        return int(record.get("deduped") or 0)
    reasons = record.get("skipped_by_reason")
    if isinstance(reasons, Mapping):
        return deduped_from_reasons(reasons)
    return None


def _left_unhandled(record: Mapping[str, Any]) -> int:
    """Actionable notices that were neither filed nor deduped."""
    deduped = _deduped_of(record)
    if deduped is None:
        return 0
    return max(0, _actionable_of(record) - int(record.get("done") or 0) - deduped)


def _human_deferred_of(record: Mapping[str, Any]) -> int:
    """Notices legitimately deferred to humans on one run record.

    Falls back to ``skipped_by_reason`` for records written before the
    ``human_deferred`` counter existed.
    """
    if "human_deferred" in record:
        return int(record.get("human_deferred") or 0)
    reasons = record.get("skipped_by_reason")
    if isinstance(reasons, Mapping):
        return human_deferred_from_reasons(reasons)
    return 0


def _left_truly_unhandled(record: Mapping[str, Any]) -> int:
    """Actionable notices that were neither filed, deduped, nor legitimately
    deferred to a human. This is the stall signal: a healthy driver that
    correctly triages everything to humans leaves zero here."""
    deduped = _deduped_of(record)
    if deduped is None:
        return 0
    return max(
        0,
        _actionable_of(record)
        - int(record.get("done") or 0)
        - deduped
        - _human_deferred_of(record),
    )


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
    """Quiet unless the last ``need`` judged live runs each left work undone.

    A judged run is a completed live run that carries ``deduped`` or
    ``skipped_by_reason``. Fewer than ``need`` judged runs is quiet. A
    later filing, a run whose actionable notices were all deduped, or a
    run whose notices were all ignored or unrecognized breaks the streak.
    Records with neither counter are not judged, so they do not keep an
    old false alert red.

    Notices legitimately deferred to humans (``needs_human_review``,
    ``applicant_unresolved``) do NOT count as stalled work: a healthy
    driver that correctly triages everything to humans is working, not
    stuck. The deferred backlog is reported informationally via
    ``human_deferred_backlog`` so a growing queue is visible without
    paging as a stall.
    """
    live = [record for record in records if completed_live_run(record)]
    judged = [record for record in live if _deduped_of(record) is not None]
    window = judged[-need:]
    rows = [
        {
            "at": record.get("at"),
            "actionable_seen": _actionable_of(record),
            "done": int(record.get("done") or 0),
            "deduped": _deduped_of(record),
            "human_deferred": _human_deferred_of(record),
            "unhandled": _left_unhandled(record),
            "truly_unhandled": _left_truly_unhandled(record),
            "ignored": int(record.get("ignored") or 0),
            "unrecognized": int(record.get("unrecognized") or 0),
        }
        for record in window
    ]
    backlog = sum(row["human_deferred"] for row in rows)
    base = {
        "need": need,
        "runs_found": len(window),
        "unjudged": len(live) - len(judged),
        "human_deferred_backlog": backlog,
        "last_runs": rows,
    }
    if len(window) < need:
        return {
            "status": "OK",
            "detail": (
                f"ascend driver stall quiet: {len(window)} live run(s), need {need}"
            ),
            **base,
        }
    stalled = all(
        row["truly_unhandled"] > 0 and row["done"] == 0 for row in rows
    )
    if stalled:
        return {
            "status": "ALERT",
            "detail": (
                f"ascend notices arrived but done=0 for {need} consecutive live runs"
            ),
            **base,
        }
    detail = "ascend driver not stalled"
    if backlog:
        detail += f" ({backlog} notice(s) awaiting human review in window)"
    return {"status": "OK", "detail": detail, **base}


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
