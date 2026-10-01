"""Playground memory snapshots. No network and no key file."""

from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from durable_temp import durable_temporary_directory

from robie_job_engine.playground_memory import memory_db_path, recall_for_turn, remember_preference
from robie_job_engine.playground_memory_backup import (
    BUCKET_ENV,
    assert_no_key_file,
    main,
    object_name,
    restore_snapshot,
    run_backup,
    snapshot_memory_db,
    snapshots_to_delete,
)


WHEN = datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc)


class MemoryBackupTests(unittest.TestCase):
    def test_snapshot_is_consistent_and_private(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            saved = remember_preference(
                db,
                requested_by="Casey",
                fact="I like short notes",
                now=WHEN,
            )
            self.assertEqual(saved.scope, "person")
            dest = Path(tmp) / "snap.sqlite"
            snapshot_memory_db(db, dest)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o640)
            conn = sqlite3.connect(dest)
            try:
                row = conn.execute("SELECT scope, author, text FROM playground_memory").fetchone()
            finally:
                conn.close()
            self.assertEqual(row, ("person", "Casey", "I like short notes"))
            live = sqlite3.connect(memory_db_path(db))
            try:
                columns = {info[1] for info in live.execute("PRAGMA table_info(playground_memory)")}
            finally:
                live.close()
            self.assertTrue(
                {
                    "scope",
                    "author",
                    "team",
                    "applicant_id",
                    "text",
                    "created_at",
                    "source_job_id",
                    "expires_at",
                }.issubset(columns)
            )

    def test_old_snapshots_are_the_ones_deleted(self):
        objects = [
            (
                "playground-memory/host/2026-09-01T020000Z.sqlite",
                datetime(2026, 9, 1, tzinfo=timezone.utc),
            ),
            (
                "playground-memory/host/2026-09-20T020000Z.sqlite",
                datetime(2026, 9, 20, tzinfo=timezone.utc),
            ),
        ]
        stale = snapshots_to_delete(objects, now=WHEN, days=14)
        self.assertEqual(stale, ["playground-memory/host/2026-09-01T020000Z.sqlite"])

    def test_key_file_is_refused(self):
        with mock.patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "/opt/keys/sa.json"}):
            with self.assertRaises(RuntimeError) as caught:
                assert_no_key_file()
        self.assertIn("key files", str(caught.exception).casefold())

    def test_unset_bucket_uploads_nothing(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            remember_preference(db, requested_by="Casey", fact="I like short notes", now=WHEN)
            uploaded: list[str] = []
            with mock.patch.dict(os.environ, {BUCKET_ENV: ""}, clear=False):
                os.environ.pop(BUCKET_ENV, None)
                result = run_backup(
                    db,
                    now=WHEN,
                    hostname="hermes-test-01",
                    snapshot_dir=Path(tmp),
                    upload=lambda *_args: uploaded.append("up"),
                    list_objects=lambda *_args: [],
                    delete_object=lambda *_args: uploaded.append("del"),
                )
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(uploaded, [])

    def test_upload_then_prune_and_restore(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            remember_preference(db, requested_by="Casey", fact="I like short notes", now=WHEN)
            stored: dict[str, bytes] = {}

            def upload(_bucket: str, name: str, data: bytes) -> None:
                stored[name] = data

            def list_objects(_bucket: str, _prefix: str):
                return [
                    (
                        "playground-memory/hermes-test-01/2026-09-01T020000Z.sqlite",
                        datetime(2026, 9, 1, tzinfo=timezone.utc),
                    ),
                    (object_name(now=WHEN, hostname="hermes-test-01"), WHEN),
                ]

            deleted: list[str] = []
            env = {
                BUCKET_ENV: "robie-memory-test",
                "GOOGLE_APPLICATION_CREDENTIALS": "",
                "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON": "",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                result = run_backup(
                    db,
                    now=WHEN,
                    hostname="hermes-test-01",
                    snapshot_dir=Path(tmp),
                    upload=upload,
                    list_objects=list_objects,
                    delete_object=lambda _bucket, name: deleted.append(name),
                )
            self.assertEqual(result["status"], "uploaded")
            self.assertEqual(deleted, ["playground-memory/hermes-test-01/2026-09-01T020000Z.sqlite"])
            name = str(result["object"])
            self.assertIn(name, stored)
            remember_preference(db, requested_by="Casey", fact="I like long notes", now=WHEN)
            self.assertTrue(
                any("long notes" in item.text for item in recall_for_turn(db, requested_by="Casey", text="notes", now=WHEN))
            )
            snapshot = Path(tmp) / "back.sqlite"
            snapshot.write_bytes(stored[name])
            restore_snapshot(db, snapshot)
            restored = recall_for_turn(db, requested_by="Casey", text="notes", now=WHEN)
            self.assertTrue(any("short notes" in item.text for item in restored))
            self.assertFalse(any("long notes" in item.text for item in restored))
            self.assertEqual(Path(memory_db_path(db)).stat().st_mode & 0o777, 0o640)

    def test_restore_command_refuses_without_confirm(self):
        with mock.patch.dict(os.environ, {BUCKET_ENV: "robie-memory-test"}):
            code = main(["restore", "--object", "playground-memory/host/2026-09-30T070000Z.sqlite"])
        self.assertEqual(code, 2)

    def test_legacy_rows_migrate_to_the_new_columns(self):
        with durable_temporary_directory() as tmp:
            db = str(Path(tmp) / "jobs.db")
            path = memory_db_path(db)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE playground_memory (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       scope TEXT NOT NULL,
                       owner TEXT NOT NULL,
                       kind TEXT NOT NULL,
                       subject TEXT NOT NULL,
                       body TEXT NOT NULL,
                       client_name TEXT NOT NULL DEFAULT '',
                       job_id TEXT NOT NULL DEFAULT '',
                       outcome TEXT NOT NULL DEFAULT '',
                       created_at TEXT NOT NULL,
                       forgotten_at TEXT NOT NULL DEFAULT ''
                   )"""
            )
            conn.execute(
                """INSERT INTO playground_memory (
                       scope, owner, kind, subject, body, created_at
                   ) VALUES ('person', 'Casey', 'preference', 'Casey', 'I like short notes', ?)""",
                (WHEN.isoformat(),),
            )
            conn.commit()
            conn.close()
            found = recall_for_turn(db, requested_by="Casey", text="notes", now=WHEN)
            hidden = recall_for_turn(db, requested_by="Alex", text="notes", now=WHEN)
            self.assertTrue(any(item.text == "I like short notes" for item in found))
            self.assertEqual(found[0].author, "Casey")
            self.assertFalse(any(item.text == "I like short notes" for item in hidden))
