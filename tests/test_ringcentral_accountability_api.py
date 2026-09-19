from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from robie_job_engine.ringcentral_accountability_api import (
    RingCentralApiEvidenceError,
    collect_ringcentral_api_evidence,
)
from robie_job_engine.ringcentral_client import RingCentralClient


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _extension_page(next_uri=None):
    navigation = {"nextPage": {"uri": next_uri}} if next_uri else {}
    return {
        "records": [
            {"id": "101", "extensionNumber": "101", "name": "Alex Example"},
            {"id": "201", "extensionNumber": "201", "name": "Service Test Queue"},
        ],
        "navigation": navigation,
    }


def _call_page(next_uri=None):
    navigation = {"nextPage": {"uri": next_uri}} if next_uri else {}
    return {
        "records": [
            {
                "id": "record-1",
                "sessionId": "session-1",
                "startTime": "2026-09-18T14:00:00.000Z",
                "direction": "Inbound",
                "result": "Accepted",
                "from": {"phoneNumber": "+12025550111", "name": "Caller"},
                "to": {"phoneNumber": "+12025550112", "name": "Service Test Queue"},
                "legs": [
                    {
                        "startTime": "2026-09-18T14:00:00.000Z",
                        "direction": "Inbound",
                        "result": "Accepted",
                        "duration": 42,
                        "from": {"phoneNumber": "+12025550111", "name": "Caller"},
                        "to": {"phoneNumber": "+12025550112", "name": "Service Test Queue"},
                        "extension": {"id": "101"},
                    }
                ],
            }
        ],
        "navigation": navigation,
    }


def test_collects_paginated_detailed_api_evidence(tmp_path: Path):
    extension_next = "https://platform.ringcentral.com/extensions-page-2"
    call_next = "https://platform.ringcentral.com/calls-page-2"
    responses = [
        Response(_extension_page(extension_next)),
        Response({"records": [], "navigation": {}}),
        Response(_call_page(call_next)),
        Response({"records": [], "navigation": {}}),
    ]
    client = RingCentralClient(access_token="test-token")
    with patch(
        "robie_job_engine.ringcentral_accountability_api.urllib.request.urlopen",
        side_effect=responses,
    ) as request:
        output = collect_ringcentral_api_evidence(
            client,
            output_path=tmp_path / "ringcentral.json",
            report_kind="daily",
            target_date=date(2026, 9, 18),
            required_users=["Alex Example"],
            required_queues=["Service Test Queue"],
            required_queue_members={"Service Test Queue": ["Alex Example"]},
        )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert request.call_count == 4
    assert data["complete"] is True
    assert data["pages"] == {"extensions": 2, "calls": 2}
    assert data["record_counts"] == {"extensions": 2, "sessions": 1, "legs": 1}
    assert data["calls"][0]["Session Id"] == "session-1"
    assert data["calls"][0]["Result"] == "Call connected"
    assert data["calls"][0]["Queue"] == "Service Test Queue"
    assert len(data["evidence_sha256"]) == 64


def test_fails_closed_when_current_user_is_missing(tmp_path: Path):
    client = RingCentralClient(access_token="test-token")
    with patch(
        "robie_job_engine.ringcentral_accountability_api.urllib.request.urlopen",
        return_value=Response(_extension_page()),
    ):
        with pytest.raises(RingCentralApiEvidenceError, match="missing current users"):
            collect_ringcentral_api_evidence(
                client,
                output_path=tmp_path / "ringcentral.json",
                report_kind="daily",
                target_date=date(2026, 9, 18),
                required_users=["Missing Example"],
                required_queues=["Service Test Queue"],
                required_queue_members={"Service Test Queue": ["Missing Example"]},
            )


def test_fails_closed_on_pagination_loop(tmp_path: Path):
    loop = "https://platform.ringcentral.com/loop"
    client = RingCentralClient(access_token="test-token")
    with patch(
        "robie_job_engine.ringcentral_accountability_api.urllib.request.urlopen",
        side_effect=[Response(_extension_page(loop)), Response(_extension_page(loop))],
    ):
        with pytest.raises(RingCentralApiEvidenceError, match="pagination loop"):
            collect_ringcentral_api_evidence(
                client,
                output_path=tmp_path / "ringcentral.json",
                report_kind="daily",
                target_date=date(2026, 9, 18),
                required_users=["Alex Example"],
                required_queues=["Service Test Queue"],
                required_queue_members={"Service Test Queue": ["Alex Example"]},
            )


def test_refuses_weekly_until_analytics_reconciliation_is_certified(tmp_path: Path):
    client = RingCentralClient(access_token="test-token")
    with pytest.raises(RingCentralApiEvidenceError, match="daily reports only"):
        collect_ringcentral_api_evidence(
            client,
            output_path=tmp_path / "ringcentral.json",
            report_kind="weekly",
            target_date=date(2026, 9, 18),
            required_users=["Alex Example"],
            required_queues=["Service Test Queue"],
            required_queue_members={"Service Test Queue": ["Alex Example"]},
        )
