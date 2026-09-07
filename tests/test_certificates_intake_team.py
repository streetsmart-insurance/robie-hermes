"""Certificates-specific routing tests using synthetic data only."""
import unittest
from pathlib import Path
from unittest.mock import Mock
import test_intake_phase1 as fixtures
from robie_job_engine.certificates_intake import CertificatesIntake
from robie_job_engine.intake_core import IntakeVerifier


class CertificatesTeamTests(unittest.TestCase):
    setUp = fixtures.IntakePhase1Tests.setUp

    def run_certificate(self, assignee='test-reviewer'):
        return CertificatesIntake(self.api, self.archive).perform(
            self.source, self.ids, assignee_id=assignee, due_at=self.due)

    def test_verified_team_member_receives_manual_upload_task(self):
        result = self.run_certificate()
        self.assertTrue(result.succeeded, result.error)
        self.assertIn('verified Certificates team member', result.destination['description'])
        self.assertTrue(result.destination['manual_upload_required'])
        self.assertTrue(IntakeVerifier(self.api).verify({}, {'destination': result.destination}).verified)

    def test_missing_membership_integration_preserves_source_and_holds(self):
        self.api.lookup_certificates_team_member = None
        result = self.run_certificate()
        self.assertFalse(result.succeeded)
        self.assertTrue(Path(result.detail['source_artifact']).is_file())
        self.assertEqual(self.api.writes, 0)

    def test_missing_ambiguous_wrong_or_nonmember_assignee_never_writes(self):
        valid = dict(user_id='test-reviewer', certificates_team_member=True)
        for rows in [fixtures.read(), fixtures.read(valid, valid),
                     fixtures.read(dict(valid, user_id='other')),
                     fixtures.read(dict(valid, certificates_team_member=False)),
                     fixtures.read(dict(user_id='test-reviewer'))]:
            self.api.lookup_certificates_team_member = Mock(return_value=rows)
            self.assertFalse(self.run_certificate().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_stale_or_partial_membership_holds(self):
        row = dict(user_id='test-reviewer', certificates_team_member=True)
        for result in [fixtures.read(row, authoritative=False), fixtures.read(row, complete=False)]:
            self.api.lookup_certificates_team_member = Mock(return_value=result)
            self.assertFalse(self.run_certificate().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_membership_does_not_replace_active_user_verification(self):
        self.api.assignees = fixtures.read(dict(user_id='test-reviewer', active=False))
        self.assertFalse(self.run_certificate().succeeded)
        self.assertFalse(self.run_certificate('').succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_duplicate_request_reuses_task_but_revoked_membership_holds(self):
        first = self.run_certificate()
        self.assertEqual(self.run_certificate().destination['task_id'], first.destination['task_id'])
        self.api.lookup_certificates_team_member = Mock(return_value=fixtures.read())
        self.assertFalse(self.run_certificate().succeeded)
        self.assertEqual(self.api.writes, 1)
