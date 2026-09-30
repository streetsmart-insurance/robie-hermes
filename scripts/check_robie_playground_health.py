#!/usr/bin/env python3
"""Same-day check for Chat playground.

Exit 0 when ROBIE_PLAYGROUND is off. That is the default on Test and
Production.

Exit 1 when the flag is on and either:
- no AI provider is configured, or
- every one of the last N Chat jobs created today (UTC) failed.

The probe reads names only. It does not print secret values, and it does
not change jobs, writes, binding, payments, or email.

Run it on the gateway host so it sees that unit's environment:

    python3 scripts/check_robie_playground_health.py --unit hermes-gateway.service
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_JOB_DB = "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"
DEFAULT_LIMIT = 5
KEY_ENV_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "XAI_API_KEY",
    "GROQ_API_KEY",
    "ROBIE_GEMINI_API_KEY",
)
VERTEX_PROVIDERS = frozenset({"vertex", "google-vertex", "gcp"})
HERMES_HOME_CANDIDATES = (
    "/home/streetsmart-hermes/.hermes",
    "/opt/streetsmart-hermes/.hermes",
)


def playground_flag_on(env: dict[str, str]) -> bool:
    return str(env.get("ROBIE_PLAYGROUND") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _nonempty(env: dict[str, str], name: str) -> bool:
    return bool(str(env.get(name) or "").strip())


def _parse_env_file(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        found[name.strip()] = value.strip().strip("'\"")
    return found


def _config_model_provider(path: Path) -> str:
    """Return a provider name from config.yaml, or empty. Never returns a key."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    in_model = False
    model_indent = 0
    provider = ""
    saw_model_value = False
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        text = raw.strip()
        if not in_model:
            if text.startswith("model:"):
                in_model = True
                model_indent = indent
                rest = text.split(":", 1)[1].strip()
                if rest and rest not in {"|", ">", "|-", ">-"}:
                    saw_model_value = True
            continue
        if indent <= model_indent:
            break
        if text.startswith("provider:"):
            provider = text.split(":", 1)[1].strip().strip("'\"")
        elif text.startswith(("default:", "model:", "name:")):
            if text.split(":", 1)[1].strip():
                saw_model_value = True
    if provider:
        return provider
    return "named-model" if saw_model_value else ""


def hermes_homes(env: dict[str, str], explicit: str | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    homes = []
    named = str(env.get("HERMES_HOME") or "").strip()
    if named:
        homes.append(Path(named))
    for candidate in HERMES_HOME_CANDIDATES:
        path = Path(candidate)
        if path not in homes:
            homes.append(path)
    return homes


def provider_signals(
    env: dict[str, str],
    *,
    hermes_home: str | None = None,
) -> list[str]:
    """Names of configuration that can call a model. Values are not returned."""
    signals: list[str] = []
    for name in KEY_ENV_NAMES:
        if _nonempty(env, name):
            signals.append(name)
    secret = str(env.get("ROBIE_GEMINI_API_KEY_SECRET") or "").strip()
    if secret and "gemini-api-key" in secret:
        signals.append("ROBIE_GEMINI_API_KEY_SECRET")
    for home in hermes_homes(env, hermes_home):
        file_env = _parse_env_file(home / ".env")
        for name in KEY_ENV_NAMES:
            if str(file_env.get(name) or "").strip():
                label = f"{home}/.env:{name}"
                if label not in signals:
                    signals.append(label)
        provider = _config_model_provider(home / "config.yaml")
        if provider.lower() in VERTEX_PROVIDERS:
            label = f"{home}/config.yaml:vertex"
            if label not in signals:
                signals.append(label)
    return signals


def recent_chat_statuses(
    db_path: str,
    *,
    limit: int,
    day: str | None = None,
) -> list[str]:
    """Statuses of up to ``limit`` Chat jobs created on ``day`` (UTC)."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    today = day or datetime.now(timezone.utc).date().isoformat()
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "jobs" not in tables or "conversation_job_links" not in tables:
            return []
        rows = conn.execute(
            """
            SELECT jobs.status
            FROM jobs
            WHERE jobs.id IN (SELECT job_id FROM conversation_job_links)
              AND substr(jobs.created_at, 1, 10) = ?
            ORDER BY jobs.created_at DESC, jobs.id DESC
            LIMIT ?
            """,
            (today, limit),
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def read_unit_environ(unit: str) -> dict[str, str]:
    """Environment of a running systemd unit. Empty when it cannot be read."""
    show = subprocess.run(
        ["systemctl", "show", unit, "-p", "MainPID", "--value"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    pid = (show.stdout or "").strip()
    if not pid.isdigit() or pid == "0":
        raise RuntimeError(f"could not read MainPID for {unit}")
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    found: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        name, value = item.split(b"=", 1)
        found[name.decode(errors="replace")] = value.decode(errors="replace")
    return found


def evaluate(
    env: dict[str, str],
    *,
    db_path: str,
    limit: int = DEFAULT_LIMIT,
    hermes_home: str | None = None,
    day: str | None = None,
) -> tuple[int, str]:
    """Return (exit code, plain-English line). Secrets are not included."""
    if not playground_flag_on(env):
        return 0, "Playground is off. Nothing to check."
    signals = provider_signals(env, hermes_home=hermes_home)
    problems: list[str] = []
    if not signals:
        problems.append(
            "Playground is on, but no AI provider is configured."
        )
    try:
        statuses = recent_chat_statuses(db_path, limit=limit, day=day)
    except FileNotFoundError:
        problems.append(f"Chat job database was not found at {db_path}.")
        statuses = []
    except sqlite3.Error as exc:
        problems.append(f"Chat job database could not be read: {type(exc).__name__}.")
        statuses = []
    if statuses and all(status == "FAILED" for status in statuses):
        problems.append(
            f"The last {len(statuses)} Chat job"
            f"{'' if len(statuses) == 1 else 's'} from today all failed."
        )
    if problems:
        return 1, " ".join(problems)
    return (
        0,
        "Playground is on, an AI provider is configured, "
        "and today's Chat jobs are not all failed.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("ROBIE_JOB_DB", DEFAULT_JOB_DB))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--unit", default="", help="Read this systemd unit's environment.")
    parser.add_argument("--hermes-home", default="")
    parser.add_argument("--day", default="", help="UTC day YYYY-MM-DD. Default is today.")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.unit:
        try:
            env.update(read_unit_environ(args.unit))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"Could not read the gateway environment: {exc}", file=sys.stderr)
            return 2
    if args.limit < 1:
        print("limit must be at least 1", file=sys.stderr)
        return 2
    code, message = evaluate(
        env,
        db_path=args.db,
        limit=args.limit,
        hermes_home=args.hermes_home or None,
        day=args.day or None,
    )
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
