"""Regression test for the 2026-10-01 task-verifier Gmail bug.

scripts/run_task_verifier.py::fetch_report_csv() called
adapter.search() and adapter.attachments(), which CertGmailAdapter never
had. Every 15-minute run failed with:

    AttributeError: 'CertGmailAdapter' object has no attribute 'search'

so the report fetch always failed and every task verification went
INCONCLUSIVE (Carlo got the Chat alert).

The test injects a fake adapter exposing the REAL CertGmailAdapter
interface (list_message_ids / get_full_message / get_attachment_bytes)
and asserts the CSV attachment is fetched. Against the old code this
test fails (AttributeError -> (None, "report fetch failed: ...")).

No live credentials, no network -- everything is local.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_script():
    path = os.path.join(REPO_ROOT, "scripts", "run_task_verifier.py")
    spec = importlib.util.spec_from_file_location("run_task_verifier", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_task_verifier"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeAdapter:
    """Exposes exactly the real CertGmailAdapter interface -- no search()."""

    def __init__(self):
        self.csv_bytes = b"task_id,subject,status\n1,hello,open\n"

    @classmethod
    def with_dwd(cls, mailbox=""):
        # The report lands in robie@, not the adapter's certificates@ default.
        assert mailbox == "robie@streetsmart.insurance", f"wrong mailbox: {mailbox!r}"
        return cls()

    def list_message_ids(self, query, page_token, page_size=50):
        assert 'subject:"ROBIE task report CSV"' in query
        return (["msg_abc"], None)

    def get_full_message(self, gmail_id):
        assert gmail_id == "msg_abc"
        now_ms = str(int(time.time() * 1000))
        return {
            "id": "msg_abc",
            "internalDate": now_ms,
            "payload": {
                "parts": [
                    {
                        "filename": "task_report.csv",
                        "mimeType": "text/csv",
                        "body": {"attachmentId": "att_1", "size": 42},
                    },
                    {
                        "filename": "",
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {
                                "filename": "nested_report.CSV",
                                "mimeType": "text/csv",
                                "body": {"attachmentId": "att_2", "size": 10},
                            }
                        ],
                    },
                ]
            },
        }

    def get_attachment_bytes(self, gmail_id, attachment_id):
        assert gmail_id == "msg_abc" and attachment_id in ("att_1", "att_2")
        return self.csv_bytes


def _install_fake_adapter():
    fake_mod = types.ModuleType("robie_job_engine.cert_gmail_adapter")
    fake_mod.CertGmailAdapter = _FakeAdapter
    sys.modules["robie_job_engine.cert_gmail_adapter"] = fake_mod


class FetchReportCsvTest(unittest.TestCase):
    def test_fetches_csv_via_real_adapter_interface(self):
        _install_fake_adapter()
        mod = _load_script()
        data, detail = mod.fetch_report_csv("ROBIE task report CSV")
        self.assertIsNotNone(data, f"expected CSV bytes, got detail={detail!r}")
        self.assertEqual(data, b"task_id,subject,status\n1,hello,open\n")
        self.assertTrue(
            "task_report.csv" in detail or "nested_report.CSV" in detail,
            f"unexpected detail={detail!r}",
        )

    def test_no_messages_returns_none(self):
        class _Empty(_FakeAdapter):
            def list_message_ids(self, query, page_token, page_size=50):
                return ([], None)

        fake_mod = types.ModuleType("robie_job_engine.cert_gmail_adapter")
        fake_mod.CertGmailAdapter = _Empty
        sys.modules["robie_job_engine.cert_gmail_adapter"] = fake_mod
        mod = _load_script()
        data, detail = mod.fetch_report_csv("ROBIE task report CSV")
        self.assertIsNone(data)
        self.assertIn("no email with subject", detail)


if __name__ == "__main__":
    unittest.main()
