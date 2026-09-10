#!/usr/bin/env python3
"""Read the scheduler tick's login-secret health sentinel and print the
newest ENABLED secret-version resource names as KEY=VALUE lines.

Read-only: opens the job DB in read-only mode and reads the
login_secret_health sentinel checkpoint, which holds Secret Manager
version STATES (never payloads). Exit 2 (fail closed) when the sentinel
is missing, stale, or a secret has no ENABLED version.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
DEFAULT_PROJECT = "streetsmart-hermes-poc"
SENTINEL_KEY = "login_secret_health"
MAX_AGE_SECONDS = 2 * 60 * 60

# Secret Manager secret id -> environment variable the engine reads.
ENV_MAP = {
    "ezlynx-username": "ROBIE_EZLYNX_USERNAME_SECRET",
    "ezlynx-password": "ROBIE_EZLYNX_PASSWORD_SECRET",
}


def fail(message: str) -> int:
    print(f"read-login-secret-versions: refusing: {message}", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print newest ENABLED secret-version resource names "
        "(states only, never payloads)."
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_file():
        return fail(f"db not found: {db_path}")
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT id FROM jobs WHERE idempotency_key=? "
                "ORDER BY created_at DESC LIMIT 1",
                (SENTINEL_KEY,),
            ).fetchone()
            if not row:
                return fail("login_secret_health sentinel job not found")
            cp = con.execute(
                "SELECT data_json, created_at FROM checkpoints "
                "WHERE job_id=? AND kind=?",
                (row[0], SENTINEL_KEY),
            ).fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return fail(f"db read failed: {type(exc).__name__}: {exc}")
    if not cp:
        return fail("login_secret_health checkpoint not found")
    try:
        data = json.loads(cp[0])
    except Exception as exc:  # noqa: BLE001
        return fail(f"checkpoint is not valid JSON: {exc}")
    try:
        checked_at = datetime.fromisoformat(str(cp[1]))
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - checked_at).total_seconds()
    except Exception:  # noqa: BLE001
        age = float("inf")
    if age > MAX_AGE_SECONDS:
        return fail(f"sentinel checkpoint is stale ({age:.0f}s old)")
    by_id = {item.get("secret_id"): item for item in data.get("secrets") or []}
    lines = []
    for secret_id, env_var in ENV_MAP.items():
        item = by_id.get(secret_id)
        version = (item or {}).get("newest_enabled_version")
        if version is None:
            return fail(f"no ENABLED version for {secret_id}")
        resource = (
            f"projects/{args.project}/secrets/{secret_id}/versions/{version}"
        )
        lines.append(f"{env_var}={resource}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
