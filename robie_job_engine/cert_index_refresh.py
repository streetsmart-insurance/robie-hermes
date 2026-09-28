"""Governed refresh of the certificate applicant index CSV.

The sweep matches requests against the full-book applicant export
(``CERT_APPLICANT_INDEX_PATH``). That export is dated and is known to
omit rows that exist in EZLynx (Laney Express, 2026-09-27), and there is
no EZLynx applicant-by-name API to fall back on. This script rebuilds
the CSV from a fresh full-book export with sanity gates:

1. Validate the export headers (Account Name + Applicant ID required).
2. Sanity-gate the row count: refuse ``--apply`` when the new export
   carries fewer than ``CERT_INDEX_MIN_ROW_RATIO`` (default 0.8) of the
   live CSV's applicant IDs — a shrunken export must never silently
   replace the directory (fail closed).
3. Diff old vs new (added/removed account names) for the operator log.
4. Report miss coverage: which names in the ``cert_index_misses``
   ledger (current NO_MATCH requests) the fresh export now resolves.
5. ``--apply``: atomic replace of the live CSV with a timestamped
   backup. Without ``--apply`` this is a dry run — nothing is written.

Usage (on hermes-poc-01, where the live CSV lives)::

    /opt/streetsmart-hermes/venv/bin/python -m robie_job_engine.cert_index_refresh \\
        --source /path/to/full-book-export.xlsx \\
        --live-csv /opt/streetsmart-hermes/robie-job-engine/data/cert-applicant-index.csv \\
        --apply

The export itself comes from the phone-accountability full-book report
(the pipeline that produced the current CSV); this script governs what
happens after the export lands on the box, not how it is produced.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

#: Fail-closed floor: the new export must carry at least this fraction of
#: the live CSV's applicant IDs or --apply is refused.
DEFAULT_MIN_ROW_RATIO = 0.8

#: Columns written to the live CSV (the set load_applicant_index reads,
#: plus dba for operators).
OUT_COLUMNS = ["account_name", "applicant_id", "dba", "email_primary",
               "email_business", "phone_cell", "phone_home", "phone_work"]

#: Header normalization: workbook ("Account Name") and snake_case CSV
#: ("account_name") headers both map onto OUT_COLUMNS.
_HEADER_ALIASES = {
    "accountname": "account_name",
    "applicantid": "applicant_id",
    "dba": "dba",
    "emailprimary": "email_primary",
    "email-primary": "email_primary",
    "emailbusiness": "email_business",
    "email-business": "email_business",
    "phonecell": "phone_cell",
    "phone-cell": "phone_cell",
    "phonehome": "phone_home",
    "phone-home": "phone_home",
    "phonework": "phone_work",
    "phone-work": "phone_work",
    "phonebusiness": "phone_work",
    "phone-business": "phone_work",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _norm_header(header: str) -> str:
    key = "".join(ch for ch in (header or "").strip().lower()
                  if ch.isalnum() or ch == "-")
    return _HEADER_ALIASES.get(key, "")


def _read_workbook_rows(path: str) -> list[dict[str, str]]:
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError(
            "openpyxl is required to read .xlsx exports") from exc
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    headers = [str(c.value or "") for c in
               next(ws.iter_rows(min_row=1, max_row=1))]
    mapping = {i: _norm_header(h) for i, h in enumerate(headers)
               if _norm_header(h)}
    if "account_name" not in mapping.values():
        raise RuntimeError(f"export headers unusable (need Account Name): "
                           f"{headers}")
    if "applicant_id" not in mapping.values():
        raise RuntimeError(f"export headers unusable (need Applicant ID): "
                           f"{headers}")
    rows: list[dict[str, str]] = []
    for values in ws.iter_rows(min_row=2, values_only=True):
        row = {col: ("" if values[i] is None else str(values[i]).strip())
               for i, col in mapping.items()}
        rows.append(row)
    return rows


def _read_csv_rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        mapping = {h: _norm_header(h) for h in (reader.fieldnames or [])}
        rows = []
        for raw in reader:
            rows.append({col: (raw[h] or "").strip()
                         for h, col in mapping.items() if col})
    if not any("account_name" in r for r in rows) and rows:
        # Headers were unusable — surface it instead of writing garbage.
        raise RuntimeError("export headers unusable: "
                           f"{list((reader.fieldnames or []))}")
    return rows


def read_export_rows(path: str) -> list[dict[str, str]]:
    """Read a full-book export (.xlsx or .csv) into normalized row dicts."""
    if not os.path.isfile(path):
        raise RuntimeError(f"export not found: {path}")
    if path.lower().endswith(".xlsx"):
        return _read_workbook_rows(path)
    return _read_csv_rows(path)


def _applicant_ids(rows: list[dict[str, str]]) -> set[str]:
    ids = set()
    for r in rows:
        raw = (r.get("applicant_id") or "").strip()
        if raw and raw != "0":
            ids.add(raw)
    return ids


def refresh_report(source: str, live_csv: str, data_dir: str = "",
                   apply: bool = False,
                   min_row_ratio: float = DEFAULT_MIN_ROW_RATIO,
                   mark_resolved: bool = False) -> dict[str, Any]:
    """Build the refresh report; write only with ``apply=True``."""
    from .cert_applicant_index import build_index, normalize_account_name

    new_rows = read_export_rows(source)
    new_ids = _applicant_ids(new_rows)
    report: dict[str, Any] = {
        "source": source,
        "live_csv": live_csv,
        "new_rows": len(new_rows),
        "new_applicant_ids": len(new_ids),
        "applied": False,
    }
    if not new_ids:
        report["error"] = ("the export has no applicant IDs — refusing to "
                           "do anything with an empty directory")
        return report

    live_rows = _read_csv_rows(live_csv) if os.path.isfile(live_csv) else []
    live_ids = _applicant_ids(live_rows)
    report["live_rows"] = len(live_rows)
    report["live_applicant_ids"] = len(live_ids)

    ratio = (len(new_ids) / len(live_ids)) if live_ids else 1.0
    report["row_ratio"] = round(ratio, 4)
    report["min_row_ratio"] = min_row_ratio
    if ratio < min_row_ratio:
        report["error"] = (
            f"row-count sanity gate failed: new export has "
            f"{len(new_ids)} applicant IDs vs {len(live_ids)} live "
            f"(ratio {ratio:.3f} < floor {min_row_ratio}) — refusing to "
            f"replace the directory. Investigate the export before "
            f"re-running.")
        return report

    # Diff by normalized account name.
    live_names = {normalize_account_name(r.get("account_name"))
                  for r in live_rows}
    new_names = {normalize_account_name(r.get("account_name"))
                 for r in new_rows}
    live_names.discard("")
    new_names.discard("")
    report["names_added"] = sorted(new_names - live_names)
    report["names_removed"] = sorted(live_names - new_names)

    # Miss coverage against the fresh export.
    new_index = build_index(
        [{"account_name": r.get("account_name", ""),
          "applicant_id": r.get("applicant_id", ""),
          "email_primary": r.get("email_primary", ""),
          "phones": []} for r in new_rows],
        source_path=source)
    misses = _unresolved_misses(data_dir)
    covered = []
    for miss in misses:
        ids = new_index.by_name.get(miss["name_key"], [])
        if len(ids) == 1:
            covered.append({"name_key": miss["name_key"],
                            "raw_name": miss["raw_name"],
                            "applicant_id": ids[0],
                            "occurrences": miss["occurrences"]})
    report["unresolved_misses"] = len(misses)
    report["misses_now_covered"] = covered
    if mark_resolved and covered:
        report["misses_marked_resolved"] = _mark_misses_resolved(
            data_dir, covered)

    if apply:
        backup = _atomic_replace(live_csv, new_rows)
        report["applied"] = True
        report["backup"] = backup
    return report


def _unresolved_misses(data_dir: str) -> list[dict[str, Any]]:
    if not data_dir:
        return []
    db_path = os.path.join(data_dir, "cert-sweep.db")
    if not os.path.isfile(db_path):
        return []
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        rows = conn.execute(
            "SELECT name_key, raw_name, occurrences FROM cert_index_misses"
            " WHERE resolved_applicant_id IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # table does not exist yet
    finally:
        conn.close()
    return [{"name_key": r[0], "raw_name": r[1], "occurrences": r[2]}
            for r in rows]


def _mark_misses_resolved(data_dir: str,
                          covered: list[dict[str, Any]]) -> int:
    db_path = os.path.join(data_dir, "cert-sweep.db")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        for item in covered:
            conn.execute(
                "UPDATE cert_index_misses SET resolved_applicant_id = ?"
                " WHERE name_key = ?",
                (item["applicant_id"], item["name_key"]))
        conn.commit()
    finally:
        conn.close()
    return len(covered)


def _atomic_replace(live_csv: str, rows: list[dict[str, str]]) -> str:
    """Write the new CSV atomically; keep a timestamped backup."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{live_csv}.bak-{stamp}"
    if os.path.isfile(live_csv):
        shutil.copy2(live_csv, backup)
    tmp = f"{live_csv}.tmp-{stamp}"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in OUT_COLUMNS})
    os.replace(tmp, live_csv)
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cert_index_refresh",
        description="Governed refresh of the certificate applicant index "
                    "CSV (dry run unless --apply).")
    parser.add_argument("--source", required=True,
                        help="fresh full-book export (.xlsx or .csv)")
    parser.add_argument("--live-csv", required=True,
                        help="the CSV the sweep reads "
                             "(CERT_APPLICANT_INDEX_PATH)")
    parser.add_argument("--data-dir", default="",
                        help="sweep data dir (for the miss ledger)")
    parser.add_argument("--apply", action="store_true",
                        help="atomically replace the live CSV (default: "
                             "dry run)")
    parser.add_argument("--mark-resolved", action="store_true",
                        help="mark covered misses resolved in the ledger")
    parser.add_argument("--min-row-ratio", type=float,
                        default=DEFAULT_MIN_ROW_RATIO,
                        help="fail-closed floor for new/live applicant-ID "
                             "ratio")
    args = parser.parse_args(argv)

    data_dir = args.data_dir or os.path.expanduser(
        os.environ.get("CERT_SWEEP_DATA_DIR", "~/.cert-sweep"))
    try:
        report = refresh_report(args.source, args.live_csv, data_dir,
                                apply=args.apply,
                                min_row_ratio=args.min_row_ratio,
                                mark_resolved=args.mark_resolved)
    except Exception as exc:
        print(json.dumps({"error": f"refresh failed: {exc}"}, indent=2))
        return 1
    print(json.dumps(report, indent=2, default=str))
    return 0 if "error" not in report else 1


if __name__ == "__main__":
    sys.exit(main())
