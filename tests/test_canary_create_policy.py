import json
import pytest
from unittest.mock import patch, MagicMock

import scripts.canary_create_policy as canary_module
from scripts.canary_create_policy import (
    execute_canary_create,
    CANARY_APPLICANT_ID,
)


def test_rejects_unauthorized_applicant():
    with pytest.raises(ValueError, match="applicant_id must be 220250093"):
        execute_canary_create(
            origin="https://app.ezlynx.com",
            token="fake-token",
            applicant_id="999999999",
            policy_number="TEST-CANARY-20260912-01",
        )


def test_rejects_invalid_policy_number_format():
    with pytest.raises(ValueError, match="policy_number must match"):
        execute_canary_create(
            origin="https://app.ezlynx.com",
            token="fake-token",
            applicant_id=CANARY_APPLICANT_ID,
            policy_number="INVALID-POLICY-NUMBER",
        )


def test_refuses_when_policy_already_exists():
    with patch.object(canary_module, "oauth_request") as mock_req:
        # 1. lob-codes -> 200 []
        # 2. writing-companies -> 200 []
        # 3. search -> 200 [{"PolicyNumber": "TEST-CANARY-20260912-01"}]
        mock_req.side_effect = [
            (200, {}, "[]", []),
            (200, {}, "[]", []),
            (200, {}, '[{"PolicyNumber": "TEST-CANARY-20260912-01"}]', [{"PolicyNumber": "TEST-CANARY-20260912-01"}]),
        ]

        report = execute_canary_create(
            origin="https://app.ezlynx.com",
            token="fake-token",
            applicant_id=CANARY_APPLICANT_ID,
            policy_number="TEST-CANARY-20260912-01",
        )

        assert report["verdict"] == "REFUSED_ALREADY_EXISTS"
        assert report["create_attempt_count"] == 0
        assert report["create_call"] is None
        # Must have stopped after search without calling create
        assert mock_req.call_count == 3


def test_single_create_attempt_on_error():
    with patch.object(canary_module, "oauth_request") as mock_req:
        # 1. lob-codes -> 200 [{"code": "HOME", "name": "Homeowners"}]
        # 2. writing-companies -> 200 [{"writingCompanyId": "10048"}]
        # 3. search -> 200 [] (not found)
        # 4. create -> 400 Bad Request
        mock_req.side_effect = [
            (200, {}, '[{"code": "HOME", "name": "Homeowners"}]', [{"code": "HOME", "name": "Homeowners"}]),
            (200, {}, '[{"writingCompanyId": "10048"}]', [{"writingCompanyId": "10048"}]),
            (200, {}, "[]", []),
            (400, {"content-type": "application/json"}, '{"error": "Invalid carrier type"}', {"error": "Invalid carrier type"}),
        ]

        report = execute_canary_create(
            origin="https://app.ezlynx.com",
            token="fake-token",
            applicant_id=CANARY_APPLICANT_ID,
            policy_number="TEST-CANARY-20260912-01",
        )

        assert report["verdict"] == "CREATE_FAILED"
        assert report["create_attempt_count"] == 1
        assert report["create_call"]["response_status"] == 400
        assert report["create_call"]["response_body_json"] == {"error": "Invalid carrier type"}
        assert report["read_back"] is None
        # Exactly 4 calls, no retries or permutations
        assert mock_req.call_count == 4


def test_success_read_back_extracts_carrier_literally():
    with patch.object(canary_module, "oauth_request") as mock_req:
        # 1. lob-codes -> 200 [{"code": "HOME", "name": "Homeowners"}]
        # 2. writing-companies -> 200 [{"writingCompanyId": "10048"}]
        # 3. pre-search -> 200 [] (not found)
        # 4. create -> 200 "83651799"
        # 5. post-search -> 200 [{"PolicyNumber": "TEST-CANARY-20260912-01", "Carrier": "13585", "policy_id": "83651799"}]
        mock_req.side_effect = [
            (200, {}, '[{"code": "HOME", "name": "Homeowners"}]', [{"code": "HOME", "name": "Homeowners"}]),
            (200, {}, '[{"writingCompanyId": "10048"}]', [{"writingCompanyId": "10048"}]),
            (200, {}, "[]", []),
            (200, {}, '"83651799"', 83651799),
            (200, {}, '[{"PolicyNumber": "TEST-CANARY-20260912-01", "Carrier": "13585", "policy_id": "83651799"}]', [
                {"PolicyNumber": "TEST-CANARY-20260912-01", "Carrier": "13585", "policy_id": "83651799"}
            ]),
        ]

        report = execute_canary_create(
            origin="https://app.ezlynx.com",
            token="fake-token",
            applicant_id=CANARY_APPLICANT_ID,
            policy_number="TEST-CANARY-20260912-01",
        )

        assert report["verdict"] == "SUCCESS"
        assert report["create_attempt_count"] == 1
        assert report["created_policy_id"] == "83651799"
        assert report["read_back"]["status"] == 200
        assert report["read_back"]["found"] is True
        assert report["read_back"]["carrier_literal"] == "13585"
        assert mock_req.call_count == 5
