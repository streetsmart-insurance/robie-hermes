"""Daily carrier document pull on Test (scheduled; dry run, EZLynx filing off).

Run by ``robie-carrier-pull-test.timer`` on weekdays at 07:30 America/New_York
from its own staged release folder (``/var/lib/robie-carrier-pull-test``). It
never reads or switches Test's ``current`` link.

For each carrier, one at a time:

1. close stale tabs on the carrier Chrome (PDF viewers, blank tabs, the BOP
   app window, error pages) so each carrier's tab selector finds exactly one
   portal tab;
2. run that carrier's pull in its own subprocess with its own time limit, so
   a crash or a hang in one carrier never stops the others;
3. with ``--upload-drive``, copy the new PDFs to
   ``Robie Carrier Pull QA (Nicole)/<Carrier>/<pull date>/`` through the
   carrier Drive ledger (each notice is uploaded once).

Guard stays off the daily timer until a person confirms the account is not
locked after the 2026-10-08 rejection. Travelers signs itself in. A plain-English
summary is written to ``<root>/runs/<date>/summary.txt`` (and JSON). With
``--notify`` and ``ROBIE_HEALTH_CHAT_SPACE`` set, the summary is also posted to
the ROBIE health Chat as the Robie Chat app; neither is set on Test today.

Safety gates (checked before anything runs): ROBIE_ENV=TEST, Production hosts
refused, ``ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX`` forced to 0 for this process
and every child, CDP limited to the local carrier Chrome. No email is sent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .carrier_dry_run import KILL_SWITCH_ENV, SPECS, _require_dry_run_environment

EASTERN = ZoneInfo("America/New_York")
DEFAULT_ROOT = "/var/lib/robie-carrier-pull-test"
CARRIER_CDP_PORT = 9223
DEFAULT_CDP_URL = f"http://127.0.0.1:{CARRIER_CDP_PORT}"

DAILY_CARRIERS = (
    "progressive",
    "progressive_bop",
    "geico",
    "travelers",
    "natgen",
    "uticafirst",
    "farmersofsalem",
)
SKIPPED_UNTIL_LOGIN_FIXED = {
    "guard": (
        "Guard login was rejected on 2026-10-08. The daily timer stays off "
        "until the account is confirmed unlocked. A manual pull still makes one attempt."
    ),
}
DISPLAY = {name: spec.display for name, spec in SPECS.items()}
DISPLAY["progressive_bop"] = "Progressive BOP"
CARRIER_TIMEOUT_S = {"progressive": 2400, "progressive_bop": 900}
DEFAULT_TIMEOUT_S = 1800

# Tabs that are never a carrier's portal tab and confuse the tab selectors.
_STALE_HOSTS = ("bop.americanstrategic.com",)
_STALE_URL_PARTS = ("pdfhandler.ashx", "/filemanager/filemanager/getfile/")
_STALE_SCHEMES = ("blob:", "chrome-error:", "devtools:")


def eastern_today() -> date:
    return datetime.now(EASTERN).date()


def _require_local_cdp(url: str) -> str:
    from .intake_core import IntakeHold

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        raise IntakeHold("Daily carrier run must use the local Test CDP endpoint")
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost"}:
        raise IntakeHold("Daily carrier run must use the local Test CDP endpoint")
    # Only the carrier Chrome. Port 9222 is the EZLynx Chrome: never attach
    # to it (live 2026-10-08: an env file's ROBIE_BROWSER_CDP_URL pointed the
    # first service run at 9222; it was stopped within 20 seconds).
    if parsed.port != CARRIER_CDP_PORT:
        raise IntakeHold(f"Daily carrier run only attaches to the carrier Chrome on port {CARRIER_CDP_PORT}")
    return url.rstrip("/")


def is_stale_tab(url: str) -> bool:
    low = (url or "").strip().lower()
    if low in {"", "about:blank"} or low.startswith(_STALE_SCHEMES):
        return True
    parsed = urllib.parse.urlsplit(low)
    if (parsed.hostname or "") in _STALE_HOSTS:
        return True
    if any(part in low for part in _STALE_URL_PARTS):
        return True
    return parsed.path.endswith(".pdf")


def _http_json(url: str, *, method: str = "GET") -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - loopback only
        body = response.read()
    try:
        return json.loads(body or b"null")
    except ValueError:
        return None


def close_stale_tabs(cdp_url: str, *, http: Callable[..., Any] = _http_json) -> list[str]:
    """Close stale page tabs on the local carrier Chrome. Keeps one page open."""
    base = _require_local_cdp(cdp_url)
    targets = http(f"{base}/json/list") or []
    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    stale = [t for t in pages if is_stale_tab(str(t.get("url") or ""))]
    if stale and len(stale) == len(pages):
        stale = stale[1:]  # never close the last page
    closed = []
    for target in stale:
        target_id = str(target.get("id") or "")
        if not target_id or not target_id.replace("-", "").isalnum():
            continue
        try:
            http(f"{base}/json/close/{target_id}")
        except Exception:  # noqa: BLE001 - a tab that is already gone is fine
            continue
        closed.append(str(target.get("url") or "")[:80])
    return closed


# Carriers whose pull expects an already-open signed-in tab. When none exists
# (or the session expired), the runner signs in through the carrier login
# module. Credentials stay in Secret Manager; NatGen reads its email code the
# same way Utica does. Nothing is posted to clients.
CARRIER_SIGN_IN = {
    "geico": "robie_job_engine.geico_login.ensure_geico_tab",
    "natgen": "robie_job_engine.natgen_login.ensure_natgen_tab",
    "progressive": "robie_job_engine.progressive_login.ensure_progressive_tab",
    "progressive_bop": "robie_job_engine.progressive_login.ensure_progressive_tab",
    "travelers": "robie_job_engine.travelers_login.ensure_travelers_tab",
    "farmersofsalem": "robie_job_engine.farmersofsalem_login.ensure_farmers_tab",
}


def _import_ensure(path: str) -> Callable[[str], str]:
    module_name, attr = path.rsplit(".", 1)
    import importlib

    return getattr(importlib.import_module(module_name), attr)


def ensure_carrier_tab(
    name: str,
    cdp_url: str,
    *,
    http: Callable[..., Any] = _http_json,
    sleep: Callable[[float], None] | None = None,
    sign_in: Callable[[str], str] | None = None,
) -> str | None:
    """Sign in (or reopen) the carrier portal tab when this carrier needs one.

    Returns the portal URL after sign-in, or None when the carrier does not
    need a pre-opened tab. ``sign_in`` is injectable for tests.
    """
    path = CARRIER_SIGN_IN.get(name)
    if not path:
        return None
    base = _require_local_cdp(cdp_url)
    ensure = sign_in if sign_in is not None else _import_ensure(path)
    return ensure(base)


def parse_json_tail(stdout: str) -> dict[str, Any] | None:
    """The last top-level JSON object a pull CLI printed (``{`` at column 0)."""
    lines = (stdout or "").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index] == "{":
            try:
                value = json.loads("\n".join(lines[index:]))
            except ValueError:
                continue
            return value if isinstance(value, dict) else None
    return None


def carrier_command(name: str, *, day: date, root: Path, cdp_url: str, python: str) -> list[str]:
    out = str(root / "packs" / name)
    if name == "progressive_bop":
        return [python, "-u", "-m", "robie_job_engine.progressive_bop", "--report-date", day.isoformat(),
                "--output-root", out, "--cdp-url", cdp_url]
    if name not in SPECS:
        raise ValueError(f"unknown carrier {name!r}")
    return [python, "-u", "-m", "robie_job_engine.carrier_dry_run", "--carriers", name, "--as-of", day.isoformat(),
            "--output-root", out, "--cdp-url", cdp_url]


def _reason(entry: Any) -> str:
    from .carrier_dry_run import hold_reason

    return hold_reason(entry)


def normalize_result(name: str, payload: dict[str, Any] | None, *, returncode: int, stderr: str) -> dict[str, Any]:
    display = DISPLAY.get(name, name)
    if payload is None:
        tail = (stderr or "").strip().splitlines()[-1:] or [f"exit {returncode}"]
        return {"display": display, "status": "FAILED", "downloaded": 0, "held": [], "error": tail[0][:300]}
    if name == "progressive_bop":
        status = str(payload.get("status") or "")
        rows = [o for o in payload.get("policies", []) or [] if isinstance(o, dict)]
        held = [f"{o.get('policy_number')}: {o.get('reason') or 'held'}" for o in rows if o.get("disposition") == "held"]
        pulled = sum(1 for o in rows if o.get("disposition") == "pulled")
        if status in {"PULLED", "EMPTY"} or (status == "HELD" and rows):
            # Per-policy holds are listed; the pull itself ran.
            return {"display": display, "status": "OK", "downloaded": pulled, "held": held,
                    "note": "no policies on today's report" if status == "EMPTY" else "", "error": None}
        return {"display": display, "status": "HELD", "downloaded": pulled, "held": held,
                "reason": str(payload.get("reason") or "held"), "error": None}
    result = (payload.get("carriers") or {}).get(name)
    if not isinstance(result, dict):
        return {"display": display, "status": "FAILED", "downloaded": 0, "held": [], "error": "no carrier result"}
    out = {
        "display": display,
        "status": result.get("status") or "FAILED",
        "downloaded": int(result.get("downloaded") or 0),
        "held": [_reason(h) for h in result.get("held") or []],
        "error": result.get("error"),
        "pack": result.get("pack"),
    }
    if result.get("reason"):
        out["reason"] = result["reason"]
    if result.get("unprocessed") is not None:
        out["unprocessed"] = result["unprocessed"]
    return out


def run_carrier(
    name: str,
    *,
    day: date,
    root: Path,
    cdp_url: str,
    python: str = sys.executable,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Run one carrier in a subprocess. Never raises."""
    timeout = CARRIER_TIMEOUT_S.get(name, DEFAULT_TIMEOUT_S)
    env = dict(os.environ)
    env[KILL_SWITCH_ENV] = "0"
    env["ROBIE_ENV"] = "TEST"
    env["PYTHONUNBUFFERED"] = "1"
    env["ROBIE_BROWSER_CDP_URL"] = _require_local_cdp(cdp_url)
    log_dir = root / "runs" / day.isoformat()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    try:
        cmd = carrier_command(name, day=day, root=root, cdp_url=cdp_url, python=python)
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        log_path.write_text((exc.stdout or "") + "\n--- stderr ---\n" + (exc.stderr or ""))
        return {"display": DISPLAY.get(name, name), "status": "FAILED", "downloaded": 0, "held": [],
                "error": f"timed out after {timeout // 60} minutes"}
    except Exception as exc:  # noqa: BLE001 - one carrier never stops the run
        return {"display": DISPLAY.get(name, name), "status": "FAILED", "downloaded": 0, "held": [],
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    log_path.write_text((proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""))
    return normalize_result(name, parse_json_tail(proc.stdout or ""), returncode=proc.returncode, stderr=proc.stderr or "")


def upload_carrier(name: str, *, day: date, root: Path, drive: Any) -> dict[str, Any]:
    """Drive upload for one carrier's pack. Never raises."""
    from .carrier_qa_drive import CarrierDriveUpload

    try:
        carrier_root = root / "packs" / name
        result = CarrierDriveUpload(drive, name, carrier_root).upload_pack(carrier_root / day.isoformat(), day.isoformat())
        return {"status": "OK", **result.as_dict()}
    except Exception as exc:  # noqa: BLE001 - Drive trouble never stops the run
        return {"status": "HELD", "reason": f"{type(exc).__name__}: {exc}"[:300]}


_TECHNICAL = re.compile(r"[\[\]{}<>#=$]|\b[a-z]+_[a-z_]+\b|[A-Z][a-z]+(?:Error|Exception|Expired)\b|https?://|\.py\b")


def plain(text: Any, fallback: str) -> str:
    """One plain-English sentence for the health Chat (no field names or code)."""
    text = " ".join(str(text or "").split())
    if not text:
        return fallback
    if "timed out" in text.lower() or "timeout" in text.lower():
        return "did not finish in time"
    if re.fullmatch(r"exit -?\d+", text):
        return "stopped before it finished"
    if _TECHNICAL.search(text):
        return fallback
    return text.rstrip(".")[:160]


def render_summary(summary: dict[str, Any]) -> str:
    day = summary["as_of"]
    lines = [f"Robie carrier pull for {day} (Test; nothing filed to EZLynx, no emails sent)."]
    for name, r in summary["carriers"].items():
        if r["status"] == "SKIPPED":
            lines.append(f"- {r['display']}: skipped. {r['reason']}.")
            continue
        if r["status"] == "OK":
            text = f"- {r['display']}: {r['downloaded']} new PDF{'s' if r['downloaded'] != 1 else ''}"
            if r.get("note"):
                text += f" ({r['note']})"
            if r["held"]:
                text += f", {len(r['held'])} held"
        elif r["status"] == "PARTIAL":
            left = r.get("unprocessed")
            extra = f", {left} policies left" if left is not None else ""
            text = (
                f"- {r['display']}: partial. {r['downloaded']} downloaded{extra}. "
                f"{plain(r.get('reason'), 'the pull stopped early')}"
            )
        elif r["status"] == "HELD":
            text = f"- {r['display']}: held. {plain(r.get('reason'), 'a carrier page did not look as expected')}"
        else:
            text = f"- {r['display']}: failed, {plain(r.get('error'), 'stopped with an error (details are in the Test log)')}"
        drive = r.get("drive")
        if drive:
            if drive.get("status") == "OK":
                text += f"; Drive: {len(drive['uploaded'])} uploaded, {len(drive['skipped'])} already there"
                if drive["held"]:
                    text += f", {len(drive['held'])} not uploaded"
            else:
                text += f"; not uploaded to Drive: {plain(drive.get('reason'), 'Drive was not reachable')}"
        lines.append(text + ".")
        reasons: dict[str, int] = {}
        for reason in r["held"]:
            reason = plain(reason, "a carrier page did not look as expected")
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"    {count} held: {reason}")
    t = summary["totals"]
    lines.append(
        f"Total: {t['pdfs']} new PDFs, {t['uploaded']} uploaded to Drive, "
        f"{t['failed']} carrier(s) failed, {t.get('partial', 0)} partial."
    )
    return "\n".join(lines)


def notify(text: str) -> str:
    """Post to the ROBIE health Chat when configured. Returns a status line."""
    space = os.environ.get("ROBIE_HEALTH_CHAT_SPACE", "").strip()
    if not space:
        return "not posted: ROBIE_HEALTH_CHAT_SPACE is not set"
    try:
        from .chat_app_post import post_as_chat_app

        post_as_chat_app(space, text)
        return f"posted to {space}"
    except Exception as exc:  # noqa: BLE001
        return f"not posted: {type(exc).__name__}"


def run_daily(
    *,
    day: date,
    root: Path,
    carriers: tuple[str, ...] = DAILY_CARRIERS,
    cdp_url: str = DEFAULT_CDP_URL,
    upload_drive: bool = False,
    do_notify: bool = False,
    run_one: Callable[..., dict[str, Any]] = run_carrier,
    close_tabs: Callable[[str], list[str]] = close_stale_tabs,
    open_tab: Callable[[str, str], str | None] = ensure_carrier_tab,
    drive_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    from .intake_core import IntakeHold

    _require_dry_run_environment()
    os.environ[KILL_SWITCH_ENV] = "0"
    cdp_url = _require_local_cdp(cdp_url)
    drive = None
    drive_error = None
    if upload_drive:
        try:
            if drive_factory is None:
                from .carrier_qa_drive import build_drive_service as drive_factory  # noqa: N811
            drive = drive_factory()
        except Exception as exc:  # noqa: BLE001 - pulls still run without Drive
            drive_error = f"{type(exc).__name__}: {exc}"[:300]
    results: dict[str, dict[str, Any]] = {}
    for name in carriers:
        if name in SKIPPED_UNTIL_LOGIN_FIXED:
            results[name] = {"display": DISPLAY.get(name, name), "status": "SKIPPED",
                             "reason": SKIPPED_UNTIL_LOGIN_FIXED[name], "downloaded": 0, "held": []}
            continue
        try:
            closed = close_tabs(cdp_url)
        except Exception as exc:  # noqa: BLE001
            closed = [f"(tab cleanup failed: {type(exc).__name__})"]
        try:
            opened = open_tab(name, cdp_url)
        except IntakeHold as exc:
            # The sign-in already submitted a password. Do not start the pull,
            # which would submit it again.
            results[name] = {
                "display": DISPLAY.get(name, name),
                "status": "HELD",
                "reason": str(exc),
                "downloaded": 0,
                "held": [],
            }
            continue
        except Exception as exc:  # noqa: BLE001 - the pull's own tab check decides
            opened = f"(could not open a tab: {type(exc).__name__})"
        result = run_one(name, day=day, root=root, cdp_url=cdp_url)
        result["closed_tabs"] = closed
        if opened:
            result["opened_tab"] = opened
        if result.get("status") == "HELD" and "not authenticated" in str(result.get("reason") or "").lower():
            result["reason"] = f"{result['display']} sign-in did not leave a usable session"
        if upload_drive:
            if drive is None:
                result["drive"] = {"status": "HELD", "reason": drive_error or "Drive is not available"}
            else:
                result["drive"] = upload_carrier(name, day=day, root=root, drive=drive)
        results[name] = result
    try:
        close_tabs(cdp_url)
    except Exception:  # noqa: BLE001
        pass
    totals = {
        "pdfs": sum(int(r.get("downloaded") or 0) for r in results.values()),
        "uploaded": sum(len((r.get("drive") or {}).get("uploaded") or []) for r in results.values()),
        "failed": sum(1 for r in results.values() if r["status"] == "FAILED"),
        "partial": sum(1 for r in results.values() if r["status"] == "PARTIAL"),
    }
    summary = {"as_of": day.isoformat(), "carriers": results, "totals": totals}
    text = render_summary(summary)
    summary["notify"] = notify(text) if do_notify else "not requested"
    out = root / "runs" / day.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.txt").write_text(text + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    summary["text"] = text
    return summary


def _carrier_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DAILY_CARRIERS + tuple(SKIPPED_UNTIL_LOGIN_FIXED)
    names = tuple(n.strip().lower() for n in raw.split(",") if n.strip())
    unknown = [n for n in names if n not in DISPLAY]
    if unknown:
        raise SystemExit(f"Unknown carrier(s): {', '.join(unknown)}")
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daily carrier pull on Test (dry run; EZLynx filing off)")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="state root: packs/, runs/")
    parser.add_argument("--as-of", default=None, help="pull date YYYY-MM-DD (default: today, Eastern)")
    parser.add_argument("--carriers", default=None, help="comma list (default: the daily set; Guard/Travelers are skipped)")
    # Deliberately not read from ROBIE_BROWSER_CDP_URL: Test env files point
    # that at the EZLynx Chrome (9222).
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL, help="carrier Chrome CDP (port 9223 only)")
    parser.add_argument("--upload-drive", action="store_true", help="upload new PDFs to the carrier QA Drive folders")
    parser.add_argument("--notify", action="store_true", help="post the summary to ROBIE_HEALTH_CHAT_SPACE")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    day = date.fromisoformat(args.as_of) if args.as_of else eastern_today()
    root = Path(args.root)
    try:
        summary = run_daily(
            day=day, root=root, carriers=_carrier_list(args.carriers), cdp_url=args.cdp_url,
            upload_drive=args.upload_drive, do_notify=args.notify,
        )
    except Exception as exc:  # noqa: BLE001 - environment gate failures
        print(f"Daily carrier run refused: {exc}", file=sys.stderr)
        return 2
    print(summary.pop("text"))
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
