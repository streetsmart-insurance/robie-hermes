"""Lightweight notifier for the box-native certificate sweep.

Reads one sweep JSON summary (file path argument or stdin) and decides
what is noteworthy:

- ``filed`` non-empty — always noteworthy (Steffany's task was proven).
- ``errors`` non-empty — always noteworthy (the sweep needs attention).
- ``unverified`` non-empty — noteworthy (human eyes needed; the source
  email was left unread).

Quiet otherwise: an empty sweep produces no message and no log line.

Sinks (both fail-soft — this module never raises):

1. ``notifications.jsonl`` in the sweep data dir — one compact JSON line
   per noteworthy run, for the audit trail.
2. Google Chat home space via :func:`post_as_chat_app` — the Chat-app
   identity path already used by the job engine. When the Chat identity
   is not configured on the host, the sink is skipped and recorded.

No secrets are read or written here; the summary contains none.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

MAX_FILED_LINES = 5
MAX_UNVERIFIED_LINES = 8


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def noteworthy(summary: dict[str, Any]) -> dict[str, Any]:
    """Split a sweep summary into its noteworthy parts."""
    return {
        "filed": list(summary.get("filed") or []),
        "unverified": list(summary.get("unverified") or []),
        "errors": list(summary.get("errors") or []),
        "stats": dict(summary.get("stats") or {}),
        "sweep_at": summary.get("sweep_at", ""),
    }


def is_noteworthy(parts: dict[str, Any]) -> bool:
    return bool(parts["filed"] or parts["unverified"] or parts["errors"])


def _filed_line(entry: dict[str, Any]) -> str:
    subject = entry.get("subject") or "(no subject)"
    docs = entry.get("documents") or []
    doc_bits = f"email PDF + {len(docs) - 1} attachment(s)" if docs else "documents"
    task = "Steffany's review task created and confirmed" if entry.get(
        "task_id") or entry.get("task_action") else "task status unconfirmed"
    read = "Source email marked read." if entry.get(
        "marked_read") else "Source email left unread."
    return (f"\u2022 {subject} \u2014 {doc_bits} filed to Documents; {task}. "
            f"{read}")


def _unverified_line(entry: dict[str, Any]) -> str:
    subject = entry.get("subject") or "(no subject)"
    reason = entry.get("reason") or "unverified"
    if len(reason) > 220:
        reason = reason[:217] + "..."
    parked = entry.get("parked_for_human_review")
    tail = " (parked for human review)" if parked else " Email left unread."
    return f"\u2022 {subject} \u2014 {reason}.{tail}"


def render_message(parts: dict[str, Any]) -> str | None:
    """Plain-English Chat text for a noteworthy sweep, else None."""
    if not is_noteworthy(parts):
        return None
    stats = parts["stats"]
    lines = [
        f"Certificate sweep {parts['sweep_at'] or 'unknown time'}: "
        f"filed {stats.get('filed', 0)}, "
        f"needs-eyes {stats.get('unverified', 0)}, "
        f"errors {len(parts['errors'])}."
    ]
    if parts["filed"]:
        lines.append("Filed:")
        for entry in parts["filed"][:MAX_FILED_LINES]:
            lines.append(_filed_line(entry))
        extra = len(parts["filed"]) - MAX_FILED_LINES
        if extra > 0:
            lines.append(f"\u2022 \u2026and {extra} more (see sweep log).")
    if parts["unverified"]:
        lines.append("Needs human eyes:")
        for entry in parts["unverified"][:MAX_UNVERIFIED_LINES]:
            lines.append(_unverified_line(entry))
        extra = len(parts["unverified"]) - MAX_UNVERIFIED_LINES
        if extra > 0:
            lines.append(f"\u2022 \u2026and {extra} more (see sweep log).")
    if parts["errors"]:
        lines.append("Errors:")
        for err in parts["errors"][:5]:
            text = str(err)
            if len(text) > 220:
                text = text[:217] + "..."
            lines.append(f"\u2022 {text}")
    return "\n".join(lines)


def _default_chat_poster() -> Any:
    """Poster using the Chat-app identity; raises ChatAppIdentityError
    when the identity is not configured (caught by the caller)."""
    from .chat_app_post import post_as_chat_app, robie_home_space

    space = (_env("CERT_SWEEP_CHAT_SPACE") or robie_home_space() or "")

    def poster(text: str) -> dict[str, Any]:
        if not space:
            raise RuntimeError("no Chat space configured")
        return post_as_chat_app(space, text)

    return poster


def append_notification_log(data_dir: str, parts: dict[str, Any]) -> str:
    """Append one compact line to notifications.jsonl. Returns the path."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "notifications.jsonl")
    line = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sweep_at": parts["sweep_at"],
        "filed": len(parts["filed"]),
        "unverified": len(parts["unverified"]),
        "errors": len(parts["errors"]),
        "filed_subjects": [e.get("subject") for e in parts["filed"]],
        "unverified_subjects": [e.get("subject")
                                for e in parts["unverified"]],
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, default=str) + "\n")
    return path


def notify_summary(summary: dict[str, Any], *,
                   data_dir: str = "",
                   chat_poster: Any = None) -> dict[str, Any]:
    """Notify about a noteworthy sweep. Never raises.

    Returns ``{"notified": bool, "chat": {...}, "log_path": ...}``.
    """
    parts = noteworthy(summary)
    result: dict[str, Any] = {"notified": False, "chat": None,
                              "log_path": None}
    if not is_noteworthy(parts):
        return result
    data_dir = data_dir or os.path.expanduser(
        _env("CERT_SWEEP_DATA_DIR", "~/.cert-sweep"))
    try:
        result["log_path"] = append_notification_log(data_dir, parts)
    except Exception as exc:
        result["chat"] = {"skipped": f"log append failed: {exc}"}
    text = render_message(parts)
    if text is None:
        return result
    poster = chat_poster
    if poster is None:
        try:
            poster = _default_chat_poster()
        except Exception as exc:
            result["chat"] = {"skipped": f"chat identity unavailable: {exc}"}
            result["notified"] = result["log_path"] is not None
            return result
    try:
        posted = poster(text)
        result["chat"] = {"posted": posted}
        result["notified"] = True
    except Exception as exc:
        # Fail-soft: the log line above is the durable record; a Chat
        # outage must not fail the sweep wrapper.
        result["chat"] = {"failed": f"{type(exc).__name__}: {exc}"}
        result["notified"] = result["log_path"] is not None
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI: ``cert_notify [summary.json]`` (or JSON on stdin)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="cert_notify",
        description="Notify about noteworthy certificate-sweep outcomes.")
    parser.add_argument("summary_file", nargs="?",
                        help="sweep JSON summary file (default: stdin)")
    args = parser.parse_args(argv)

    try:
        if args.summary_file:
            with open(args.summary_file, encoding="utf-8") as fh:
                summary = json.load(fh)
        else:
            summary = json.load(sys.stdin)
    except Exception as exc:
        print(json.dumps({"notified": False,
                          "error": f"cannot read summary: {exc}"}))
        return 2
    result = notify_summary(summary)
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
