"""Alert when Production remains on the EZLynx login page for 15 minutes."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from .session_preflight import DEFAULT_CDP_URL, LOGGED_OUT, check


DEFAULT_STATE_PATH = Path(
    "/opt/streetsmart-hermes/robie-job-engine/data/ezlynx-login-alert.json"
)
DEFAULT_THRESHOLD_SECONDS = 15 * 60


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def post_webhook(text: str) -> None:
    webhook = os.environ.get("ROBIE_GOOGLE_CHAT_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("ROBIE_GOOGLE_CHAT_WEBHOOK_URL is not configured")
    body = json.dumps({"text": text}).encode("utf-8")
    request = Request(
        webhook,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=UTF-8"},
    )
    with urlopen(request, timeout=15) as response:
        if not 200 <= int(response.status) < 300:
            raise RuntimeError(f"Google Chat webhook returned HTTP {response.status}")


def evaluate(
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
    now: datetime | None = None,
    checker: Callable[[str], dict[str, Any]] = check,
    poster: Callable[[str], None] = post_webhook,
    cdp_url: str = DEFAULT_CDP_URL,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report = checker(cdp_url)
    state = _read_state(state_path)
    if report.get("state") != LOGGED_OUT:
        if state_path.exists():
            state_path.unlink()
        return {"status": "CLEAR", "session_state": report.get("state")}

    first_seen_raw = str(state.get("first_seen") or "")
    try:
        first_seen = datetime.fromisoformat(first_seen_raw.replace("Z", "+00:00"))
    except ValueError:
        first_seen = current
        state = {"first_seen": current.isoformat(), "alerted": False}
        _write_state(state_path, state)
    elapsed = max(0, int((current - first_seen).total_seconds()))
    if elapsed < threshold_seconds:
        return {"status": "PENDING", "logged_out_seconds": elapsed}
    if state.get("alerted"):
        return {"status": "ALREADY_ALERTED", "logged_out_seconds": elapsed}

    poster(
        "ROBIE Production alert: EZLynx has remained on the login page for "
        f"{elapsed // 60} minutes. Browser work is blocked. Check the shared "
        "driver lease before re-authenticating SSRobie."
    )
    state["alerted"] = True
    state["alerted_at"] = current.isoformat()
    _write_state(state_path, state)
    return {"status": "ALERTED", "logged_out_seconds": elapsed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--threshold-seconds", type=int, default=DEFAULT_THRESHOLD_SECONDS)
    parser.add_argument("--cdp-url", default=os.environ.get("ROBIE_BROWSER_CDP_URL", DEFAULT_CDP_URL))
    args = parser.parse_args(argv)
    try:
        result = evaluate(
            state_path=args.state_path,
            threshold_seconds=args.threshold_seconds,
            cdp_url=args.cdp_url,
        )
    except Exception as exc:  # noqa: BLE001 - timer must report its own failure
        print(json.dumps({"status": "MONITOR_ERROR", "error": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
