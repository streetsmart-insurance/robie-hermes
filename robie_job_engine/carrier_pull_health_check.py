"""Health check for the carrier document retrieval pulls.

Verifies the OUTCOME happened: each carrier produced today's expected output
(downloaded documents, confirmed zero-results, or a recorded hold) in its QA
pack directory. This is not a server-uptime check.

Per-carrier status:
  OK      - today's dated QA folder exists with output files (PDFs, manifest,
            or receipt showing downloads / zero-results)
  HELD    - today's run recorded holds with reasons (not a failure, but needs
            eyes on it)
  MISSING - no output for today; the pull did not run or produced nothing

When anything is MISSING, render_alert() composes a plain-English message
suitable for the ROBIE health Chat. When all carriers are OK or HELD, the
check stays quiet.

This module never pulls, never files to EZLynx, and never posts to Chat on
its own. The poster is an injectable dependency; wiring it to the real Chat
is a separate step (currently frozen).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any


def qa_root() -> Path:
    """Base directory for carrier QA packs."""
    override = str(os.environ.get("ROBIE_CARRIER_QA_ROOT") or "").strip()
    if override:
        return Path(override)
    return Path(
        "/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa"
    )


# Carrier key -> QA pack directory name under the qa root.
# Matches document_retrieval_service.py CARRIER_QA_ROOTS.
CARRIERS: dict[str, str] = {
    "guard": "guard",
    "progressive": "progressive",
    "progressive_bop": "progressive-bop",
    "geico": "geico",
    "travelers": "travelers",
    "natgen": "natgen",
    "uticafirst": "uticafirst",
    "farmersofsalem": "farmersofsalem",
}

# Friendly display names for alerts.
CARRIER_NAMES: dict[str, str] = {
    "guard": "Guard",
    "progressive": "Progressive (FAO)",
    "progressive_bop": "Progressive BOP",
    "geico": "GEICO",
    "travelers": "Travelers",
    "natgen": "NatGen",
    "uticafirst": "Utica First",
    "farmersofsalem": "Farmers of Salem",
}

# File names that count as run evidence (not just stray files).
RECEIPT_NAMES = ("manifest.json", "receipt.json", "run-receipt.json")

# A marker file a pull writes when it ran but found zero actionable documents.
ZERO_RESULTS_NAMES = ("zero-results.json", "no-documents.json", ".zero-results")


def _today_str(as_of: date | None = None) -> str:
    return (as_of or date.today()).isoformat()


def _find_dated_folder(carrier_root: Path, day: str) -> Path | None:
    """Locate today's output folder for a carrier.

    Checks both `{root}/{YYYY-MM-DD}/` and `{root}/*/{YYYY-MM-DD}/` to
    tolerate the per-module subfolder naming differences.
    """
    direct = carrier_root / day
    if direct.is_dir():
        return direct
    if carrier_root.is_dir():
        for child in sorted(carrier_root.iterdir()):
            if child.is_dir():
                nested = child / day
                if nested.is_dir():
                    return nested
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _folder_receipt(folder: Path) -> dict[str, Any] | None:
    for name in RECEIPT_NAMES:
        receipt = _read_json(folder / name)
        if receipt is not None:
            return receipt
    return None


def _has_zero_results_marker(folder: Path) -> bool:
    return any((folder / name).exists() for name in ZERO_RESULTS_NAMES)


def _pdf_files(folder: Path) -> list[Path]:
    return [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]


def check_carrier(
    carrier: str, *, root: str | Path = "", as_of: date | None = None
) -> dict[str, Any]:
    """Evaluate one carrier's output for today. Never pulls, never files."""
    base = Path(root) if root else qa_root()
    day = _today_str(as_of)
    carrier_dir = CARRIERS.get(carrier, carrier)
    carrier_root = base / carrier_dir
    result: dict[str, Any] = {
        "carrier": carrier,
        "name": CARRIER_NAMES.get(carrier, carrier),
        "day": day,
        "status": "MISSING",
    }

    folder = _find_dated_folder(carrier_root, day)
    if folder is None:
        result["reason"] = f"no QA pack folder for {day}"
        return result

    result["folder"] = str(folder)
    pdfs = _pdf_files(folder)
    receipt = _folder_receipt(folder)
    held: list[Any] = []
    downloaded = len(pdfs)
    if receipt is not None:
        raw_held = receipt.get("held")
        if isinstance(raw_held, list):
            held = raw_held
        raw_downloaded = receipt.get("downloaded")
        if isinstance(raw_downloaded, list):
            downloaded = max(downloaded, len(raw_downloaded))

    result["pdf_count"] = len(pdfs)
    result["downloaded"] = downloaded
    result["held_count"] = len(held)

    if held and not pdfs and downloaded == 0:
        result["status"] = "HELD"
        reasons = []
        for item in held:
            if isinstance(item, dict):
                reasons.append(str(item.get("reason") or item.get("outcome") or "held"))
            else:
                reasons.append(str(item))
        result["reason"] = "; ".join(reasons[:3])
        if len(reasons) > 3:
            result["reason"] += f" (+{len(reasons) - 3} more)"
        return result

    if pdfs or downloaded > 0 or _has_zero_results_marker(folder):
        result["status"] = "OK"
        if _has_zero_results_marker(folder) and not pdfs:
            result["reason"] = "ran with zero actionable documents"
        return result

    result["reason"] = f"QA pack folder exists for {day} but has no documents"
    return result


def probe(
    *,
    root: str | Path = "",
    as_of: date | None = None,
    carriers: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate all carriers. Never pulls, never files, never posts."""
    keys = list(carriers) if carriers else sorted(CARRIERS)
    checks = [check_carrier(c, root=root, as_of=as_of) for c in keys]
    missing = [c for c in checks if c["status"] == "MISSING"]
    held = [c for c in checks if c["status"] == "HELD"]
    ok = [c for c in checks if c["status"] == "OK"]
    green = not missing
    return {
        "green": green,
        "day": _today_str(as_of),
        "carriers": checks,
        "summary": {
            "ok": len(ok),
            "held": len(held),
            "missing": len(missing),
            "total": len(checks),
        },
    }


def render_alert(report: dict[str, Any]) -> str:
    """Plain-English alert for the ROBIE health Chat. No jargon."""
    missing = [c for c in report["carriers"] if c["status"] == "MISSING"]
    held = [c for c in report["carriers"] if c["status"] == "HELD"]
    lines = [
        f"Carrier document pull is missing output for {report['day']}.",
        "",
        "What's missing:",
    ]
    for c in missing:
        lines.append(f"- {c['name']}: {c.get('reason', 'no output recorded')}.")
    if held:
        lines.append("")
        lines.append("Held (needs a look, not a failure):")
        for c in held:
            lines.append(f"- {c['name']}: {c.get('reason', 'held')}.")
    lines.extend([
        "",
        "The pulls either didn't run or didn't save their output. "
        "Nothing was filed to EZLynx by this check.",
    ])
    return "\n".join(lines)


def render_recovery(report: dict[str, Any]) -> str:
    summary = report["summary"]
    return (
        f"Carrier document pull is healthy again for {report['day']}: "
        f"{summary['ok']} carrier(s) OK, "
        f"{summary['held']} held, "
        f"{summary['missing']} missing."
    )


def maybe_alert(
    report: dict[str, Any],
    *,
    poster: Any = None,
    state_dir: str | Path = "",
) -> dict[str, Any]:
    """Decide whether to alert. Never posts without an explicit poster.

    With poster=None (the default), this only reports what WOULD be sent.
    No timers, no Chat wiring here.
    """
    result: dict[str, Any] = {"alerted": False}
    if report["green"]:
        result["state"] = "green-quiet"
        return result
    result["state"] = "red"
    result["message"] = render_alert(report)
    if poster is not None:
        try:
            poster(result["message"])
            result["alerted"] = True
        except Exception as exc:
            result["alerted"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Health check for carrier document retrieval pulls."
    )
    parser.add_argument("--qa-root", default="")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--carrier", action="append", default=[])
    args = parser.parse_args(list(argv) if argv is not None else None)

    as_of = None
    if args.as_of.strip():
        as_of = date.fromisoformat(args.as_of.strip())

    report = probe(
        root=args.qa_root,
        as_of=as_of,
        carriers=args.carrier or None,
    )
    decision = maybe_alert(report)
    print(json.dumps({"probe": report, "alert": decision}, indent=2, default=str))
    return 0 if report["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
