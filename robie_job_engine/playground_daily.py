"""Send Carlo the last 24 hours of Playground writes.

Chat DM first, then email. The Playground flag must be on. This module
does not deploy itself; the systemd timer calls it on the Test host.
"""

from __future__ import annotations

import argparse
import os
import sys

from .playground_config import DIGEST_RECIPIENT, digest_chat_user
from .playground_undo import daily_window, list_writes_since, render_daily_change_list
from .runtime_env import playground_enabled
from .store import JobStore


def build_daily_change_list(db_path: str, *, now=None) -> str:
    store = JobStore(db_path)
    since, until = daily_window(now=now)
    rows = list_writes_since(store, since=since, until=until)
    return render_daily_change_list(rows, now=until)


def deliver_daily_change_list(
    text: str,
    *,
    chat_sender=None,
    email_sender=None,
) -> dict[str, str]:
    """Prefer an existing Chat DM. Fall back to Carlo's email. Never a client."""
    if chat_sender is not None:
        try:
            chat_sender(digest_chat_user(), text)
            return {"channel": "chat", "status": "sent"}
        except Exception as exc:
            chat_error = type(exc).__name__
    else:
        chat_error = "not_configured"
    if email_sender is None:
        return {"channel": "none", "status": "undelivered", "chat_error": chat_error}
    email_sender(to=DIGEST_RECIPIENT, subject="Playground changes", body=text)
    return {"channel": "email", "status": "sent", "chat_error": chat_error}


def _default_chat_sender(user: str, text: str) -> None:
    from .chat_app_post import find_direct_message_space, post_as_chat_app

    space = find_direct_message_space(user)
    post_as_chat_app(space, text)


def _default_email_sender(*, to: str, subject: str, body: str) -> None:
    from .hitl_email import carlo_hitl_email_sender

    carlo_hitl_email_sender()(to=to, subject=subject, body=body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the Playground daily change list")
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "ROBIE_JOB_DB",
            "/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db",
        ),
    )
    parser.add_argument("--print-only", action="store_true")
    args = parser.parse_args(argv)
    if not playground_enabled():
        print("Playground is off. No change list sent.")
        return 0
    text = build_daily_change_list(args.db)
    if args.print_only:
        print(text)
        return 0
    try:
        result = deliver_daily_change_list(
            text,
            chat_sender=_default_chat_sender,
            email_sender=_default_email_sender,
        )
    except Exception as exc:
        print(f"Playground change list was not sent ({type(exc).__name__}).")
        return 1
    print(f"Playground change list {result.get('status')} via {result.get('channel')}.")
    if result.get("status") != "sent":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
