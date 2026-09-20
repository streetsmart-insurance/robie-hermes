#!/usr/bin/env python3
"""ONE-SHOT Test overdue submission reports on hermes-test-01 only.

UI patches (process-local, no release edits):
- MDC agency checkbox force-click
- Page-size 100 force-click
- Status sort header force-click

CLEAN Sheets path (Test only):
- Uses load_approved_producer_directory (live Sheets ADC) — no producers→carlo map.
- ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT still remaps To:/cc=[] to Carlo only
  with TEST ONLY subjects after every producer resolves against the live roster.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

assert os.environ.get("ROBIE_ENV") == "TEST", repr(os.environ.get("ROBIE_ENV"))
sink = (os.environ.get("ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT") or "").strip().casefold()
assert sink == "carlo@streetsmart.insurance", repr(sink)
host = open("/proc/sys/kernel/hostname").read().strip()
assert host.startswith("hermes-test-01"), host

ROOT = Path("/opt/streetsmart-hermes-test/releases/current")
sys.path.insert(0, str(ROOT / ".gateway-runtime"))
sys.path.insert(0, str(ROOT))

from playwright.sync_api import Error as PlaywrightError
import robie_job_engine.submission_audit_runner as sar
from robie_job_engine.ezlynx_session_lock import exclusive_session
from robie_job_engine.submission_audit import ensure_ezlynx_login
from robie_job_engine.overdue_submission_reports import (
    OverdueSubmissionReportVerifier,
    OverdueSubmissionReportWorker,
    load_approved_producer_directory,
)
from robie_job_engine.store import JobStore
from robie_job_engine.test_runtime import build_test_engine

_orig_activate = sar._activate


def _activate_mdc_safe(locator, label: str, *, key: str = "Enter") -> None:
    target = sar._first_visible(locator, label)
    target.scroll_into_view_if_needed()
    if "sort header" in label.casefold():
        container = target.locator(".mat-sort-header-container")
        try:
            if container.count():
                container.first.click(timeout=5_000, force=True)
            else:
                target.click(timeout=5_000, force=True)
            return
        except PlaywrightError:
            pass
    if key == "Space":
        nested = target.locator("input[type='checkbox'], input")
        if nested.count():
            try:
                nested.first.click(timeout=5_000, force=True)
                return
            except PlaywrightError:
                pass
    try:
        _orig_activate(locator, label, key=key)
    except Exception:
        try:
            target.click(timeout=5_000, force=True)
        except PlaywrightError as exc:
            raise RuntimeError(
                f"PLAYWRIGHT_BLOCKED: visible {label} could not be activated"
            ) from exc


def _set_page_size_force(page) -> None:
    paginator = page.locator("mat-paginator")
    if not paginator.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: paginator not found")
    selected_value = paginator.locator(
        ".mat-mdc-select-value-text, .mat-select-value-text"
    )
    if selected_value.count() and selected_value.first.inner_text().strip() == "100":
        return
    combo = paginator.get_by_role("combobox")
    if not combo.count():
        combo = paginator.locator("mat-select")
    if not combo.count():
        raise RuntimeError("PLAYWRIGHT_BLOCKED: page-size control not found")
    combo.first.click(timeout=5_000, force=True)
    page.wait_for_timeout(500)
    opt = page.get_by_role("option", name="100", exact=True)
    if not opt.count():
        opts = page.locator("mat-option, [role=option]")
        chosen = None
        for index in range(opts.count()):
            text = " ".join((opts.nth(index).inner_text() or "").split())
            if text == "100":
                chosen = opts.nth(index)
                break
        if chosen is None:
            raise RuntimeError("PLAYWRIGHT_BLOCKED: 100 page-size option not found")
        chosen.click(timeout=5_000, force=True)
    else:
        opt.first.click(timeout=5_000, force=True)
    page.wait_for_timeout(1_000)


sar._activate = _activate_mdc_safe
sar._set_page_size = _set_page_size_force

_cached_observation: dict | None = None


def patched_audit_reader() -> dict:
    global _cached_observation
    if _cached_observation is None:
        with exclusive_session():
            ensure_ezlynx_login()
            _cached_observation = sar.audit(fresh=True)
        # Persist redacted summary for operators
        summary = {
            "open_over_30_count": _cached_observation.get("open_over_30_count"),
            "counts_by_producer": _cached_observation.get("counts_by_producer"),
            "counts_by_status": _cached_observation.get("counts_by_status"),
            "pages_reviewed": _cached_observation.get("pages_reviewed"),
            "boundary_kind": _cached_observation.get("boundary_kind"),
            "pager_total": _cached_observation.get("pager_total"),
        }
        Path("/opt/streetsmart-hermes-test/test-tmp/overdue-audit-summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
        )
        print("AUDIT_SUMMARY " + json.dumps(summary, sort_keys=True), flush=True)
    return _cached_observation


def sheets_directory_loader(manifest_path: str) -> dict[str, str]:
    """Real Sheets roster — no invented producers→carlo map."""
    directory = load_approved_producer_directory(manifest_path)
    print(
        "SHEETS_DIRECTORY "
        + json.dumps(
            {"count": len(directory), "sample_keys": sorted(directory)[:8]},
            sort_keys=True,
        ),
        flush=True,
    )
    return directory

DB = "/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db"
MANIFEST = "/opt/streetsmart-hermes-test/accountability/connection-manifest.json"
ACTION = "send_producer_reports"
JOB_TYPE = "ezlynx.overdue_submission_reports"
key = f"test-overdue-clean-sheets-{uuid.uuid4().hex[:12]}"

store = JobStore(DB)
job = store.create_job(
    JOB_TYPE,
    {
        "worker": "overdue-submission-reports",
        "task_name": "TEST ONE-SHOT overdue submission producer reports (clean Sheets + Carlo sink)",
        "resource_id": "ezlynx:submission-center:overview:submissions",
        "manifest_path": MANIFEST,
        "authorized_actions": [ACTION],
        "test_rehearsal": True,
        "perform_timeout_seconds": 3600,
        "scope": {
            "time_frame": "All Submissions",
            "assigned_producer": "Streetsmart Insurance",
            "my_submissions": False,
            "page_size": 100,
            "status_sort": "ascending",
            "inspection_boundary": "first_closed_row_or_pager_exhausted",
        },
    },
    idempotency_key=key,
)
job_id = str(job["id"])
print(
    json.dumps(
        {
            "created_job_id": job_id,
            "idempotency_key": key,
            "sink": sink,
            "sheets_path": "load_approved_producer_directory",
            "mdc_click_patch": True,
            "page_size_force_click": True,
            "status_sort_force_click": True,
            "in_process_audit_reader": True,
        },
        sort_keys=True,
    ),
    flush=True,
)

engine = build_test_engine(store)
engine.perform_timeout_seconds = 3600
engine.lease_seconds = 3600
engine.call_worker_on_calling_thread = True
engine.workers["overdue-submission-reports"] = OverdueSubmissionReportWorker(
    audit_reader=patched_audit_reader,
    directory_loader=sheets_directory_loader,
)
engine.verifiers["ezlynx.overdue_submission_reports"] = OverdueSubmissionReportVerifier(
    audit_reader=patched_audit_reader
)

t0 = time.time()
try:
    engine.run(job_id)
except Exception as exc:
    print(f"ENGINE_EXCEPTION {type(exc).__name__}: {exc}", flush=True)
elapsed = round(time.time() - t0, 1)
final = store.get_job(job_id) or {}

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT id, status, last_error, action_type, attempt_count, verification_count FROM jobs WHERE id=?",
    (job_id,),
).fetchone()
attempts = conn.execute(
    "SELECT * FROM attempts WHERE job_id=? ORDER BY rowid DESC LIMIT 8", (job_id,)
).fetchall()
ves = conn.execute(
    "SELECT * FROM verification_evidence WHERE job_id=? ORDER BY rowid DESC LIMIT 5",
    (job_id,),
).fetchall()
conn.close()


def row_to_dict(r):
    d = {k: r[k] for k in r.keys()}
    for k, v in list(d.items()):
        if isinstance(v, str) and len(v) > 2000:
            d[k] = v[:2000] + "...<truncated>"
    return d


safe = {
    "job_id": job_id,
    "status": final.get("status") or (row["status"] if row else None),
    "last_error": final.get("last_error") or (row["last_error"] if row else None),
    "elapsed_seconds": elapsed,
    "action_type": final.get("action_type") or (row["action_type"] if row else None),
    "attempt_count": row["attempt_count"] if row else None,
    "verification_count": row["verification_count"] if row else None,
    "attempts": [row_to_dict(a) for a in attempts],
    "verification_evidence": [row_to_dict(v) for v in ves],
    "sink_env": sink,
    "host": host,
    "sheets_path": "load_approved_producer_directory",
}
print(json.dumps(safe, indent=2, sort_keys=True, default=str), flush=True)
