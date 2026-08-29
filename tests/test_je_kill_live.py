from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from durable_temp import durable_temporary_directory

from robie_job_engine.je_kill_live import (
    PHASES,
    PersistentChromeEzlynxPort,
    load_fixture,
    require_live_test_host,
)


def fixture_payload() -> dict:
    scenarios = {}
    for index, phase in enumerate(PHASES, start=1):
        scenarios[phase] = {
            "account_id": "test-account",
            "resource_id": f"test-document-{index}",
            "document_name": f"je-kill-{index}.pdf",
            "label_id": "test-label",
            "label": "JE-KILL-01",
            "action_url": f"https://test.ezlynx.com/web/document/{index}",
            "readback_url": f"https://test.ezlynx.com/web/document/{index}",
            "label_control": {"kind": "role", "value": "button", "name": "Labels"},
            "search_input": {"kind": "role", "value": "textbox", "name": "Search Labels"},
            "label_option": {"kind": "text", "value": "JE-KILL-01"},
            "apply_button": {"kind": "role", "value": "button", "name": "Apply"},
            "applied_label": {"kind": "text", "value": "JE-KILL-01"},
        }
    return {
        "test_only": True,
        "disposable": True,
        "approved_by": "Carlo Ferrara",
        "approval_scope": "JE-KILL-01",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": scenarios,
    }


class JeKillLiveGuardTests(unittest.TestCase):
    def _write(self, root: Path, payload: dict) -> Path:
        path = root / "fixture.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_fixture_requires_three_distinct_disposable_approved_test_records(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            fixture = load_fixture(self._write(root, fixture_payload()))
            self.assertEqual(set(fixture.scenarios), set(PHASES))
            self.assertEqual(
                len({item.resource_id for item in fixture.scenarios.values()}), 3
            )

            payload = fixture_payload()
            payload["disposable"] = False
            with self.assertRaisesRegex(ValueError, "disposable"):
                load_fixture(self._write(root, payload))

            payload = fixture_payload()
            payload["approved_by"] = ""
            with self.assertRaisesRegex(ValueError, "Carlo Ferrara"):
                load_fixture(self._write(root, payload))

            payload = fixture_payload()
            payload["scenarios"]["after_action"]["resource_id"] = "test-document-1"
            with self.assertRaisesRegex(ValueError, "distinct"):
                load_fixture(self._write(root, payload))

    def test_fixture_refuses_known_live_account_non_ezlynx_urls_and_positional_selectors(self):
        with durable_temporary_directory() as tmp:
            root = Path(tmp)
            payload = fixture_payload()
            payload["scenarios"]["before_action"]["account_id"] = "221398001"
            with self.assertRaisesRegex(ValueError, "forbidden account"):
                load_fixture(self._write(root, payload))

            payload = fixture_payload()
            payload["scenarios"]["before_action"]["action_url"] = "https://example.com/web/x"
            with self.assertRaisesRegex(ValueError, "ezlynx.com"):
                load_fixture(self._write(root, payload))

            payload = fixture_payload()
            payload["scenarios"]["before_action"]["applied_label"] = {
                "kind": "css",
                "value": ".label:nth-child(1)",
            }
            with self.assertRaisesRegex(ValueError, "positional"):
                load_fixture(self._write(root, payload))

    def test_host_guard_requires_test_env_and_exact_test_hostname(self):
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False), patch(
            "robie_job_engine.je_kill_live.socket.gethostname",
            return_value="hermes-test-01.c.internal",
        ):
            require_live_test_host()
        with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False), patch(
            "robie_job_engine.je_kill_live.socket.gethostname",
            return_value="hermes-test-01",
        ):
            with self.assertRaisesRegex(RuntimeError, "ROBIE_ENV=TEST"):
                require_live_test_host()
        with patch.dict(os.environ, {"ROBIE_ENV": "TEST"}, clear=False), patch(
            "robie_job_engine.je_kill_live.socket.gethostname",
            return_value="hermes-poc-01",
        ):
            with self.assertRaisesRegex(RuntimeError, "refuses host"):
                require_live_test_host()

    def test_port_fails_closed_on_expired_session_and_non_unique_readback(self):
        payload = fixture_payload()
        with durable_temporary_directory() as tmp:
            scenario = load_fixture(self._write(Path(tmp), payload)).scenarios["before_action"]

        class Locator:
            def __init__(self, count: int):
                self._count = count

            def count(self):
                return self._count

        class Page:
            url = "https://test.ezlynx.com/login"

            def get_by_role(self, *args, **kwargs):
                return Locator(1)

        port = PersistentChromeEzlynxPort(scenario, cdp_url="http://127.0.0.1:9222")
        port._page = Page()
        with self.assertRaisesRegex(RuntimeError, "AUTH_CHALLENGE"):
            port._assert_authenticated()
        with self.assertRaisesRegex(RuntimeError, "matched 2"):
            port._require_one(Locator(2), "applied label")

    def test_workflow_is_main_only_test_only_and_does_not_log_fixture(self):
        workflow = Path(".github/workflows/je-kill-test.yml").read_text(encoding="utf-8")
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("RUN_JE_KILL_01_ON_HERMES_TEST_01", workflow)
        self.assertIn("TEST_VM: hermes-test-01", workflow)
        self.assertIn("ROBIE_ENV=TEST", workflow)
        self.assertNotIn("cat \"${FIXTURE}\"", workflow)
        self.assertNotIn("hermes-poc-01", workflow)

    def test_workflow_transfers_runner_and_fails_closed_without_remote_sentinel(self):
        workflow = Path(".github/workflows/je-kill-test.yml").read_text(encoding="utf-8")
        self.assertIn("gcloud compute scp", workflow)
        self.assertIn("scripts/run-je-kill-test-remote.sh", workflow)
        self.assertIn("JE-KILL REMOTE VERIFIED", workflow)
        self.assertNotIn("bash -s", workflow)

        remote = Path("scripts/run-je-kill-test-remote.sh").read_text(encoding="utf-8")
        self.assertIn("JE-KILL Test inventory: 0 blocking Jobs/leases", remote)
        self.assertIn("run-je-kill-live-test.py", remote)
        self.assertIn("grep -Fq '\"result\": \"TEST VERIFIED\"'", remote)
        self.assertIn("JE-KILL REMOTE VERIFIED", remote)


if __name__ == "__main__":
    unittest.main()
