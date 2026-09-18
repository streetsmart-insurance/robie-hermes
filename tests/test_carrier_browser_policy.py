"""Regression tests: Hartford browser jobs fail closed on ROBIE servers.

Permanent rule (2026-09-18, Carlo): thehartford.com / EBC is unreachable from
the hermes-* fleet (proven by curl probes on hermes-poc-01, direct and via the
residential proxy; independently re-proven by Dusty). Hartford Playwright jobs
must refuse before starting, with a plain-English reason. Other carriers keep
the designed stealth+proxy path — the gate must not touch them.

Guards:
- a Hartford site URL in the job refuses on hermes-poc-01 and hermes-test-01
- "Hartford EBC" wording (no URL) also refuses — portal jobs count
- a bare "Hartford" name mention without portal context does NOT refuse
  (same principle as the 264a708f Ascend lesson: name-only is not evidence)
- non-Hartford carriers are unaffected on hermes-*
- Hartford jobs are unaffected off the hermes-* fleet (host-scoped gate)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robie_job_engine.action_gate import hold_reason_for_job, refuse_playwright_start
from robie_job_engine.carrier_browser_policy import (
    HARTFORD_REFUSAL,
    hartford_playwright_refusal,
    hartford_target_in_blob,
    is_hermes_server_host,
)


def _on(host: str):
    return patch("robie_job_engine.carrier_browser_policy.socket.gethostname", return_value=host)


class TestHostScope:
    def test_hermes_poc_is_server(self):
        assert is_hermes_server_host("hermes-poc-01") is True

    def test_hermes_test_is_server(self):
        assert is_hermes_server_host("hermes-test-01") is True

    def test_other_hosts_not_server(self):
        assert is_hermes_server_host("sandbox-vm-01") is False
        assert is_hermes_server_host("github-runner") is False


class TestTargetDetection:
    def test_site_url_detected(self):
        assert hartford_target_in_blob("open https://agency.thehartford.com/ebc") is True

    def test_portal_wording_detected(self):
        assert hartford_target_in_blob("download forms from Hartford EBC") is True
        assert hartford_target_in_blob("check the hartford portal") is True

    def test_bare_name_not_detected(self):
        # Name-only mention is not a browser job (264a708f principle).
        assert hartford_target_in_blob("the Hartford quote came back higher") is False
        assert hartford_target_in_blob("hartford") is False

    def test_other_carrier_not_detected(self):
        assert hartford_target_in_blob("open https://app.ezlynx.com/web/account/1") is False


class TestGate:
    def test_hartford_url_refused_on_poc(self):
        with _on("hermes-poc-01"):
            reason = hartford_playwright_refusal(
                code="page.goto('https://agency.thehartford.com/ebc')"
            )
        assert reason == HARTFORD_REFUSAL

    def test_hartford_ebc_wording_refused_on_test_box(self):
        with _on("hermes-test-01"):
            reason = hartford_playwright_refusal(
                text="Download the forms from Hartford EBC",
                payload={"carrier": "Hartford"},
            )
        assert reason == HARTFORD_REFUSAL

    def test_other_carrier_allowed_on_poc(self):
        with _on("hermes-poc-01"):
            reason = hartford_playwright_refusal(
                code="page.goto('https://app.ezlynx.com/web/account/123')"
            )
        assert reason is None

    def test_hartford_allowed_off_fleet(self):
        with _on("carlo-macbook"):
            reason = hartford_playwright_refusal(
                code="page.goto('https://agency.thehartford.com/ebc')"
            )
        assert reason is None

    def test_bare_name_allowed_on_poc(self):
        with _on("hermes-poc-01"):
            reason = hartford_playwright_refusal(text="the Hartford quote came back higher")
        assert reason is None


class TestGateWiring:
    def test_hold_reason_for_job_refuses_hartford(self):
        job = {"id": "job-1", "payload": {"url": "https://agency.thehartford.com/ebc/forms"}}
        with _on("hermes-poc-01"):
            reason = hold_reason_for_job(job)
        assert reason == HARTFORD_REFUSAL

    def test_refuse_playwright_start_refuses_hartford_code(self):
        with _on("hermes-poc-01"):
            reason = refuse_playwright_start("page.goto('https://www.thehartford.com/')")
        assert reason == HARTFORD_REFUSAL

    def test_refuse_playwright_start_allows_other_carrier(self):
        with _on("hermes-poc-01"):
            reason = refuse_playwright_start("page.goto('https://app.ezlynx.com/')")
        # No Hartford refusal; whatever the action gate decides otherwise is
        # its own business — it must not be the Hartford reason.
        assert reason != HARTFORD_REFUSAL
