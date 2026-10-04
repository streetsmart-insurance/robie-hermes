"""create_task must not report success when the Zapier hook never ran.

Production recorded EZLynx tasks as created after
``No module named 'src.ezlynx'``. These tests pin the honest result and
the ascend-sync rule: a failed create stays pending and raises the Chat alert.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from robie_job_engine.ascend_sync import AscendEZLynxSyncManager, AscendSyncStore
from robie_job_engine.ezlynx_note_poster import EZLynxAgreementPoster

_SUCCESS_CLIENT = """
class EZLynxApiClient:
    def create_user_task(self, applicant_id, title, description, assigned_user=None, due_days_out=0):
        return {"status": "success", "task_id": "zap-77", "via": "zapier"}
"""

_HTTP_ERROR_CLIENT = """
import urllib.error

class EZLynxApiClient:
    def create_user_task(self, **kwargs):
        raise urllib.error.HTTPError(
            "https://hooks.zapier.com/hooks/catch/SECRETVALUE/1",
            502,
            "Bad Gateway",
            hdrs=None,
            fp=None,
        )
"""

_HTTP_RESULT_CLIENT = """
class EZLynxApiClient:
    def create_user_task(self, **kwargs):
        return {"status": "error", "reason": "HTTP 500 from Zapier hook", "via": "zapier"}
"""

_MISSING_HOOK_CLIENT = """
class EZLynxApiClient:
    pass
"""

_BROKEN_HOOK_CLIENT = """
class EZLynxApiClient:
    def create_user_task(self, **kwargs):
        raise RuntimeError("zapier hook missing at https://hooks.zapier.com/hooks/catch/SECRETVALUE/1")
"""


def _write_client(root: Path, source: str) -> None:
    package = root / "src" / "ezlynx"
    package.mkdir(parents=True)
    (root / "src" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "api_client.py").write_text(source)


def _cancellation_api() -> MagicMock:
    api = MagicMock()
    api.fetch_cancelation_returns.return_value = [
        {
            "id": "cancel-honesty-1",
            "unearned_premium_cents": 100,
            "unearned_commission_cents": 0,
            "unearned_surplus_lines_tax_cents": 0,
            "billable": {"id": "bill-honesty-1", "cancelation_effective_date": "2026-07-30"},
            "cancelation_docs": [],
        }
    ]
    api.fetch_billable.return_value = {
        "policy_number": "POL-HONESTY",
        "program_id": "prog-honesty",
        "carrier": {"title": "Test Carrier"},
        "coverage_type": {"title": "General Liability"},
    }
    api.fetch_program.return_value = {
        "insured": {"business_name": "Honesty LLC"},
        "producer": {"first_name": "Ada", "last_name": "Lovelace"},
    }
    api.fetch_invoices.return_value = []
    api.fetch_programs.return_value = []
    api.fetch_payouts.return_value = []
    return api


def _manager(store: AscendSyncStore, poster: EZLynxAgreementPoster) -> AscendEZLynxSyncManager:
    matcher = MagicMock()
    matcher.match_account.return_value = ("app-honesty", "Ada Lovelace")
    poster.post_custom_note = MagicMock(return_value={"status": "filed", "note_id": "note-1"})
    return AscendEZLynxSyncManager(
        api_client=_cancellation_api(),
        store=store,
        matcher=matcher,
        poster=poster,
        quickbooks_client=MagicMock(),
    )


class TestCreateTaskHonesty(unittest.TestCase):
    def test_import_failure_returns_error_and_does_not_mark_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-such-renewal-tree"
            missing.mkdir()
            poster = EZLynxAgreementPoster(renewal_root=str(missing))
            result = poster.create_task(
                applicant_id="app-1",
                title="dry run",
                description="import broken on purpose",
            )
            self.assertEqual(result["status"], "error")
            self.assertIn("not found", result["reason"])
            self.assertNotIn("SECRET", result["reason"])
            self.assertNotEqual(result.get("status"), "success")

            store = AscendSyncStore(Path(tmp) / "sync.db")
            manager = _manager(store, poster)
            # #747 disabled scheduled sync; it must refuse and mark nothing
            # done. The pending-on-failure contract now lives on the
            # ReliableDelivery path (tests/test_ascend_destinations.py).
            with self.assertRaisesRegex(RuntimeError, "ASCEND_SYNC_DISABLED"):
                manager.sync_once()
            self.assertFalse(store.is_event_processed("cancel-honesty-1"))

            # The hermes accountability package is still the ``src`` on this path.
            import src

            self.assertIn("accountability", src.__doc__ or "")
            with self.assertRaises(ModuleNotFoundError):
                import src.ezlynx  # noqa: F401

    def test_http_failure_returns_error_and_does_not_mark_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "renewal"
            _write_client(root, _HTTP_ERROR_CLIENT)
            poster = EZLynxAgreementPoster(renewal_root=str(root))
            result = poster.create_task("app-1", "title", "body")
            self.assertEqual(result["status"], "error")
            self.assertIn("HTTP 502", result["reason"])
            self.assertNotIn("SECRETVALUE", result["reason"])

            store = AscendSyncStore(Path(tmp) / "sync.db")
            manager = _manager(store, poster)
            # #747 disabled scheduled sync; it must refuse and mark nothing
            # done. The pending-on-failure contract now lives on the
            # ReliableDelivery path (tests/test_ascend_destinations.py).
            with self.assertRaisesRegex(RuntimeError, "ASCEND_SYNC_DISABLED"):
                manager.sync_once()
            self.assertFalse(store.is_event_processed("cancel-honesty-1"))

    def test_http_error_dict_is_not_rewritten_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "renewal"
            _write_client(root, _HTTP_RESULT_CLIENT)
            poster = EZLynxAgreementPoster(renewal_root=str(root))
            result = poster.create_task("app-1", "title", "body")
            self.assertEqual(result["status"], "error")
            self.assertIn("HTTP 500", result["reason"])
            self.assertNotEqual(result["status"], "success")

    def test_success_passthrough_is_returned_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "renewal"
            _write_client(root, _SUCCESS_CLIENT)
            before_path = list(sys.path)
            poster = EZLynxAgreementPoster(renewal_root=str(root))
            result = poster.create_task(
                applicant_id="app-1",
                title="Bind",
                description="ready",
                assigned_user="Ada",
                due_days_out=0,
            )
            self.assertEqual(
                result,
                {"status": "success", "task_id": "zap-77", "via": "zapier"},
            )
            self.assertEqual(sys.path, before_path)

            store = AscendSyncStore(Path(tmp) / "sync.db")
            manager = _manager(store, poster)
            # #747 disabled scheduled sync; it must refuse and mark nothing
            # done. The pending-on-failure contract now lives on the
            # ReliableDelivery path (tests/test_ascend_destinations.py).
            with self.assertRaisesRegex(RuntimeError, "ASCEND_SYNC_DISABLED"):
                manager.sync_once()
            self.assertFalse(store.is_event_processed("cancel-honesty-1"))

            import src

            self.assertIn("accountability", src.__doc__ or "")
            with self.assertRaises(ModuleNotFoundError):
                import src.ezlynx  # noqa: F401

    def test_env_root_is_used_instead_of_cwd_src(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "renewal"
            _write_client(root, _SUCCESS_CLIENT)
            with patch.dict(os.environ, {"ROBIE_RENEWAL_AUTOMATION_ROOT": str(root)}):
                poster = EZLynxAgreementPoster()
                result = poster.create_task("app-9", "t", "d")
            self.assertEqual(result["task_id"], "zap-77")
            self.assertEqual(result["status"], "success")

    def test_missing_hook_and_other_exceptions_return_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_hook = Path(tmp) / "missing-hook"
            _write_client(missing_hook, _MISSING_HOOK_CLIENT)
            missing = EZLynxAgreementPoster(renewal_root=str(missing_hook)).create_task(
                "app-1", "t", "d"
            )
            self.assertEqual(missing["status"], "error")
            self.assertIn("missing", missing["reason"].lower())

            broken = Path(tmp) / "broken-hook"
            _write_client(broken, _BROKEN_HOOK_CLIENT)
            failed = EZLynxAgreementPoster(renewal_root=str(broken)).create_task(
                "app-1", "t", "d"
            )
            self.assertEqual(failed["status"], "error")
            self.assertIn("[redacted]", failed["reason"])
            self.assertNotIn("SECRETVALUE", failed["reason"])


if __name__ == "__main__":
    unittest.main()
