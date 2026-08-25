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


if __name__ == "__main__":
    unittest.main()
