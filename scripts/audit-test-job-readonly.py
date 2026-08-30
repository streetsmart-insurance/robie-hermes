#!/usr/bin/env python3
"""Redacted, read-only Test job audit by unique id prefix."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote


def _digest(value: str | None) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--job-prefix", required=True)
    args = parser.parse_args()
    path = Path(args.db).resolve()
    if not path.is_file() or len(args.job_prefix) < 8:
        raise SystemExit("refusing: missing database or short job prefix")
    uri = f"file:{quote(str(path))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    jobs = connection.execute(
        """SELECT id,action_type,status,attempt_count,verification_count,
                  last_error,created_at,updated_at,completed_at,payload_json
           FROM jobs WHERE id LIKE ? ORDER BY created_at""",
        (f"{args.job_prefix}%",),
    ).fetchall()
    if len(jobs) != 1:
        print(json.dumps({"result": "INCONCLUSIVE", "matching_jobs": len(jobs)}))
        return 3
    job = jobs[0]
    job_id = str(job["id"])
    payload_text = str(job["payload_json"] or "")
    checkpoints = connection.execute(
        "SELECT kind,data_json,created_at FROM checkpoints WHERE job_id=? ORDER BY id",
        (job_id,),
    ).fetchall()
    playwright = connection.execute(
        """SELECT id,tool,status,code_preview,result_json,created_at,updated_at
           FROM playwright_exec WHERE job_id=? ORDER BY id""",
        (job_id,),
    ).fetchall()
    evidence = connection.execute(
        """SELECT id,verified,method,source,authoritative,locator,
                  evidence_sha256,captured_at
           FROM verification_evidence WHERE job_id=? ORDER BY id""",
        (job_id,),
    ).fetchall()
    attempts = connection.execute(
        "SELECT phase,attempt_number,outcome,created_at FROM attempts WHERE job_id=? ORDER BY id",
        (job_id,),
    ).fetchall()
    checkpoint_summary = []
    post_audit = None
    for row in checkpoints:
        data_text = str(row["data_json"] or "{}")
        try:
            data = json.loads(data_text)
        except json.JSONDecodeError:
            data = {}
        checkpoint_summary.append(
            {
                "kind": row["kind"],
                "created_at": row["created_at"],
                "sha256": _digest(data_text),
                "verified": data.get("verified") if isinstance(data, dict) else None,
                "verdict": data.get("verdict") if isinstance(data, dict) else None,
                "authorizes_complete": (
                    data.get("authorizes_complete") if isinstance(data, dict) else None
                ),
            }
        )
        if row["kind"] == "post_job_audit" and isinstance(data, dict):
            post_audit = data
    report = {
        "result": "PASS",
        "job": {
            "id": job_id,
            "action_type": job["action_type"],
            "status": job["status"],
            "attempt_count": job["attempt_count"],
            "verification_count": job["verification_count"],
            "last_error_present": bool(job["last_error"]),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "completed_at": job["completed_at"],
            "expected_test_identity": {
                "applicant_220250093": "220250093" in payload_text,
                "commercial_auto": "commercial auto" in payload_text.casefold(),
                "robie_test_llc": "robie test llc" in payload_text.casefold(),
            },
        },
        "attempts": [dict(row) for row in attempts],
        "playwright_exec": [
            {
                "id": row["id"],
                "tool": row["tool"],
                "status": row["status"],
                "code_sha256": _digest(row["code_preview"]),
                "result_sha256": _digest(row["result_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in playwright
        ],
        "destination_evidence": [dict(row) for row in evidence],
        "checkpoints": checkpoint_summary,
        "post_job_audit": {
            "present": post_audit is not None,
            "verdict": (post_audit or {}).get("verdict"),
            "authorizes_complete": (post_audit or {}).get("authorizes_complete"),
            "destination_result": ((post_audit or {}).get("destination_evidence") or {}).get("result"),
            "playwright_result": ((post_audit or {}).get("playwright_exec") or {}).get("result"),
            "tool_vs_recording": ((post_audit or {}).get("tool_vs_recording") or {}).get("result"),
        },
        "completion_authorization": (
            "FOUND"
            if any(
                item.get("authorizes_complete") is True
                for item in checkpoint_summary
            )
            else "NOT_FOUND"
        ),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
