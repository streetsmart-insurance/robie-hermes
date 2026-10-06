"""Orchestration + CLI for the Weekly Expiration List report.

Run:
    python -m robie_job_engine.reports.expiration_list.runner [options]

Default mode is DRY-RUN: the full read path executes, the layout is
built, and a summary plus payload previews are printed — but NOTHING is
written to the sheet. Writes require an explicit --write (X1: no writes
by default).

E13 (LOCKED 2026-10-05): --write creates a NEW dated tab
"Expiration List YYYY-MM-DD" by duplicating Sheet1 (the read-only
template). Sheet1 is never written. Fail-closed if the dated tab already
exists; --force may overwrite ONLY that same-named dated tab.

Exit codes: 0 = success, 2 = fail-closed abort (nothing was written),
3 = partial run (some accounts failed; X3: no partial write).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import config, layout, logic, sheets
from .layout import SheetRow, build_sheet_rows
from .logic import AccountRow, fetch_retention_rows, run_account, today_et
from .portal import ExpirationListError, ExpirationPortalClient, PortalTransport


def state_dir() -> Path:
    base = os.environ.get(config.STATE_DIR_ENV, "").strip()
    if base:
        return Path(base)
    return Path.home() / ".robie" / "expiration-list"


def load_state(tab: str) -> dict[str, Any]:
    path = state_dir() / "state.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) and data.get("tab") == tab else {}


def save_state(tab: str, fingerprint: str, account_ids: list[int]) -> None:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(
        json.dumps(
            {
                "tab": tab,
                "fingerprint": fingerprint,
                "timestamp": datetime.now(ZoneInfo(config.TIMEZONE)).isoformat(),
                "account_ids": account_ids,
            },
            indent=2,
        )
    )


class FixtureClient(ExpirationPortalClient):
    """Replays recorded portal JSON so the smoke test exercises the same
    code path as live reads (logic.run_account etc.) without the box."""

    def __init__(self, fixtures: Path):
        self.fixtures = fixtures
        # Bypass transport entirely.
        self.transport = None  # type: ignore[assignment]

    def _load(self, name: str) -> Any:
        path = self.fixtures / f"{name}.json"
        if not path.exists():
            raise ExpirationListError(f"fixture missing: {name}.json")
        return json.loads(path.read_text())

    def retention_expiration_list(self, *, page_size=100, page_index=1, **kw):
        path = self.fixtures / f"retention_page_{page_index}.json"
        if not path.exists():
            return {"results": []}  # missing page = end of list
        return self._load(f"retention_page_{page_index}")

    def sidebar(self, applicant_id: int):
        return self._load(f"sidebar_{applicant_id}")

    def policies(self, applicant_id: int):
        return self._load(f"policies_{applicant_id}")["policyCards"]

    def paged_discussions(self, applicant_id: int, *, page_number=1, page_size=60):
        return self._load(f"discussions_{applicant_id}")

    def discussion_detail(self, discussion_id: int, applicant_id: int):
        return self._load(f"detail_{discussion_id}")


def build_rows(
    client: ExpirationPortalClient,
    *,
    limit_accounts: int | None = None,
    progress: Any = None,
) -> tuple[list[AccountRow], list[dict[str, Any]]]:
    """Run the full read path. Returns (rows, failures). Fail-closed is
    per-account: failures are collected and reported, never silently
    dropped; the caller decides whether to proceed (X3: no partial write).
    Accounts whose expiring policies all renewed (E8) drop off the list."""
    today = today_et()
    retention = fetch_retention_rows(client)
    if limit_accounts is not None:
        retention = retention[:limit_accounts]
    rows: list[AccountRow] = []
    failures: list[dict[str, Any]] = []
    for r in retention:
        try:
            row = run_account(client, r, today=today)
            if row is not None:
                rows.append(row)
            if progress:
                progress(f"ok {r.applicant_id}")
        except ExpirationListError as exc:
            failures.append({"applicant_id": r.applicant_id, "error": str(exc)})
            if progress:
                progress(f"FAIL {r.applicant_id}: {exc}")
    return rows, failures


def verify_sample(
    client: ExpirationPortalClient, rows: list[AccountRow], n: int
) -> list[dict[str, Any]]:
    """E14: optional independent re-check of N rows (re-runs the read path
    and diffs). See config.VERIFICATION_CHECKLIST for the standing policy."""
    corrections: list[dict[str, Any]] = []
    today = today_et()
    for row in rows[:n]:
        retention = logic.RetentionRow(
            applicant_id=row.applicant_id,
            days_to_expiration=row.days_to_expiration,
            earliest_expiration=None,
        )
        try:
            check = run_account(client, retention, today=today)
        except ExpirationListError as exc:
            corrections.append({"applicant_id": row.applicant_id, "error": str(exc)})
            continue
        if check is None:
            # E8: the account dropped off between runs (all policies renewed).
            corrections.append({"applicant_id": row.applicant_id, "diffs": ["dropped: all renewed"]})
            continue
        diffs = []
        for field in ("account_name", "producer", "notes", "last_activity_by"):
            if getattr(check, field) != getattr(row, field):
                diffs.append(field)
        if check.policy_lines != row.policy_lines:
            diffs.append("policy_lines")
        if diffs:
            corrections.append({"applicant_id": row.applicant_id, "diffs": diffs})
    return corrections


def summarize(rows: list[AccountRow]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "accounts": len(rows),
        "nonrenewal": [r.applicant_id for r in rows if r.nonrenewal],
        "cancellation_pending": [r.applicant_id for r in rows if r.cancellation_pending],
        "no_notes": [r.applicant_id for r in rows if r.notes == config.NO_NOTES_TEXT],
        # E9/E10: unassigned producers surface in the trailing "Unassigned"
        # section AND here for review.
        "unassigned_producer": [
            {"applicant_id": r.applicant_id, "producer": r.producer}
            for r in rows
            if logic.producer_section(r.producer) is None
        ],
        # E5: policies whose LOB was missing/unmapped (120-day fallback used).
        "lob_review": [
            {"applicant_id": r.applicant_id, "policies": r.lob_flags}
            for r in rows
            if r.lob_flags
        ],
    }
    # E14 (LOCKED): the verification checklist rides on every run summary.
    summary["verification_checklist"] = config.VERIFICATION_CHECKLIST
    return summary


def dated_tab(base: str, today: str) -> str:
    return f"{base} {today}"


def run_report(
    *,
    sheet_id: str = config.SHEET_ID,
    tab: str = config.DEFAULT_TAB,
    write: bool = False,
    force: bool = False,
    fixtures: Path | None = None,
    limit_accounts: int | None = None,
    verify_sample_n: int = 0,
    progress: Any = None,
) -> dict[str, Any]:
    """Execute the report. Returns a result dict; raises on fail-closed.

    `tab` is the dated base (default "Expiration List"). On --write the
    target is always "<base> YYYY-MM-DD", created by duplicating Sheet1.
    Sheet1 is never written.
    """
    if tab == config.TEMPLATE_TAB:
        raise sheets.SheetsError(
            f"refusing: '{config.TEMPLATE_TAB}' is the read-only template; "
            "weekly runs write to a dated tab instead (E13)"
        )
    client: ExpirationPortalClient
    if fixtures is not None:
        client = FixtureClient(fixtures)
    else:
        client = ExpirationPortalClient(PortalTransport())

    rows, failures = build_rows(client, limit_accounts=limit_accounts, progress=progress)
    sheet_rows = build_sheet_rows(rows)
    summary = summarize(rows)
    summary["failures"] = failures

    if failures:
        # X3: fail closed — no partial sheet write.
        summary["write"] = "skipped: partial read failures (fail-closed)"
        return summary

    if verify_sample_n:
        summary["verification_corrections"] = verify_sample(client, rows, verify_sample_n)

    values = [r.values for r in sheet_rows]
    fp = sheets.fingerprint(values)
    today_str = today_et().isoformat()
    target_tab = dated_tab(tab, today_str)

    if not write:
        summary["write"] = "dry-run: no sheet writes"
        summary["dry_run_preview"] = {
            "sheet_id": sheet_id,
            "tab": target_tab,
            "row_count": len(values),
            "fingerprint": fp,
            "first_rows": values[:6],
        }
        return summary

    # E13: write to the new dated tab only. Fail closed if it already
    # exists; --force may overwrite ONLY that same-named dated tab.
    service = sheets.connect()
    if sheets.get_sheet_id(service, sheet_id, target_tab) is not None and not force:
        raise sheets.SheetsError(
            f"fail-closed: tab '{target_tab}' already exists; "
            "a new dated tab is written per week — use --force to overwrite "
            "this same-named dated tab only (never Sheet1)"
        )
    sheet_id_num = sheets.get_sheet_id(service, sheet_id, target_tab)
    if sheet_id_num is None:
        # E13: born by duplicating Sheet1 (read-only template).
        sheet_id_num = sheets.duplicate_tab(
            service, sheet_id, config.TEMPLATE_TAB, target_tab
        )
        summary["write"] = (
            f"wrote {len(values)} rows to new dated tab '{target_tab}' "
            f"(duplicated from {config.TEMPLATE_TAB})"
        )
    else:
        sheets.clear_a_to_e(service, sheet_id, target_tab, len(values))
        summary["write"] = (
            f"wrote {len(values)} rows to existing dated tab '{target_tab}' (--force)"
        )
    sheets.write_values(service, sheet_id, target_tab, values)
    fmt_requests = layout.format_requests(sheet_id_num, sheet_rows)
    summary["format_requests"] = len(fmt_requests)
    sheets.apply_format_requests(service, sheet_id, fmt_requests)
    save_state(target_tab, fp, [r.applicant_id for r in rows])
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly Expiration List report (dry-run by default)")
    parser.add_argument("--sheet-id", default=config.SHEET_ID)
    parser.add_argument("--tab", default=config.DEFAULT_TAB,
                        help="dated-tab base (a YYYY-MM-DD suffix is appended on write)")
    parser.add_argument("--write", action="store_true", help="actually write to the sheet (default: dry-run)")
    parser.add_argument("--force", action="store_true", help="overwrite the same-named dated tab (never Sheet1)")
    parser.add_argument("--fixtures", type=Path, default=None, help="recorded portal fixtures dir (no live reads)")
    parser.add_argument("--limit-accounts", type=int, default=None)
    parser.add_argument("--verify-sample", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = run_report(
            sheet_id=args.sheet_id,
            tab=args.tab,
            write=args.write,
            force=args.force,
            fixtures=args.fixtures,
            limit_accounts=args.limit_accounts,
            verify_sample_n=args.verify_sample,
            progress=(lambda m: print(m, file=sys.stderr)),
        )
    except (ExpirationListError, sheets.SheetsError) as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"accounts: {result['accounts']}")
        print(f"non-renewal: {result['nonrenewal']}")
        print(f"cancellation pending: {result['cancellation_pending']}")
        print(f"no notes (follow-up): {result['no_notes']}")
        print(f"unassigned producer: {result['unassigned_producer']}")
        if result.get("lob_review"):
            print(f"LOB review (120-day fallback): {result['lob_review']}")
        if result.get("failures"):
            print(f"FAILURES ({len(result['failures'])}):")
            for f in result["failures"]:
                print(f"  {f['applicant_id']}: {f['error']}")
        print(f"write: {result['write']}")
        if result.get("verification_corrections") is not None:
            print(f"verification corrections: {result['verification_corrections']}")
        print(f"VERIFICATION: {result['verification_checklist']}")
    return 0 if not result.get("failures") else 3


if __name__ == "__main__":
    raise SystemExit(main())
