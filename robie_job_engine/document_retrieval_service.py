"""Pull and file FAO, NatGen, and Geico for the previous business day.

Progressive BOP is not in this run. Filing still requires the kill switch
``ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1``. When that switch is off, this
records a hold and does not call a carrier portal or EZLynx.

The last-run file is JSON next to the job database. It records the exit
code and the filed, held, and duplicate counts.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping

from .business_calendar import previous_business_day
from .document_retrieval_filing import (
    PROD_CARRIER_RULES,
    eastern_today,
    file_carrier_batch,
    is_production_target,
    live_filing_decision,
    pack_filing_items,
)

CARRIERS = ("fao", "natgen", "geico", "travelers", "farmersofsalem", "guard", "uticafirst")
LAST_RUN_NAME = "document-retrieval-last-run.json"
BAD_SIGNALS = ("EZLYNX_WRITE_SCOPE_REFUSED", "document_filed_note_held")
OUTPUT_ROOTS = {
    "fao": Path("/opt/streetsmart-hermes/robie-job-engine/data/artifacts/carrier-pull-qa/progressive"),
    "natgen": Path("/opt/streetsmart-hermes/robie-job-engine/data/artifacts/carrier-pull-qa/natgen"),
    "geico": Path("/opt/streetsmart-hermes/robie-job-engine/data/artifacts/carrier-pull-qa/geico"),
    "travelers": Path("/opt/streetsmart-hermes/robie-job-engine/data/artifacts/carrier-pull-qa/travelers"),
    "farmersofsalem": Path("/opt/streetsmart-hermes/robie-job-engine/data/artifacts/carrier-pull-qa/farmersofsalem"),
    "guard": Path("/opt/streetsmart-hermes/robie-job-engine/data/artifacts/carrier-pull-qa/guard"),
    "uticafirst": Path("/opt/streetsmart-hermes/robie-job-engine/data/artifacts/carrier-pull-qa/uticafirst"),
}
_HELD_STATUSES = frozenset({
    "held",
    "document_filed_note_held",
    "document_filed_task_held",
    "filed_sheet_held",
})
_FILED_STATUSES = frozenset({"filed", "filed_no_workflow"})


def state_dir() -> Path:
    override = str(os.environ.get("ROBIE_DOCUMENT_RETRIEVAL_STATE_DIR") or "").strip()
    if override:
        return Path(override)
    db = str(os.environ.get("ROBIE_JOB_DB") or "").strip()
    if db:
        return Path(db).expanduser().resolve().parent
    return Path("/opt/streetsmart-hermes/robie-job-engine/data")


def carrier_argv(day: date) -> dict[str, list[str]]:
    """Command arguments for the seven Production pulls. BOP is absent."""

    iso = day.isoformat()
    return {
        "fao": [
            "--start", iso, "--end", iso, "--as-of", iso,
            "--output", str(OUTPUT_ROOTS["fao"]),
        ],
        "natgen": ["--start", iso, "--end", iso, "--output", str(OUTPUT_ROOTS["natgen"])],
        "geico": ["--as-of", iso, "--output-root", str(OUTPUT_ROOTS["geico"])],
        "travelers": ["--as-of", iso, "--output-root", str(OUTPUT_ROOTS["travelers"])],
        "farmersofsalem": ["--as-of", iso, "--output-root", str(OUTPUT_ROOTS["farmersofsalem"])],
        "guard": ["--as-of", iso, "--output-root", str(OUTPUT_ROOTS["guard"])],
        "uticafirst": ["--as-of", iso, "--qa-root", str(OUTPUT_ROOTS["uticafirst"])],
    }


def _signals_in(text: str) -> list[str]:
    found = [name for name in BAD_SIGNALS if name in text]
    return found


def tally_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Count filed, held, and duplicate rows. Pull a filing block out when present."""

    body = dict(payload or {})
    filing = body.get("filing") if isinstance(body.get("filing"), dict) else body
    results = filing.get("results") if isinstance(filing, dict) else None
    rows = [row for row in (results or []) if isinstance(row, dict)]
    filed = held = duplicate = 0
    signals: list[str] = []
    for row in rows:
        status = str(row.get("status") or "")
        reason = str(row.get("reason") or "")
        if status in _FILED_STATUSES:
            filed += 1
        elif status == "skipped_duplicate":
            duplicate += 1
        elif status in _HELD_STATUSES:
            held += 1
        signals.extend(_signals_in(status))
        signals.extend(_signals_in(reason))
    status = str((filing or {}).get("status") or body.get("status") or "")
    reason = str((filing or {}).get("reason") or body.get("reason") or "")
    if status in {"disabled", "held", "HELD"} and not rows:
        held += 1
    signals.extend(_signals_in(status))
    signals.extend(_signals_in(reason))
    ordered = []
    for name in signals:
        if name not in ordered:
            ordered.append(name)
    return {"filed": filed, "held": held, "duplicate": duplicate, "signals": ordered}


def _invoke(main: Callable[..., int], argv: list[str]) -> dict[str, Any]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = int(main(argv))
    text = buffer.getvalue().strip()
    payload: dict[str, Any]
    try:
        parsed = json.loads(text) if text else {}
        payload = parsed if isinstance(parsed, dict) else {"status": "HELD", "reason": "pull did not return JSON"}
    except json.JSONDecodeError:
        payload = {"status": "HELD", "reason": "pull did not return JSON"}
    payload["_exit_code"] = code
    return payload


def _file_output(carrier: str, output: Path) -> dict[str, Any]:
    return file_carrier_batch(pack_filing_items(output), rule=PROD_CARRIER_RULES[carrier])


def default_runners(day: date) -> dict[str, Callable[[], dict[str, Any]]]:
    """Live pulls. FAO files inside its own command. NatGen and Geico file after."""

    commands = carrier_argv(day)

    def fao() -> dict[str, Any]:
        from .progressive_fao_memo import main as fao_main

        return _invoke(fao_main, commands["fao"])

    def natgen() -> dict[str, Any]:
        from .natgen_pending_cancellation import main as natgen_main

        pulled = _invoke(natgen_main, commands["natgen"])
        if int(pulled.get("_exit_code") or 0) != 0:
            return pulled
        filing = _file_output("natgen", OUTPUT_ROOTS["natgen"])
        pulled["filing"] = filing
        if str(filing.get("status") or "") not in {"filed", "filed_no_workflow", "skipped_duplicate", "empty"}:
            pulled["_exit_code"] = 1
        return pulled

    def geico() -> dict[str, Any]:
        from .geico_pending_cancellation_noc import main as geico_main

        pulled = _invoke(geico_main, commands["geico"])
        if int(pulled.get("_exit_code") or 0) != 0:
            return pulled
        filing = _file_output("geico", OUTPUT_ROOTS["geico"])
        pulled["filing"] = filing
        if str(filing.get("status") or "") not in {"filed", "filed_no_workflow", "skipped_duplicate", "empty"}:
            pulled["_exit_code"] = 1
        return pulled

    def _filed_runner(carrier: str, module: str) -> Callable[[], dict[str, Any]]:
        """Pull then file, for carriers that file after the pull (like NatGen/Geico)."""

        def run() -> dict[str, Any]:
            mod = __import__(f"robie_job_engine.{module}", fromlist=["main"])
            pulled = _invoke(mod.main, commands[carrier])
            if int(pulled.get("_exit_code") or 0) != 0:
                return pulled
            filing = _file_output(carrier, OUTPUT_ROOTS[carrier])
            pulled["filing"] = filing
            if str(filing.get("status") or "") not in {"filed", "filed_no_workflow", "skipped_duplicate", "empty"}:
                pulled["_exit_code"] = 1
            return pulled

        return run

    return {
        "fao": fao,
        "natgen": natgen,
        "geico": geico,
        "travelers": _filed_runner("travelers", "travelers_pending_cancellation"),
        "farmersofsalem": _filed_runner("farmersofsalem", "farmersofsalem_pending_cancellation"),
        "guard": _filed_runner("guard", "guard_pending_cancellation"),
        "uticafirst": _filed_runner("uticafirst", "uticafirst_pending_cancellation"),
    }


def run_retrieval(
    *,
    business_day: date | None = None,
    directory: str | Path | None = None,
    runners: Mapping[str, Callable[[], dict[str, Any]]] | None = None,
    environ: Mapping[str, str] | None = None,
    hostname: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Run the three carriers, or hold when the Production gate is closed."""

    env = os.environ if environ is None else environ
    host = socket.gethostname() if hostname is None else hostname
    day = business_day or previous_business_day(today or eastern_today())
    root = Path(directory) if directory else state_dir()
    calls = runners if runners is not None else default_runners(day)
    carrier_rows: list[dict[str, Any]] = []
    for name in CARRIERS:
        decision = live_filing_decision(env, host, name)
        if not decision.allowed:
            carrier_rows.append({
                "carrier": name,
                "exit_code": 1,
                "filed": 0,
                "held": 1,
                "duplicate": 0,
                "signals": [],
                "reason": decision.reason,
            })
            continue
        try:
            payload = calls[name]()
        except Exception as exc:
            carrier_rows.append({
                "carrier": name,
                "exit_code": 1,
                "filed": 0,
                "held": 1,
                "duplicate": 0,
                "signals": [],
                "reason": f"{name} pull failed ({type(exc).__name__})",
            })
            continue
        counts = tally_payload(payload)
        code = int(payload.get("_exit_code") or 0)
        if counts["held"] or counts["signals"]:
            code = 1 if code == 0 else code
        carrier_rows.append({
            "carrier": name,
            "exit_code": code,
            "filed": counts["filed"],
            "held": counts["held"],
            "duplicate": counts["duplicate"],
            "signals": counts["signals"],
            "reason": str(payload.get("reason") or ""),
        })
    filed = sum(row["filed"] for row in carrier_rows)
    held = sum(row["held"] for row in carrier_rows)
    duplicate = sum(row["duplicate"] for row in carrier_rows)
    signals: list[str] = []
    for row in carrier_rows:
        for name in row["signals"]:
            if name not in signals:
                signals.append(name)
    exit_code = 0 if held == 0 and not signals and all(row["exit_code"] == 0 for row in carrier_rows) else 1
    record = {
        "run_at": _utc_now(),
        "business_day": day.isoformat(),
        "exit_code": exit_code,
        "filed": filed,
        "held": held,
        "duplicate": duplicate,
        "signals": signals,
        "carriers": carrier_rows,
        "production": is_production_target(env, host),
    }
    _write_last_run(root, record)
    return record


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_last_run(directory: Path, record: Mapping[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LAST_RUN_NAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull and file FAO, NatGen, and Geico.")
    parser.add_argument("--business-day", default="", help="YYYY-MM-DD. Default: previous business day.")
    parser.add_argument("--state-dir", default="", help="Where the last-run file is written.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    day = date.fromisoformat(args.business_day) if args.business_day else None
    record = run_retrieval(
        business_day=day,
        directory=args.state_dir or None,
    )
    print(json.dumps(record, indent=2, sort_keys=True))
    return int(record["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
