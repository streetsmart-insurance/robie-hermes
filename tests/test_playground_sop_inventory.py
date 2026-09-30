"""SOP excludes from Carlo's Drive inventory, newest-copy dedupe, and stale notes."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from durable_temp import durable_temporary_directory

from robie_job_engine.playground_reply import sop_reply
from robie_job_engine.playground_sop import (
    collect_documents,
    dedupe_documents,
    exclude_list_path,
    guide_freshness_note,
    ingest_sops,
    is_excluded_drive_file,
    retrieve_sop,
    text_is_excluded,
)

WHEN = datetime(2026, 9, 30, tzinfo=timezone.utc)

EXCLUDED_NAMES = [
    "Broker of Record tracker",
    "Progressive BORs sheet",
    "Progressive quality report",
    "Policy_Master.xlsx",
    "Policy Master.csv",
    "Referrals Report",
    "Payroll calendar",
    "Performance/Quality & Incentives",
    "Quality and Incentives",
    "Ascend Finance draft",
    "QuickBooks export",
    "Applied Pay draft",
    "TenantCloud draft",
    "Agency Licenses",
    "Licensing & Document Management",
    "Guard call recordings",
    "robie-job-engine-operations-2026.tgz",
    "nightly-backup.zip",
    "bundle.tar.gz",
    "LOGIN_SECRETS.txt",
    "Carrier logins",
    "Zapier ZAP-012 setup",
    "Tech Stack Handbook",
    "EZLynx API keys",
    "Carrier login websites",
    "Book of business",
    "Lawsuit folder notes",
]

KEPT_NAMES = [
    "How to insure a truck",
    "How to read a policy",
    "Certificate holder steps",
]


class SopInventoryTests(unittest.TestCase):
    def test_exclude_list_is_one_file(self):
        path = exclude_list_path()
        self.assertEqual(path.name, "playground_sop_excludes.txt")
        text = path.read_text(encoding="utf-8")
        self.assertIn("Carlo can edit this file", text)
        self.assertIn("LOGIN_SECRETS", text)
        self.assertIn("zap-0", text)

    def test_inventory_names_and_archives_are_excluded(self):
        for name in EXCLUDED_NAMES:
            meta = {"name": name, "folder_label": "core"}
            self.assertTrue(is_excluded_drive_file(meta), name)
        for name in KEPT_NAMES:
            self.assertFalse(is_excluded_drive_file({"name": name}), name)
        self.assertTrue(
            is_excluded_drive_file(
                {"name": "notes.bin", "mimeType": "application/zip"}
            )
        )
        self.assertTrue(
            is_excluded_drive_file(
                {"name": "Handbook", "driveId": "0ANbwd0py5G63Uk9PVA"}
            )
        )
        self.assertTrue(text_is_excluded("The file says LOGIN_SECRETS=abc"))
        self.assertFalse(text_is_excluded("File the certificate the same day."))

    def test_newest_copy_and_same_title_win(self):
        files = [
            {
                "id": "old",
                "name": "How to file a cert",
                "modifiedTime": "2024-01-01T00:00:00Z",
            },
            {
                "id": "copy",
                "name": "Copy of How to file a cert",
                "modifiedTime": "2026-08-01T00:00:00Z",
            },
            {
                "id": "stale-title",
                "name": "Truck guide.pdf",
                "modifiedTime": "2020-01-01T00:00:00Z",
            },
            {
                "id": "fresh-title",
                "name": "Truck guide.pdf",
                "modifiedTime": "2026-03-01T00:00:00Z",
            },
        ]
        kept = {item["id"] for item in dedupe_documents(files)}
        self.assertEqual(kept, {"copy", "fresh-title"})

    def test_old_guide_adds_a_plain_note_and_a_new_one_does_not(self):
        old = guide_freshness_note("2024-06-01T00:00:00Z", now=WHEN)
        self.assertEqual(old, "(guide last updated 2024 — double-check)")
        recent = guide_freshness_note("2026-06-01T00:00:00Z", now=WHEN)
        self.assertEqual(recent, "")
        exact = guide_freshness_note("2025-09-30T00:00:00Z", now=WHEN)
        self.assertIn("2025", exact)
        hits = retrieve_sop(
            "How do we insure a truck?",
            [
                {
                    "doc_id": "1",
                    "title": "How to insure a truck",
                    "folder": "trucking",
                    "text": "Trucking uses the trucking guide.",
                    "modified": "2024-06-01T00:00:00Z",
                }
            ],
            now=WHEN,
        )
        reply = sop_reply(
            answer=hits[0].excerpt,
            source=hits[0].citation,
            job_id="job-1",
            freshness=hits[0].freshness_note,
        )
        self.assertIn("(guide last updated 2024 — double-check)", reply)
        self.assertIn("How to insure a truck", reply)
        fresh_hits = retrieve_sop(
            "How do we insure a truck?",
            [
                {
                    "doc_id": "2",
                    "title": "How to insure a truck",
                    "folder": "trucking",
                    "text": "Trucking uses the trucking guide.",
                    "modified": "2026-08-01T00:00:00Z",
                }
            ],
            now=WHEN,
        )
        self.assertEqual(fresh_hits[0].freshness_note, "")

    def test_ingest_drops_secret_text_and_keeps_the_modified_time(self):
        class Drive:
            def list_folder(self, folder_id: str):
                del folder_id
                return [
                    {
                        "id": "good",
                        "name": "How to insure a truck",
                        "mimeType": "text/plain",
                        "modifiedTime": "2024-06-01T00:00:00Z",
                        "folder_label": "core",
                    },
                    {
                        "id": "secret",
                        "name": "Morning checklist",
                        "mimeType": "text/plain",
                        "modifiedTime": "2026-01-01T00:00:00Z",
                        "folder_label": "core",
                    },
                    {
                        "id": "archive",
                        "name": "robie-job-engine-operations-1.tgz",
                        "mimeType": "application/gzip",
                        "folder_label": "core",
                    },
                    {
                        "id": "mentions",
                        "name": "How to read a policy",
                        "mimeType": "text/plain",
                        "modifiedTime": "2026-02-01T00:00:00Z",
                        "folder_label": "core",
                    },
                ]

            def export_text(self, file_id: str, mime: str) -> str:
                del mime
                if file_id == "secret":
                    return "Do not share LOGIN_SECRETS with anyone."
                if file_id == "mentions":
                    return "Do not put payroll numbers in the policy note."
                return "Trucking uses the trucking guide."

        with durable_temporary_directory() as tmp:
            index = str(Path(tmp) / "sop.json")
            with mock.patch.dict(
                os.environ,
                {"ROBIE_PLAYGROUND_SOP_FOLDERS": "core:folder-1"},
                clear=False,
            ):
                result = ingest_sops(Drive(), index_path=index)
                collected_names = {item["name"] for item in collect_documents(Drive())}
            index_text = Path(index).read_text(encoding="utf-8")
            payload = json.loads(index_text)
        self.assertEqual(result["count"], 2)
        kept = {item["doc_id"]: item for item in payload["documents"]}
        self.assertEqual(kept["good"]["modified"], "2024-06-01T00:00:00Z")
        self.assertIn("payroll", kept["mentions"]["text"])
        self.assertNotIn("LOGIN_SECRETS", index_text)
        self.assertNotIn("robie-job-engine-operations-1.tgz", collected_names)
