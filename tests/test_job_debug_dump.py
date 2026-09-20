"""Read-only job debug dump. Isolated fixtures only. No live jobs.db."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from durable_temp import ROOT as DURABLE_TEST_ROOT
from durable_temp import durable_temporary_directory

from robie_job_engine.job_debug_dump import (
    default_jobs_db,
    dump_job,
    format_dump,
    main,
    open_readonly,
    resolve_jobs_db_for_job,
)
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore


LAST_ERROR = (
    "PLAYWRIGHT_BLOCKED: unique locator failed on Import document "
    "(live leftover 38c0fa79 class)"
)
KNOWN_JOB = "9b2c1a70-aaaa-4b11-8c22-ffffffffffff"


def _seed_job(db: Path, artifacts: Path, recordings: Path, logs: Path) -> str:
    store = JobStore(db)
    job = store.create_job(
        "hermes.google_chat_task",
        {
            "text": "post a note on EZLynx",
            "applicant_id": "220250093",
            "worker": "hermes-cua",
            "cdp_url": "http://127.0.0.1:9222",
        },
        idempotency_key="job-debug-dump-seed",
    )
    job_id = job["id"]
    store.transition(
        job_id,
        JobStatus.FAILED,
        error=LAST_ERROR,
        release_lease=True,
    )
    store.checkpoint(
        job_id,
        "action",
        {
            "note_id": "note-4411",
            "doc_id": "doc-8822",
            "discussion_id": "disc-17",
            "discussion_title": "Manual Renewal",
        },
    )
    store.checkpoint(
        job_id,
        "gateway_progress",
        {"source": "hermes-gateway", "first_at": "t0", "last_at": "t1"},
    )
    store.add_playwright_exec(
        job_id,
        "playwright_exec",
        "error",
        code_preview="page.get_by_role('button', name='Import document')",
        result={"error": "PLAYWRIGHT_BLOCKED"},
    )
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS durable_work_items (
                namespace TEXT NOT NULL,
                work_item_key TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                external_actions INTEGER NOT NULL DEFAULT 0,
                outcome TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, work_item_key)
            );
            CREATE TABLE IF NOT EXISTS job_recordings (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                segment_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                local_path TEXT NOT NULL,
                stop_file TEXT NOT NULL,
                drive_file_id TEXT,
                drive_url TEXT,
                started_at TEXT NOT NULL,
                stopped_at TEXT
            );
            """
        )
        conn.execute(
            """INSERT INTO durable_work_items
               (namespace, work_item_key, outcome, verified, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (
                "hermes.google_chat_task",
                f"policy:applicant_id:220250093:{job_id}",
                json.dumps(
                    {
                        "applicant_id": "220250093",
                        "ezlynx_note_id": "note-4411",
                        "outcome": "FAILED",
                    }
                ),
                0,
                "2026-09-17T00:00:00+00:00",
                "2026-09-17T00:01:00+00:00",
            ),
        )
        rec_path = recordings / f"{job_id}.webm"
        rec_path.write_bytes(b"webm")
        conn.execute(
            """INSERT INTO job_recordings
               (id, job_id, segment_number, status, local_path, stop_file,
                drive_url, started_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                "rec-1",
                job_id,
                1,
                "READY",
                str(rec_path),
                str(rec_path) + ".stop",
                "https://drive.example/rec",
                "2026-09-17T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    art_dir = artifacts / job_id
    art_dir.mkdir(parents=True)
    (art_dir / "playwright-trace.zip").write_bytes(b"PK")
    logs.write_text(
        "\n".join(
            [
                "unrelated scheduler tick",
                f"{job_id} playwright_exec PLAYWRIGHT_BLOCKED",
                "still unrelated",
                f"prefix {job_id[:8]} gateway_progress last_at=t1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return job_id


class JobDebugDumpTests(unittest.TestCase):
    def test_dump_prints_job_row_destination_and_evidence(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            db = root / "jobs.db"
            artifacts = root / "artifacts"
            recordings = root / "recordings"
            logs = root / "engine.log"
            artifacts.mkdir()
            recordings.mkdir()
            job_id = _seed_job(db, artifacts, recordings, logs)
            before = sqlite3.connect(db).execute(
                "SELECT id, status, last_error FROM jobs"
            ).fetchall()
            report = dump_job(
                job_id,
                db_path=db,
                artifact_root=artifacts,
                recording_root=recordings,
                log_path=logs,
            )
            after = sqlite3.connect(db).execute(
                "SELECT id, status, last_error FROM jobs"
            ).fetchall()
            self.assertEqual(before, after)
            self.assertTrue(report["ok"])
            self.assertTrue(report["read_only"])
            self.assertEqual(report["job"]["last_error"], LAST_ERROR)
            self.assertEqual(report["job"]["status"], JobStatus.FAILED.value)
            self.assertEqual(report["job"]["action_type"], "hermes.google_chat_task")
            self.assertEqual(report["job"]["worker"], "hermes-cua")
            self.assertEqual(report["destination"]["applicant_id"], "220250093")
            self.assertEqual(report["destination"]["note_id"], "note-4411")
            self.assertEqual(report["destination"]["doc_id"], "doc-8822")
            self.assertEqual(report["destination"]["discussion_title"], "Manual Renewal")
            self.assertEqual(report["destination"]["discussion_id"], "disc-17")
            self.assertEqual(report["cdp"]["CDP_URL"], "http://127.0.0.1:9222")
            self.assertEqual(report["cdp"]["CDP_PORT"], "9222")
            self.assertTrue(report["trace"]["present"])
            self.assertEqual(report["playwright_exec_count"], 1)
            self.assertTrue(report["durable_work_items"])
            self.assertIn("note-4411", report["durable_work_items"][0]["outcome"])
            text = format_dump(report)
            self.assertIn(LAST_ERROR, text)
            self.assertIn(f"JOB_ID={job_id}", text)
            self.assertIn("NOTE_ID=note-4411", text)
            self.assertIn("DOC_ID=doc-8822", text)
            self.assertIn("DISCUSSION_TITLE=Manual Renewal", text)
            self.assertIn("APPLICANT_ID=220250093", text)
            self.assertIn("TRACE_PATH=", text)
            self.assertIn("RECORDING_PATH=", text)
            self.assertIn("present=yes", text)
            self.assertIn("CDP_URL=http://127.0.0.1:9222", text)
            self.assertIn("playwright_exec PLAYWRIGHT_BLOCKED", text)
            self.assertIn("read-only", text.casefold())

            code = main(
                [
                    job_id[:8],
                    "--db",
                    str(db),
                    "--artifact-root",
                    str(artifacts),
                    "--recording-root",
                    str(recordings),
                    "--log",
                    str(logs),
                ]
            )
            self.assertEqual(code, 0)

    def test_missing_job_and_db_are_non_zero(self):
        with durable_temporary_directory() as tmp:
            db = Path(tmp) / "jobs.db"
            JobStore(db)
            missing = dump_job("00000000-0000-0000-0000-000000000000", db_path=db)
            self.assertFalse(missing["ok"])
            self.assertIn("job not found", missing["error"])
            self.assertEqual(
                main(["00000000-0000-0000-0000-000000000000", "--db", str(db)]),
                2,
            )
            absent = dump_job("anything", db_path=Path(tmp) / "no-such.db")
            self.assertFalse(absent["ok"])
            self.assertIn("missing", absent["error"])

    def test_ambiguous_prefix_does_not_guess(self):
        with durable_temporary_directory() as tmp:
            db = Path(tmp) / "jobs.db"
            JobStore(db)
            conn = sqlite3.connect(db)
            now = "2026-09-17T00:00:00+00:00"
            payload = json.dumps({"text": "x"})
            conn.execute(
                """INSERT INTO jobs
                   (id,idempotency_key,action_type,payload_json,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (KNOWN_JOB, "k1", "hermes.google_chat_task", payload, "FAILED", now, now),
            )
            conn.execute(
                """INSERT INTO jobs
                   (id,idempotency_key,action_type,payload_json,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    "9b2c1a70-bbbb-4b11-8c22-eeeeeeeeeeee",
                    "k2",
                    "hermes.google_chat_task",
                    payload,
                    "FAILED",
                    now,
                    now,
                ),
            )
            conn.commit()
            conn.close()
            report = dump_job("9b2c1a70", db_path=db)
            self.assertFalse(report["ok"])
            self.assertIn("ambiguous", report["error"])
            self.assertEqual(len(report["matches"]), 2)

    def test_open_readonly_rejects_writes(self):
        with durable_temporary_directory() as tmp:
            db = Path(tmp) / "jobs.db"
            JobStore(db)
            conn = open_readonly(db)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("UPDATE jobs SET status='COMPLETE'")
            finally:
                conn.close()

    def test_default_db_prefers_env(self):
        import os

        previous = os.environ.get("ROBIE_JOB_DB")
        isolated = str(DURABLE_TEST_ROOT / "isolated-jobs.db")
        os.environ["ROBIE_JOB_DB"] = isolated
        try:
            self.assertEqual(default_jobs_db(), isolated)
        finally:
            if previous is None:
                os.environ.pop("ROBIE_JOB_DB", None)
            else:
                os.environ["ROBIE_JOB_DB"] = previous

    def test_resolves_je_kill_phase_db_when_missing_from_primary(self):
        import os

        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            primary = root / "main-jobs.db"
            JobStore(primary)
            run_root = root / "je-kill" / "runs"
            phase_db = (
                run_root
                / "je-kill-01-20260918T113930Z-bd39bfc2"
                / "before_action"
                / "jobs.db"
            )
            phase_db.parent.mkdir(parents=True)
            store = JobStore(phase_db)
            job = store.create_job(
                "ezlynx.apply_label",
                {"account_id": "220250093", "worker": "hermes-cua"},
                idempotency_key="phase-seed",
            )
            store.transition(
                job["id"],
                JobStatus.FAILED,
                expected={JobStatus.PENDING},
                error=LAST_ERROR,
                release_lease=True,
            )
            store.add_playwright_exec(
                job["id"],
                "ensure_clean_destination",
                "error",
                code_preview='tr:has(#document-checkbox-813253571-input) button:has-text("Add label")',
                result={"error": LAST_ERROR, "action_url": "https://app.ezlynx.com/web/account/220250093/documents"},
            )
            previous = os.environ.get("ROBIE_JE_KILL_RUN_ROOT")
            os.environ["ROBIE_JE_KILL_RUN_ROOT"] = str(run_root)
            try:
                path, source = resolve_jobs_db_for_job(job["id"], primary_db=primary)
                self.assertEqual(source, "je-kill-phase")
                self.assertEqual(Path(path), phase_db.resolve())
                report = dump_job(job["id"], db_path=primary)
            finally:
                if previous is None:
                    os.environ.pop("ROBIE_JE_KILL_RUN_ROOT", None)
                else:
                    os.environ["ROBIE_JE_KILL_RUN_ROOT"] = previous
            self.assertTrue(report["ok"])
            self.assertEqual(report["db_source"], "je-kill-phase")
            self.assertEqual(report["job"]["last_error"], LAST_ERROR)
            self.assertEqual(report["playwright_exec_count"], 1)
            text = format_dump(report)
            self.assertIn("DB_SOURCE=je-kill-phase", text)
            self.assertIn("TOOL=ensure_clean_destination", text)
            self.assertIn("CODE_PREVIEW=", text)
            self.assertIn("RESULT=", text)
            self.assertIn(LAST_ERROR, text)


if __name__ == "__main__":
    unittest.main()
