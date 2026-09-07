"""Synthetic Hello ownership scenarios; no live destinations."""
import unittest
from pathlib import Path
from unittest.mock import Mock
import test_intake_phase1 as fixtures
from robie_job_engine.hello_intake import HelloIntake
from robie_job_engine.intake_core import Identifiers, IntakeVerifier


class HelloRoutingTests(unittest.TestCase):
    setUp = fixtures.IntakePhase1Tests.setUp

    def run_hello(self, kind='new_business', **kwargs):
        return HelloIntake(self.api, self.archive).perform(
            self.source, kwargs.pop('identifiers', self.ids), due_at=self.due,
            request_type=kind, **kwargs)

    def test_each_type_selects_required_owner_and_readback_verifies(self):
        for kind, role in [('new_business', 'originating_producer'), ('renewal', 'applicable_csr'), ('midterm', 'applicable_csr')]:
            with self.subTest(kind=kind):
                self.api.tasks.clear()
                self.api.lookup_hello_owner = Mock(return_value=fixtures.read(dict(
                    applicant_id='220250093', policy_id='test-policy', role=role, user_id='test-reviewer')))
                result = self.run_hello(kind)
                self.assertTrue(result.succeeded, result.error)
                self.api.lookup_hello_owner.assert_called_once_with('220250093', 'test-policy', kind)
                self.assertIn(role, result.destination['description'])
                self.assertTrue(IntakeVerifier(self.api).verify({}, {'destination': result.destination}).verified)

    def test_unconfirmed_or_phase2_code_is_held_with_original(self):
        for kind in ['', 'AI', 'unknown']:
            result = self.run_hello(kind)
            self.assertFalse(result.succeeded)
            self.assertTrue(Path(result.detail['source_artifact']).is_file())
        self.assertEqual(self.api.writes, 0)

    def test_service_requires_policy(self):
        for kind in ['renewal', 'midterm']:
            self.assertFalse(self.run_hello(kind, identifiers=Identifiers(applicant_id='220250093')).succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_owner_absent_ambiguous_stale_or_incomplete_holds(self):
        row = dict(applicant_id='220250093', policy_id='test-policy', role='originating_producer', user_id='test-reviewer')
        for result in [fixtures.read(), fixtures.read(row, row), fixtures.read(row, authoritative=False), fixtures.read(row, complete=False)]:
            self.api.lookup_hello_owner = Mock(return_value=result)
            self.assertFalse(self.run_hello().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_wrong_account_policy_role_or_missing_user_holds(self):
        row = dict(applicant_id='220250093', policy_id='test-policy', role='originating_producer', user_id='test-reviewer')
        for field, value in [('applicant_id', 'wrong'), ('policy_id', 'wrong'), ('role', 'applicable_csr'), ('user_id', '')]:
            self.api.lookup_hello_owner = Mock(return_value=fixtures.read({**row, field: value}))
            self.assertFalse(self.run_hello().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_no_explicit_override_or_inactive_fallback(self):
        self.assertFalse(self.run_hello(assignee_id='other').succeeded)
        self.api.assignees = fixtures.read(dict(user_id='test-reviewer', active=False))
        self.assertFalse(self.run_hello().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_missing_adapter_holds_without_writing(self):
        self.api.lookup_hello_owner = None
        self.assertFalse(self.run_hello().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_replay_and_type_change_do_not_duplicate(self):
        first = self.run_hello()
        self.assertTrue(first.succeeded)
        self.assertEqual(self.run_hello().destination['task_id'], first.destination['task_id'])
        self.assertFalse(self.run_hello('renewal').succeeded)
        self.assertEqual(self.api.writes, 1)
