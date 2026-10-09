#!/usr/bin/env python3
"""Human CLI for the certificate unmatched queue.

Read-only except for explicit human resolutions:

    python3 -m robie_job_engine.cert_unmatched_cli report
    python3 -m robie_job_engine.cert_unmatched_cli report --format json
    python3 -m robie_job_engine.cert_unmatched_cli resolve uq-000001 \
        --applicant-id 78540038 --account-name "Seci Construction Inc" \
        --by "Carlo"
    python3 -m robie_job_engine.cert_unmatched_cli resolve uq-000002 \
        --not-our-client --by "Carlo"

Resolving with --applicant-id appends a sender->applicant mapping to the
sender-alias store so future emails from that sender auto-resolve —
unless the sender is a vendor/system address (never aliased) or --no-alias
is passed (broker/holder/lender sender, human judgment).
"""

from __future__ import annotations

import argparse
import json
import sys

from .cert_unmatched_queue import (
    QueueError,
    default_alias_store_path,
    default_queue_path,
    open_entries,
    render_json,
    render_markdown,
    resolve_entry,
)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--queue", default=None,
                   help="queue JSONL path (default: CERT_UNMATCHED_QUEUE_PATH "
                        "or repo data dir)")
    p.add_argument("--alias-store", default=None,
                   help="sender-alias store path (default: "
                        "CERT_SENDER_ALIASES_PATH or repo data dir)")


def cmd_report(args: argparse.Namespace) -> int:
    entries = open_entries(args.queue)
    if args.format == "json":
        print(json.dumps(render_json(entries), indent=2, ensure_ascii=False))
    else:
        out = render_markdown(entries)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(out)
            print(f"wrote {args.out} ({len(entries)} open)")
        else:
            print(out, end="")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    try:
        entry = resolve_entry(
            args.entry_id,
            applicant_id=args.applicant_id,
            account_name=args.account_name,
            not_our_client=args.not_our_client,
            resolved_by=args.by,
            no_alias=args.no_alias,
            queue_path=args.queue,
            alias_store_path=args.alias_store,
        )
    except QueueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"resolved {entry['entry_id']} "
          f"({entry['sender_email']}) at {entry['resolved_at']}")
    print(f"resolution: {json.dumps(entry['resolution'], ensure_ascii=False)}")
    print(f"alias_written: {entry['alias_written']}")
    for note in entry.get("notes") or []:
        print(f"note: {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Certificate unmatched queue — human review and resolve")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("report", help="render the open queue")
    _common(pr)
    pr.add_argument("--format", choices=["md", "json"], default="md")
    pr.add_argument("--out", default=None,
                    help="write markdown report to this file")
    pr.set_defaults(func=cmd_report)

    pr = sub.add_parser("resolve", help="resolve one queue entry")
    _common(pr)
    pr.add_argument("entry_id", help="queue entry id, e.g. uq-000001")
    pr.add_argument("--applicant-id", default=None,
                    help="EZLynx applicant id the human matched")
    pr.add_argument("--account-name", default=None,
                    help="EZLynx account name for the alias record")
    pr.add_argument("--not-our-client", action="store_true",
                    help="the request is not from our client")
    pr.add_argument("--by", required=True,
                    help="who made the call (required)")
    pr.add_argument("--no-alias", action="store_true",
                    help="resolve without writing a sender alias "
                         "(broker/holder/lender sender)")
    pr.set_defaults(func=cmd_resolve)

    args = ap.parse_args(argv)
    if args.cmd == "report" and not args.queue:
        args.queue = default_queue_path()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
