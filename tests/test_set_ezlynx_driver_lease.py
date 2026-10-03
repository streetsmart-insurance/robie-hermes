"""The driver-lease workflow must read project metadata and fail closed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "set_ezlynx_driver_lease.py"
WORKFLOW = ROOT / ".github" / "workflows" / "set-ezlynx-driver.yml"


def _describe(value: str | None) -> dict:
    items = []
    if value is not None:
        items.append({"key": "robie-ezlynx-driver", "value": value})
    return {"name": "streetsmart-hermes-poc", "commonInstanceMetadata": {"items": items}}


def _run(describe: object, *, action: str = "check-in", holder: str = "TEST") -> subprocess.CompletedProcess[str]:
    with durable_temporary_directory() as tmp:
        root = Path(tmp)
        describe_file = root / "project-info.json"
        lease_file = root / "driver.json"
        describe_file.write_text(json.dumps(describe) if not isinstance(describe, str) else describe, encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "ACTION": action,
                "HOLDER": holder,
                "TTL_MINUTES": "120",
                "ACTOR": "moe",
            }
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--describe-file",
                str(describe_file),
                "--lease-file",
                str(lease_file),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        proc.lease = json.loads(lease_file.read_text(encoding="utf-8")) if lease_file.is_file() else None  # type: ignore[attr-defined]
        return proc


class DriverLeaseReadTests(unittest.TestCase):
    def test_other_holder_in_is_refused_and_not_written(self):
        current = json.dumps({"version": 1, "state": "IN", "holder": "PRODUCTION"})
        proc = _run(_describe(current), holder="TEST")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("another environment is IN", proc.stderr)
        self.assertIsNone(proc.lease)

    def test_same_holder_in_renews(self):
        current = json.dumps({"version": 1, "state": "IN", "holder": "TEST"})
        proc = _run(_describe(current), holder="TEST")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.lease["state"], "IN")
        self.assertEqual(proc.lease["holder"], "TEST")

    def test_missing_key_allows_the_first_check_in(self):
        proc = _run(_describe(None), holder="PRODUCTION")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.lease["holder"], "PRODUCTION")
        self.assertEqual(proc.lease["state"], "IN")

    def test_malformed_lease_blocks_check_in(self):
        proc = _run(_describe("not-json"), action="check-in", holder="TEST")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("malformed", proc.stderr)
        self.assertIsNone(proc.lease)

    def test_check_out_replaces_a_malformed_lease(self):
        proc = _run(_describe("not-json"), action="check-out", holder="TEST")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.lease["state"], "OUT")
        self.assertEqual(proc.lease["holder"], "NONE")

    def test_check_out_clears_another_holders_lease(self):
        current = json.dumps({"version": 1, "state": "IN", "holder": "PRODUCTION"})
        proc = _run(_describe(current), action="check-out", holder="TEST")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.lease["state"], "OUT")

    def test_unreadable_describe_fails_closed(self):
        for describe in ("ERROR: unrecognized arguments: --filter", {"name": "partial"}):
            with self.subTest(describe=describe):
                proc = _run(describe, action="check-in", holder="TEST")
                self.assertEqual(proc.returncode, 1)
                self.assertIn("could not read robie-ezlynx-driver metadata", proc.stderr)
                self.assertIsNone(proc.lease)
                out = _run(describe, action="check-out", holder="TEST")
                self.assertEqual(out.returncode, 1)
                self.assertIsNone(out.lease)

    def test_workflow_reads_json_and_does_not_swallow_a_describe_error(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("gcloud compute project-info describe", text)
        self.assertIn("--format=json", text)
        self.assertIn("scripts/set_ezlynx_driver_lease.py", text)
        self.assertNotIn("--filter", text)
        self.assertNotIn("--flatten", text)
        self.assertNotIn("|| true", text)
        self.assertIn("refs/heads/main", text)
        self.assertIn("MOE_CONFIRMED_ONE_DRIVER", text)
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("another environment is IN", script)


if __name__ == "__main__":
    unittest.main()
