from __future__ import annotations

import os
import json
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from durable_temp import durable_temporary_directory

from robie_job_engine.chat_admin import (
    handle_admin_command,
    next_cron_time,
    parse_admin_command,
)
from robie_job_engine.chat_guard import open_chat_job
from robie_job_engine.email_guard import run_guarded_email_task
from robie_job_engine.engine import JobEngine
from robie_job_engine.request_routing import classify_request
from robie_job_engine.operations import OperationsStore
from robie_job_engine.scheduler import run_once
from robie_job_engine.skill_sync import (
    DEFAULT_SUBMISSION_CENTER_SOP_URL,
    DRIVE_READONLY_SCOPE,
    GOOGLE_DOC_MIME,
    GOOGLE_FOLDER_MIME,
    DriveSkillSync,
    DriveSkillSyncVerifier,
    DriveSkillSyncWorker,
    GoogleDriveSkillSource,
    google_doc_html_to_markdown,
    load_active_context,
    load_core_context,
    submission_center_sop_url,
)
from robie_job_engine.store import JobStore


class _Drive:
    def __init__(self) -> None:
        self.children = {
            "root": [self.folder("street", "StreetSmart")],
            "street": [self.folder("robie", "Robie")],
            "robie": [self.folder("skills", "Skills")],
            "skills": [
                self.folder("core", "01_Core_Rules"),
                self.folder("draft", "02_Draft_Skills"),
                self.folder("active", "03_Active_Skills"),
                self.folder("templates", "04_Templates"),
                self.folder("archive", "05_Archive"),
            ],
            "core": [self.file("c1", "Global.md", b"Never expose credentials.")],
            "active": [
                self.file(
                    "a1",
                    "submission center checker.md",
                    (
                        b"Use the [Submission SOP](https://docs.google.com/document/d/"
                        b"1nggrFQY-q9PEDOjGcje04qYKUTx-qTGTx3Wv-4qD80M/edit)."
                    ),
                ),
                self.folder("nested-archive", "05_Archive"),
                self.file("ignore-pdf", "not-a-skill.pdf", b"ignore", mime="application/pdf"),
            ],
            "nested-archive": [self.file("old", "old.md", b"must not load")],
            "draft": [self.file("draft-file", "draft.md", b"must not load")],
            "templates": [],
            "archive": [],
        }
        self.data = {
            item["id"]: item.pop("_data")
            for items in self.children.values()
            for item in items
            if "_data" in item
        }

    @staticmethod
    def folder(file_id: str, name: str) -> dict:
        return {"id": file_id, "name": name, "mimeType": GOOGLE_FOLDER_MIME}

    @staticmethod
    def file(file_id: str, name: str, data: bytes, mime: str = "text/markdown") -> dict:
        return {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "modifiedTime": "2026-08-25T12:00:00Z",
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
            "_data": data,
        }

    def find_folder(self, parent_id: str, name: str) -> str:
        matches = [item for item in self.children[parent_id] if item["name"] == name]
        if len(matches) != 1:
            raise RuntimeError(name)
        return matches[0]["id"]

    def list_children(self, parent_id: str) -> list[dict]:
        return [dict(item) for item in self.children.get(parent_id, [])]

    def read_file(self, item: dict) -> bytes:
        return self.data[item["id"]]


class SkillSyncAndChatAdminTests(unittest.TestCase):
    def test_drive_source_uses_secret_manager_oauth_token_in_memory(self):
        token_info = {
            "type": "authorized_user",
            "client_id": "client-id",
            "client_secret": "client-secret-marker",
            "refresh_token": "refresh-token-marker",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        access = Mock()
        access.return_value.payload.data = json.dumps(token_info).encode("utf-8")
        sm_client = Mock(return_value=types.SimpleNamespace(access_secret_version=access))
        credential = object()
        credential_factory = Mock(return_value=credential)
        drive_service = object()
        build = Mock(return_value=drive_service)
        adc = Mock(side_effect=AssertionError("ADC must not be used"))

        google = types.ModuleType("google")
        google.__path__ = []
        google_auth = types.ModuleType("google.auth")
        google_auth.default = adc
        google.auth = google_auth
        google_cloud = types.ModuleType("google.cloud")
        google_cloud.__path__ = []
        secretmanager = types.ModuleType("google.cloud.secretmanager")
        secretmanager.SecretManagerServiceClient = sm_client
        google_cloud.secretmanager = secretmanager
        google_oauth2 = types.ModuleType("google.oauth2")
        google_oauth2.__path__ = []
        credentials_module = types.ModuleType("google.oauth2.credentials")
        credentials_module.Credentials = types.SimpleNamespace(
            from_authorized_user_info=credential_factory
        )
        google_oauth2.credentials = credentials_module
        google_api = types.ModuleType("googleapiclient")
        google_api.__path__ = []
        discovery = types.ModuleType("googleapiclient.discovery")
        discovery.build = build
        google_api.discovery = discovery

        modules = {
            "google": google,
            "google.auth": google_auth,
            "google.cloud": google_cloud,
            "google.cloud.secretmanager": secretmanager,
            "google.oauth2": google_oauth2,
            "google.oauth2.credentials": credentials_module,
            "googleapiclient": google_api,
            "googleapiclient.discovery": discovery,
        }
        with patch.dict(sys.modules, modules), patch.dict(
            os.environ,
            {
                "ROBIE_GOOGLE_TOKEN_SECRET": "robie-google-oauth-token",
                "ROBIE_GOOGLE_TOKEN_PROJECT": "test-project",
            },
            clear=False,
        ):
            source = GoogleDriveSkillSource.from_environment()

        self.assertIs(source.service, drive_service)
        access.assert_called_once_with(
            name=(
                "projects/test-project/secrets/"
                "robie-google-oauth-token/versions/latest"
            )
        )
        credential_factory.assert_called_once_with(
            token_info, scopes=[DRIVE_READONLY_SCOPE]
        )
        build.assert_called_once_with(
            "drive", "v3", credentials=credential, cache_discovery=False
        )

    def test_drive_source_rejects_invalid_secret_id_before_access(self):
        with patch.dict(
            os.environ,
            {
                "ROBIE_GOOGLE_TOKEN_SECRET": "../not-valid",
                "ROBIE_GOOGLE_TOKEN_PROJECT": "test-project",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "not a valid secret ID"):
                GoogleDriveSkillSource._credentials_from_secret_manager("../not-valid")

    def test_drive_secret_validation_error_never_discloses_secret_values(self):
        secret_marker = "do-not-leak-client-secret"
        refresh_marker = "do-not-leak-refresh-token"
        payload = json.dumps(
            {"client_secret": secret_marker, "refresh_token": refresh_marker}
        ).encode("utf-8")
        access = Mock()
        access.return_value.payload.data = payload
        secretmanager = types.ModuleType("google.cloud.secretmanager")
        secretmanager.SecretManagerServiceClient = Mock(
            return_value=types.SimpleNamespace(access_secret_version=access)
        )
        google_cloud = types.ModuleType("google.cloud")
        google_cloud.__path__ = []
        google_cloud.secretmanager = secretmanager
        google_oauth2 = types.ModuleType("google.oauth2")
        google_oauth2.__path__ = []
        credentials_module = types.ModuleType("google.oauth2.credentials")
        credentials_module.Credentials = object
        google_oauth2.credentials = credentials_module
        with patch.dict(
            sys.modules,
            {
                "google.cloud": google_cloud,
                "google.cloud.secretmanager": secretmanager,
                "google.oauth2": google_oauth2,
                "google.oauth2.credentials": credentials_module,
            },
        ), patch.dict(
            os.environ,
            {"ROBIE_GOOGLE_TOKEN_PROJECT": "test-project"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as caught:
                GoogleDriveSkillSource._credentials_from_secret_manager("valid-secret")
        message = str(caught.exception)
        self.assertNotIn(secret_marker, message)
        self.assertNotIn(refresh_marker, message)

    def test_dual_folder_sync_excludes_unapproved_and_rereads_exact_hashes(self):
        with durable_temporary_directory() as td:
            root = Path(td) / "sync"
            manifest = DriveSkillSync(_Drive(), destination=root).sync()
            self.assertEqual(len(manifest["files"]), 2)
            self.assertEqual(manifest["included_folders"], ["01_Core_Rules", "03_Active_Skills"])
            self.assertEqual(
                set(manifest["excluded_folders"]),
                {"02_Draft_Skills", "04_Templates", "05_Archive"},
            )
            self.assertIn("Never expose credentials", load_core_context(root))
            self.assertIn("Submission SOP", load_active_context(root))
            self.assertNotIn("must not load", load_active_context(root))
            self.assertEqual(
                submission_center_sop_url(root), DEFAULT_SUBMISSION_CENTER_SOP_URL
            )
            action = {
                "destination": {
                    "destination_root": str(root),
                    "snapshot_digest": manifest["snapshot_digest"],
                }
            }
            self.assertTrue(DriveSkillSyncVerifier().verify({}, action).verified)
            file_row = manifest["files"][0]
            scope = "core" if file_row["scope"] == "CORE_RULE" else "active"
            target = root / "snapshots" / manifest["snapshot_digest"] / scope / file_row["relative_path"]
            target.write_text("tampered", encoding="utf-8")
            self.assertFalse(DriveSkillSyncVerifier().verify({}, action).verified)

    def test_same_named_drive_files_get_distinct_snapshot_paths_and_verify(self):
        drive = _Drive()
        for file_id, content in (
            ("duplicate-a", b"first version"),
            ("duplicate-b", b"second version"),
        ):
            item = drive.file(file_id, "Collision.md", content)
            drive.data[file_id] = item.pop("_data")
            drive.children["core"].append(item)

        with durable_temporary_directory() as td:
            root = Path(td) / "sync"
            manifest = DriveSkillSync(drive, destination=root).sync()
            duplicates = [
                row for row in manifest["files"] if row["name"] == "Collision.md"
            ]
            self.assertEqual(len(duplicates), 2)
            self.assertEqual(len({row["relative_path"] for row in duplicates}), 2)
            self.assertTrue(
                all(row["drive_file_id"] in row["relative_path"] for row in duplicates)
            )
            action = {
                "destination": {
                    "destination_root": str(root),
                    "snapshot_digest": manifest["snapshot_digest"],
                }
            }
            self.assertTrue(DriveSkillSyncVerifier().verify({}, action).verified)

    def test_drive_sync_job_completes_only_after_exact_snapshot_reread(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            store = JobStore(root / "jobs.db")
            syncer = DriveSkillSync(_Drive(), destination=root / "sync")
            job = store.create_job(
                "drive.skill_sync",
                {
                    "worker": "drive-skill-sync",
                    "destination_root": str(root / "sync"),
                },
            )
            result = JobEngine(
                store,
                {"drive-skill-sync": DriveSkillSyncWorker(syncer)},
                {"drive.skill_sync": DriveSkillSyncVerifier()},
            ).run(job["id"])
            self.assertEqual(result["status"], "COMPLETE")
            evidence = store.list_evidence(job["id"])[-1]
            self.assertEqual(evidence["method"], "FILESYSTEM_EXACT_HASH_REREAD")
            self.assertTrue(evidence["authoritative"])

    def test_google_doc_html_parser_preserves_smart_chip_and_hyperlink(self):
        html = (
            b'<p>Read <a href="https://docs.google.com/document/d/abc123456789/edit">'
            b'Submission Center SOP</a></p>'
        )
        text = google_doc_html_to_markdown(html).decode("utf-8")
        self.assertIn("[Submission Center SOP](https://docs.google.com/document/d/abc123456789/edit)", text)

    def test_email_prompt_uses_synced_core_and_dynamic_submission_sop(self):
        with durable_temporary_directory() as td:
            base = Path(td)
            sync_root = base / "sync"
            DriveSkillSync(_Drive(), destination=sync_root).sync()
            calls: list[str] = []
            with patch.dict(
                os.environ,
                {"ROBIE_SKILL_SYNC_ROOT": str(sync_root)},
                clear=False,
            ):
                run_guarded_email_task(
                    db_path=str(base / "jobs.db"),
                    gmail_message_id="gmail-1",
                    prompt="Draft the Submission Center cleanup email.",
                    run_agent=lambda prompt: calls.append(prompt) or "drafted",
                )
            self.assertIn("Never expose credentials", calls[0])
            self.assertIn(DEFAULT_SUBMISSION_CENTER_SOP_URL, calls[0])
            self.assertNotIn("1yp" + "TB", calls[0])

    def test_sync_commands_route_to_bounded_worker_not_playwright(self):
        for text in ("sync skills", "update memory", "/sync-skills"):
            result = classify_request(text)
            self.assertEqual(result.action_type, "drive.skill_sync")
            self.assertEqual(result.worker, "drive-skill-sync")

    def test_sync_chat_intake_is_durable_with_explicit_video_exemption(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            with patch.dict(
                os.environ,
                {"ROBIE_SKILL_SYNC_ROOT": str(root / "sync")},
                clear=False,
            ):
                job_id = open_chat_job(
                    db,
                    "spaces/dm/messages/sync-1",
                    "sync skills",
                    conversation_id="spaces/dm",
                )
            self.assertIsNotNone(job_id)
            store = JobStore(db)
            self.assertEqual(store.get_job(job_id or "")["action_type"], "drive.skill_sync")
            exemption = store.get_checkpoint(job_id or "", "recording_exemption")
            self.assertIn("no browser UI", (exemption or {}).get("reason", ""))

    def test_admin_commands_use_sqlite_and_return_tables(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            store = JobStore(db)
            job = store.create_job(
                "drive.skill_sync", {"task_name": "Refresh approved skills"}
            )
            with patch.dict(
                os.environ,
                {"ROBIE_ARTIFACT_ROOT": str(root / "artifacts")},
                clear=False,
            ):
                pending = handle_admin_command(db, "show pending jobs")
                self.assertIn(job["id"][:8], pending or "")
                self.assertIn("| Job ID | Task Name | Status | Checkpoint |", pending or "")
                created = handle_admin_command(
                    db,
                    "/schedule Morning sync | drive.skill_sync | 0 8 * * 1-5 | "
                    "https://docs.google.com/document/d/abc123456789/edit",
                )
                self.assertIn("No Playwright run was launched", created or "")
                self.assertIn("independently verified", created or "")
                schedules = handle_admin_command(db, "/schedules")
                self.assertIn("Morning sync", schedules or "")
                self.assertIn("Weekdays @ 8:00 AM", schedules or "")
                cancelled = handle_admin_command(db, f"/cancel {job['id'][:8]}", actor="tester")
                self.assertIn("marked FAILED", cancelled or "")

    def test_cron_weekdays_and_parser_are_deterministic(self):
        next_at = next_cron_time(
            "0 8 * * 1-5",
            "America/New_York",
            now=datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(next_at, "2026-08-31T12:00:00+00:00")
        self.assertEqual(parse_admin_command("show pending jobs").name, "pending")
        self.assertEqual(
            parse_admin_command("Show me all my currently scheduled jobs").name,
            "schedules",
        )
        self.assertIsNone(parse_admin_command("please audit submissions"))

    def test_natural_chrome_restart_request_reports_existing_timer_without_job(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            with patch.dict(
                os.environ,
                {"ROBIE_ARTIFACT_ROOT": str(root / "artifacts")},
                clear=False,
            ):
                response = handle_admin_command(
                    db,
                    "Can we also add to this that we restart the chrome session? every day?",
                )
            self.assertIn("already scheduled", response or "")
            self.assertIn("3:30 AM Eastern", response or "")
            self.assertIn("did not create a duplicate", response or "")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0],
                    0,
                )

    def test_start_new_chrome_session_everyday_wording_is_admin_not_job(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            text = "can you add a cron job of starting a new chrome session everyday at 3:30"
            self.assertEqual(parse_admin_command(text).name, "chrome_restart_schedule")
            with patch.dict(
                os.environ,
                {"ROBIE_ARTIFACT_ROOT": str(root / "artifacts")},
                clear=False,
            ):
                response = handle_admin_command(db, text)
            self.assertIn("already scheduled", response or "")
            self.assertIn("3:30 AM Eastern", response or "")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_natural_daily_schedule_is_saved_and_freshly_reread(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            with patch.dict(
                os.environ,
                {
                    "ROBIE_ARTIFACT_ROOT": str(root / "artifacts"),
                    "ROBIE_SKILL_SYNC_ROOT": str(root / "sync"),
                },
                clear=False,
            ):
                response = handle_admin_command(
                    db,
                    "Please schedule skill sync every day at 8:15 AM",
                )
            self.assertIn("independently verified", response or "")
            self.assertIn("Daily @ 8:15 AM", response or "")
            rows = OperationsStore(
                db, str(root / "artifacts")
            ).list_recurring_jobs(enabled_only=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action_type"], "drive.skill_sync")
            self.assertEqual(rows[0]["cron_spec"], "15 8 * * *")
            self.assertEqual(rows[0]["parameters"]["worker"], "drive-skill-sync")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_natural_daily_schedule_without_time_asks_for_time_without_job(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            with patch.dict(
                os.environ,
                {"ROBIE_ARTIFACT_ROOT": str(root / "artifacts")},
                clear=False,
            ):
                response = handle_admin_command(db, "Please sync skills every day")
            self.assertIn("What time Eastern", response or "")
            self.assertIn("No Job or schedule was created", response or "")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_60_second_poller_enqueues_due_recurring_job_and_advances_it(self):
        with durable_temporary_directory() as td:
            root = Path(td)
            db = str(root / "jobs.db")
            ops = OperationsStore(db, str(root / "artifacts"))
            schedule = ops.ensure_recurring_job(
                "Hourly approved skill refresh",
                "drive.skill_sync",
                {
                    "worker": "drive-skill-sync",
                    "task_name": "Hourly approved skill refresh",
                    "destination_root": str(root / "sync"),
                },
                "@every 60m",
                "America/New_York",
                next_run_at="2020-01-01T00:00:00+00:00",
            )
            with patch.dict(
                os.environ,
                {
                    "ROBIE_ENABLE_EZLYNX_SESSION_REFRESH": "0",
                    "ROBIE_ARTIFACT_ROOT": str(root / "artifacts"),
                },
                clear=False,
            ), patch(
                "robie_job_engine.test_runtime.maybe_run_bounded_job",
                return_value=True,
            ):
                result = run_once(db)
            self.assertEqual(result["created"], 1)
            self.assertEqual(result["executed"], 1)
            updated = ops.get_recurring_job(schedule["id"])
            self.assertEqual(updated["next_run_at"], "2020-01-01T01:00:00+00:00")
            self.assertTrue(updated["last_job_id"])
            timer = (
                Path(__file__).resolve().parents[1]
                / "deploy/systemd/robie-scheduler.timer"
            ).read_text(encoding="utf-8")
            self.assertIn("OnUnitActiveSec=60", timer)


if __name__ == "__main__":
    unittest.main()
