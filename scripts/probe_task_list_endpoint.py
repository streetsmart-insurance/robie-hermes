#!/usr/bin/env python3
"""Read-only probe: does an EZLynx path list tasks by assignee?

One GET to ``<Discussion API base>/<path>?assignedUserId=<id>`` with the
existing Task API login (vendor_data_access as the act-as user, the token
cache, and the one-failed-login stop). It prints the HTTP status, the body's
top-level key names and, for the first row, its key names only. It never
prints values, never writes to EZLynx, and tries exactly the path you name.

This exists because no list-by-assignee endpoint or call label field has been
confirmed (see robie_job_engine/ezlynx_task_source.py). Run it only in an
approved window, on the host whose secret you mean to read. A path that works
is then set as ROBIE_TASK_API_LIST_PATH. A 404 or an unexpected shape means
the API source stays off and the report remains the source.

    python scripts/probe_task_list_endpoint.py --path v8/<candidate>
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib import parse, request


def probe(path: str, assignee: int, urlopen=None) -> dict:
    from robie_job_engine import ezlynx_task_api as api

    clean = path.strip().strip("/")
    if not clean:
        return {"ok": False, "error": "path is required"}
    if not api.direct_task_api_enabled():
        return {"ok": False, "error": "direct Task API is switched off"}
    username = api.act_as_username()
    if not username or api.is_vendor_integration_username(username):
        return {"ok": False, "error": "act-as user is not set"}
    opener = urlopen or api._default_urlopen
    token, app, reason = api._ensure_token(username, None, opener)
    if not token or app is None:
        return {"ok": False, "error": f"login unavailable ({reason})"}
    url = f"{app['discussion_base'].rstrip('/')}/{clean}?{parse.urlencode({'assignedUserId': assignee})}"
    req = request.Request(url, headers=api._headers(token), method="GET")
    status, body, transport = api._send(req, opener, [token])
    out: dict = {"ok": False, "path": clean, "status": status, "transport_error": transport}
    if transport or status is None or not 200 <= status < 300:
        return out
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        out["error"] = "body is not JSON"
        return out
    rows = parsed if isinstance(parsed, list) else None
    if isinstance(parsed, dict):
        out["top_level_keys"] = sorted(parsed)
        for key in ("tasks", "Tasks", "notes", "Notes", "items", "Items"):
            if isinstance(parsed.get(key), list):
                rows = parsed[key]
                out["list_key"] = key
                break
    out["row_count"] = None if rows is None else len(rows)
    if rows and isinstance(rows[0], dict):
        out["first_row_keys"] = sorted(rows[0])
    out["ok"] = rows is not None
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", required=True)
    parser.add_argument("--assignee", type=int, default=438318)
    args = parser.parse_args(argv)
    result = probe(args.path, args.assignee)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
