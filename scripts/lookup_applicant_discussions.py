#!/usr/bin/env python3
"""Read-only lookup of the discussions on ONE Test applicant: identifiers and field metadata only.

SEPARATE code from the field inspector: reviewing one does not clear the other.

Exactly three HTTPS requests, none else: the client's vendor token request (no user login), then
GET v8/discussions/ids-by-applicant and GET v8/discussions/by-applicant for applicant 220250093.
It runs only on hermes-test-01 and only on the approved live route with a client that has no
user-login grant and no browser-session access. Nothing is written anywhere: JSON goes to stdout.

Output (no titles, no note text, no values): applicant id; route (route, host, password_grant,
browser_cookies flag); discussion IDs; per discussion: id, note count, note key names with value types
(credential-named keys omitted), and, only if --task-id is given, a boolean for whether that exact ID
appears as a value under a task-named key (the ID itself is not echoed).
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from typing import Any

APPLICANT = "220250093"
TEST_HOST = "hermes-test-01"
OMIT = re.compile(r"pass(word|code)?|secret|token|otp|card|ssn|routing|account ?number|api[- ]?key|credential", re.I)


def _key_types(value: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict) and depth < 4:
        for key, inner in value.items():
            if OMIT.search(str(key)):
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(inner, dict):
                out.update(_key_types(inner, path, depth + 1))
            elif isinstance(inner, list):
                out[path] = "list"
            else:
                out[path] = "null" if inner is None else type(inner).__name__
    return out


def _has_task_id(value: Any, task_id: str, in_task_key: bool = False, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(value, dict):
        return any(_has_task_id(v, task_id, in_task_key or bool(re.search("task", str(k), re.I)), depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_task_id(v, task_id, in_task_key, depth + 1) for v in value)
    return in_task_key and str(value) == task_id


def lookup(client: Any, *, applicant_id: str, task_id: str | None = None) -> dict[str, Any]:
    if str(applicant_id).strip() != APPLICANT:
        raise SystemExit(f"REFUSED: only applicant {APPLICANT} may be looked up")
    route = getattr(client, "route_record", None)
    if (not isinstance(route, dict) or route.get("route") != "live" or route.get("password_grant") is not False
            or route.get("browser_cookies") is not False):
        raise SystemExit("REFUSED: the client is not on the approved live, vendor-grant-only, browser-session-free route")
    ids = [str(item) for item in client.get_discussion_ids(APPLICANT)]
    rows = client.get_discussions(APPLICANT)
    discussions = []
    for row in rows:
        notes = row.get("notes") or row.get("Notes") or []
        notes = notes if isinstance(notes, list) else []
        key_types: dict[str, str] = {}
        for note in notes:
            key_types.update(_key_types(note))
        entry: dict[str, Any] = {
            "discussion_id": str(row.get("id") or row.get("discussionId") or row.get("Id") or ""),
            "note_count": len(notes), "note_key_types": dict(sorted(key_types.items())),
        }
        if task_id:
            entry["contains_task_id"] = any(_has_task_id(note, str(task_id)) for note in notes)
        discussions.append(entry)
    return {"applicant_id": APPLICANT, "discussion_ids": ids, "discussions": discussions,
            "task_id_checked": bool(task_id),
            "route": {k: route[k] for k in ("route", "host", "password_grant", "browser_cookies")}}


def main(argv: list[str] | None = None, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--applicant-id", required=True)
    parser.add_argument("--task-id", help="optional: report which discussion holds this exact task ID (boolean only)")
    args = parser.parse_args(argv)
    if not socket.gethostname().startswith(TEST_HOST):
        print(f"REFUSED: not the Test VM ({TEST_HOST})")
        return 2
    try:
        if client is None:
            from robie_job_engine.task_discussion_route import build_task_discussion_client

            client = build_task_discussion_client()
        result = lookup(client, applicant_id=args.applicant_id, task_id=args.task_id)
    except SystemExit as exc:
        print(exc)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
