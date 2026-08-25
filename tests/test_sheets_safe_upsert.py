import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from robie_job_engine import sheets_sync


class SheetsSafeUpsertTests(unittest.TestCase):
    def test_service_uses_authorized_user_token_when_configured(self):
        credentials = MagicMock()
        credentials.scopes = list(sheets_sync.SCOPES)
        google = types.ModuleType("google")
        google_auth = types.ModuleType("google.auth")
        google.auth = google_auth
        oauth2 = types.ModuleType("google.oauth2")
        credentials_module = types.ModuleType("google.oauth2.credentials")
        credentials_class = MagicMock()
        credentials_class.from_authorized_user_file.return_value = credentials
        credentials_module.Credentials = credentials_class
        oauth2.credentials = credentials_module
        discovery = types.ModuleType("googleapiclient.discovery")
        discovery.build = MagicMock()
        googleapiclient = types.ModuleType("googleapiclient")
        googleapiclient.discovery = discovery
        modules = {
            "google": google,
            "google.auth": google_auth,
            "google.oauth2": oauth2,
            "google.oauth2.credentials": credentials_module,
            "googleapiclient": googleapiclient,
            "googleapiclient.discovery": discovery,
        }
        with patch.dict(os.environ, {"ROBIE_GOOGLE_TOKEN_FILE": "/secure/token.json"}), \
             patch.dict(sys.modules, modules):
            sheets_sync._service()

        credentials_class.from_authorized_user_file.assert_called_once_with(
            "/secure/token.json"
        )
        discovery.build.assert_called_once_with(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )

    def test_upsert_job_rows_preserves_unrelated_rows(self):
        values = MagicMock()
        values.get.return_value.execute.return_value = {
            "values": [
                ["old"] * 17 + ["unrelated-job"],
                ["old"] * 17 + ["target-job"],
            ]
        }
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value = values
        dashboard = {
            "jobs": [{
                "id": "target-job",
                "action_type": "deployment.smoke",
                "payload": {"task": "Live Test acceptance"},
                "status": "COMPLETE",
                "created_at": "2026-08-24T12:00:00+00:00",
                "updated_at": "2026-08-24T12:01:00+00:00",
                "completed_at": "2026-08-24T12:01:00+00:00",
            }]
        }
        operations = MagicMock()
        operations.dashboard_rows.return_value = dashboard
        with patch.object(sheets_sync, "_service", return_value=service), \
             patch.object(sheets_sync, "OperationsStore", return_value=operations):
            result = sheets_sync.upsert_job_rows("jobs.db", "sheet", ["target-job"])

        self.assertEqual({"jobs": 1, "updated": 1, "appended": 0}, result)
        request = values.batchUpdate.call_args.kwargs
        self.assertEqual("sheet", request["spreadsheetId"])
        self.assertEqual("Jobs!A7:Y7", request["body"]["data"][0]["range"])
        self.assertEqual("target-job", request["body"]["data"][0]["values"][0][17])
        self.assertEqual(1, len(request["body"]["data"]))

    def test_upsert_job_rows_appends_without_clearing_existing_rows(self):
        values = MagicMock()
        values.get.return_value.execute.return_value = {
            "values": [["old"] * 17 + ["unrelated-job"]]
        }
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value = values
        dashboard = {
            "jobs": [{
                "id": "new-job",
                "action_type": "deployment.smoke",
                "payload": {"task": "Live Test acceptance"},
                "status": "FAILED",
            }]
        }
        operations = MagicMock()
        operations.dashboard_rows.return_value = dashboard
        with patch.object(sheets_sync, "_service", return_value=service), \
             patch.object(sheets_sync, "OperationsStore", return_value=operations):
            result = sheets_sync.upsert_job_rows("jobs.db", "sheet", ["new-job"])

        self.assertEqual({"jobs": 1, "updated": 0, "appended": 1}, result)
        request = values.batchUpdate.call_args.kwargs
        self.assertEqual("Jobs!A7:Y7", request["body"]["data"][0]["range"])

    def test_sync_skips_missing_optional_tabs_without_dropping_jobs(self):
        values = MagicMock()
        values.get.return_value.execute.return_value = {"values": []}
        spreadsheets = MagicMock()
        spreadsheets.values.return_value = values
        spreadsheets.get.return_value.execute.return_value = {
            "sheets": [
                {"properties": {"title": title}}
                for title in (
                    "Dashboard", "Assignments", "Jobs", "Evidence",
                    "Schedules", "Artifacts",
                )
            ]
        }
        service = MagicMock()
        service.spreadsheets.return_value = spreadsheets
        operations = MagicMock()
        operations.claim_due_schedules.return_value = []
        operations.dashboard_rows.return_value = {
            "jobs": [{
                "id": "verified-job",
                "action_type": "deployment.smoke",
                "payload": {"task": "Production smoke"},
                "status": "COMPLETE",
            }],
            "evidence": [],
            "schedules": [],
            "artifacts": [],
            "releases": [],
            "reports": [],
            "recordings": [],
        }
        with patch.object(sheets_sync, "_service", return_value=service), \
             patch.object(sheets_sync, "OperationsStore", return_value=operations), \
             patch.object(sheets_sync, "JobStore", return_value=MagicMock()):
            result = sheets_sync.sync("jobs.db", "sheet")

        self.assertEqual(1, result["jobs"])
        request = values.batchUpdate.call_args.kwargs
        ranges = [item["range"] for item in request["body"]["data"]]
        self.assertIn("Jobs!A6", ranges)
        self.assertNotIn("Releases!A6", ranges)
        self.assertNotIn("Reports!A6", ranges)

    def test_publish_job_requires_exact_job_recording_and_evidence_readback(self):
        job = {
            "id": "verified-job",
            "action_type": "ezlynx.submission_audit",
            "payload": {"task": "Read-only Submission Center audit"},
            "status": "COMPLETE",
            "authoritative_evidence_count": 1,
            "recording_links": [{
                "segment": 1,
                "url": "https://drive.google.com/file/d/recording/view",
            }],
        }
        evidence = {
            "job_id": "verified-job",
            "verified": 1,
            "method": "EZLYNX_PLAYWRIGHT_FRESH_READBACK",
            "source": "EZLynx Submission Center",
            "authoritative": 1,
            "expected_json": "{}",
            "observed_json": "{}",
            "locator": "https://app.ezlynx.com/web/",
            "evidence_sha256": "evidence-digest",
            "captured_at": "2026-08-25T12:00:00+00:00",
            "created_at": "2026-08-25T12:00:00+00:00",
        }
        dashboard = {"jobs": [job], "evidence": [evidence]}
        expected_job_row = sheets_sync._friendly_jobs([job], limit=1)[0]
        evidence_row = [
            evidence[key] for key in (
                "job_id", "verified", "method", "source", "authoritative",
                "expected_json", "observed_json", "locator", "evidence_sha256",
                "captured_at", "created_at",
            )
        ]
        values = MagicMock()
        responses = (
            {"values": []},
            {"values": [expected_job_row]},
            {"values": [evidence_row]},
        )
        values.get.side_effect = [
            MagicMock(execute=MagicMock(return_value=response)) for response in responses
        ]
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value = values
        operations = MagicMock()
        operations.dashboard_rows.return_value = dashboard
        with patch.object(sheets_sync, "_service", return_value=service), patch.object(
            sheets_sync, "OperationsStore", return_value=operations
        ), patch.object(
            sheets_sync, "upsert_job_rows",
            return_value={"jobs": 1, "updated": 0, "appended": 1},
        ):
            result = sheets_sync.publish_job_to_control_center(
                "jobs.db", "sheet", "verified-job"
            )

        self.assertEqual(result["sheet_row"], 6)
        self.assertEqual(result["evidence_rows"], 1)
        self.assertIn("recording", result)
        values.batchUpdate.assert_called_once()

    def test_publish_complete_without_ready_recording_fails_closed(self):
        operations = MagicMock()
        operations.dashboard_rows.return_value = {
            "jobs": [{
                "id": "missing-recording",
                "action_type": "ezlynx.submission_audit",
                "payload": {},
                "status": "COMPLETE",
                "authoritative_evidence_count": 1,
                "recording_links": [],
            }],
            "evidence": [],
        }
        with patch.object(sheets_sync, "OperationsStore", return_value=operations), \
             self.assertRaisesRegex(RuntimeError, "READY recording link"):
            sheets_sync.publish_job_to_control_center(
                "jobs.db", "sheet", "missing-recording"
            )


if __name__ == "__main__":
    unittest.main()
