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

    def test_note_labels_path_is_portal_notes_endpoint(self):
        self.assertEqual(
            labels.note_labels_path("1128873902"),
            "/EZLynxPortalAPI/Notes/1128873902/OrganizationLabels",
        )


def test_apply_planned_label_dry_run_writes_nothing():
    class Client:
        def apply_note_organization_label(self, *args, **kwargs):
            raise AssertionError("dry-run must not apply")

    plan = {"name": "Ascend NOC", "id": "noc-1"}
    result = labels.apply_planned_label(
        Client(), "220250093", plan, dry_run=True
    )
    assert result["status"] == "dry_run"
    assert result["label_name"] == "Ascend NOC"
    assert result["method"] == "api"
    assert result["auth_path"] == labels.AUTH_PATH_CDP_SESSION
    assert result["endpoint"] == labels.NOTE_LABELS_PATH


def test_apply_planned_label_refuses_cancellation_name():
    class Client:
        def apply_note_organization_label(self, *args, **kwargs):
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
        def apply_note_organization_label(self, *args, **kwargs):
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
                note_id="1128873902",
            )


class ApplyPlannedLabelSessionTests(unittest.TestCase):
    def test_session_success_uses_note_path(self):
        class Client:
            def __init__(self):
                self.calls = []

            def apply_note_organization_label(self, note_id, label_id):
                self.calls.append({"note_id": note_id, "label_id": label_id})

            def apply_applicant_organization_label(self, *args, **kwargs):
                raise AssertionError("OAuth applicant path must not run")

        client = Client()
        result = labels.apply_planned_label(
            client,
            "220250093",
            {"name": "Ascend NOC", "id": "110248"},
            dry_run=False,
            note_id="1128873902",
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["auth_path"], labels.AUTH_PATH_CDP_SESSION)
        self.assertEqual(result["endpoint"], labels.NOTE_LABELS_PATH)
        self.assertEqual(result["note_id"], "1128873902")
        self.assertEqual(result["label_id"], "110248")
        self.assertEqual(client.calls, [{"note_id": "1128873902", "label_id": "110248"}])

    def test_http_403_is_fail_closed(self):
        class Client:
            def apply_note_organization_label(self, *args, **kwargs):
                exc = RuntimeError("forbidden")
                exc.status = 403
                raise exc

            def apply_applicant_organization_label(self, *args, **kwargs):
                raise AssertionError("must not fall back to OAuth applicant apply")

        with self.assertRaises(labels.OrgLabelError) as caught:
            labels.apply_planned_label(
                Client(),
                "220250093",
                {"name": "Ascend NOC", "id": "110248"},
                dry_run=False,
                note_id="1128873902",
            )
        self.assertEqual(caught.exception.code, labels.LABEL_APPLY_FAILED)
        self.assertIn("403", str(caught.exception))

    def test_live_without_note_id_is_fail_closed(self):
        class Client:
            def apply_note_organization_label(self, *args, **kwargs):
                raise AssertionError("must not apply without a note id")

        with self.assertRaises(labels.OrgLabelError) as caught:
            labels.apply_planned_label(
                Client(),
                "220250093",
                {"name": "Ascend NOC", "id": "noc-1"},
                dry_run=False,
            )
        self.assertEqual(caught.exception.code, labels.LABEL_APPLY_FAILED)
        self.assertIn("note", str(caught.exception).casefold())
