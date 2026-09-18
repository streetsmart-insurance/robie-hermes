"""Exact-match EZLynx org-label selection. No network, no Playwright."""

from __future__ import annotations

import unittest
from unittest import mock

import pytest

from robie_job_engine import ezlynx_org_labels as labels
from robie_job_engine.ezlynx_write_scope import EzlynxWriteScopeError


class SelectUniqueOrgLabelTests(unittest.TestCase):
    def test_exact_ascend_noc_wins(self):
        record = labels.select_unique_org_label(
            [
                {"id": "1", "name": "Cancellation"},
                {"id": "2", "name": "Ascend NOC"},
                {"id": "3", "name": "Ascend noc"},
            ]
        )
        self.assertEqual(record["id"], "2")
        self.assertEqual(labels.label_name_of(record), "Ascend NOC")

    def test_case_or_whitespace_variants_do_not_match(self):
        with self.assertRaises(labels.OrgLabelError) as caught:
            labels.select_unique_org_label([{"id": "1", "name": "Ascend NOC "}])
        self.assertEqual(caught.exception.code, labels.LABEL_NOT_FOUND)

    def test_bare_cancellation_is_refused_as_required_name(self):
        with self.assertRaises(labels.OrgLabelError) as caught:
            labels.select_unique_org_label(
                [{"id": "1", "name": "Cancellation"}],
                required_name="Cancellation",
            )
        self.assertEqual(caught.exception.code, labels.LABEL_REFUSED)

    def test_two_exact_matches_are_not_guessed(self):
        with self.assertRaises(labels.OrgLabelError) as caught:
            labels.select_unique_org_label(
                [
                    {"id": "a", "name": "Ascend NOC"},
                    {"id": "b", "Name": "Ascend NOC"},
                ]
            )
        self.assertEqual(caught.exception.code, labels.LABEL_NOT_UNIQUE)


def test_apply_planned_label_dry_run_writes_nothing():
    class Client:
        def apply_applicant_organization_label(self, *args, **kwargs):
            raise AssertionError("dry-run must not apply")

    plan = {"name": "Ascend NOC", "id": "noc-1"}
    result = labels.apply_planned_label(
        Client(), "220250093", plan, dry_run=True
    )
    assert result["status"] == "dry_run"
    assert result["label_name"] == "Ascend NOC"
    assert result["method"] == "api"


def test_apply_planned_label_refuses_cancellation_name():
    class Client:
        def apply_applicant_organization_label(self, *args, **kwargs):
            raise AssertionError("must not apply")

    with pytest.raises(labels.OrgLabelError) as caught:
        labels.apply_planned_label(
            Client(),
            "220250093",
            {"name": "Cancellation", "id": "x"},
            dry_run=False,
        )
    assert caught.value.code == labels.LABEL_REFUSED


def test_apply_planned_label_enforces_write_scope():
    class Client:
        def apply_applicant_organization_label(self, *args, **kwargs):
            raise AssertionError("must not apply")

    with pytest.raises(EzlynxWriteScopeError):
        with mock.patch(
            "robie_job_engine.ezlynx_org_labels.require_allowed_ezlynx_write_applicant",
            side_effect=EzlynxWriteScopeError("refused"),
        ):
            labels.apply_planned_label(
                Client(),
                "999999999",
                {"name": "Ascend NOC", "id": "noc-1"},
                dry_run=False,
            )
