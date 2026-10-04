from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from robie_job_engine.ezlynx_driver_gate import (
    EzlynxDriverGateRefused,
    check_driver_gate,
    require_driver_in,
)


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def lease(holder: str, *, state: str = "IN", expires: str = "2026-10-01T13:00:00Z") -> str:
    return json.dumps({"version": 1, "state": state, "holder": holder, "expires_at": expires})


class DriverGateTests(unittest.TestCase):
    def test_production_accepts_only_live_production_lease(self):
        decision = check_driver_gate(
            hostname="hermes-poc-01", now=NOW, reader=lambda: lease("PRODUCTION")
        )
        self.assertTrue(decision.allowed)

    def test_test_refuses_production_lease(self):
        decision = check_driver_gate(
            hostname="hermes-test-01", now=NOW, reader=lambda: lease("PRODUCTION")
        )
        self.assertFalse(decision.allowed)
        self.assertIn("PRODUCTION", decision.reason)

    def test_expired_out_and_malformed_leases_fail_closed(self):
        cases = (
            lambda: lease("TEST", expires="2026-10-01T11:59:59Z"),
            lambda: lease("TEST", state="OUT"),
            lambda: "not json",
            lambda: json.dumps({"version": 2, "state": "IN", "holder": "TEST", "expires_at": "2026-10-01T13:00:00Z"}),
        )
        for reader in cases:
            with self.subTest(reader=reader):
                self.assertFalse(
                    check_driver_gate(
                        hostname="hermes-test-01", now=NOW, reader=reader
                    ).allowed
                )

    def test_known_host_metadata_failure_raises_named_refusal(self):
        with self.assertRaisesRegex(EzlynxDriverGateRefused, "EZLYNX_DRIVER_NOT_IN"):
            require_driver_in(
                hostname="hermes-poc-01",
                now=NOW,
                reader=lambda: (_ for _ in ()).throw(OSError("metadata down")),
            )

    def test_ci_host_is_unmanaged_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            decision = check_driver_gate(
                hostname="github-runner", now=NOW, reader=lambda: "unused"
            )
        self.assertTrue(decision.allowed)

    def test_ci_can_force_fail_closed(self):
        with patch.dict(
            os.environ,
            {
                "ROBIE_EZLYNX_DRIVER_GATE_REQUIRED": "1",
                "ROBIE_EZLYNX_DRIVER_HOLDER": "TEST",
            },
            clear=True,
        ):
            decision = check_driver_gate(
                hostname="github-runner", now=NOW, reader=lambda: lease("PRODUCTION")
            )
        self.assertFalse(decision.allowed)


if __name__ == "__main__":
    unittest.main()
