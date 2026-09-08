import hashlib
import unittest
from dataclasses import replace
from unittest.mock import patch, Mock
import test_intake_phase1 as fixtures
from robie_job_engine.document_intake_plan import DocumentFacts, plan_batch
from robie_job_engine.intake_core import IntakeHold
from robie_job_engine.pilot_intake import HelloPilotIntake, CertificatesPilotIntake


class DocumentPlanTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'ROBIE_ENV': 'TEST'})
        env.start(); self.addCleanup(env.stop)
        self.digest = hashlib.sha256(b'synthetic').hexdigest()
        self.doc = DocumentFacts(1, 2, 'LR', 'test-account', 'test-policy', 'TEST-1', True)

    def plan(self, docs=None):
        return plan_batch('drive:test-file', self.digest, 2, docs or [self.doc])

    def test_names_and_replay_are_stable(self):
        self.assertEqual(self.plan(), self.plan())
        self.assertEqual(self.plan()[0]['document_title'], 'Loss Runs')
        self.assertFalse(self.plan()[0]['destination_verified'])

    def test_split_documents_retain_distinct_page_identity(self):
        plans = self.plan([replace(self.doc, last_page=1), replace(self.doc, first_page=2, code='NR')])
        self.assertNotEqual(plans[0]['source_key'], plans[1]['source_key'])

    def test_overlap_gap_and_unconfirmed_hold(self):
        for docs in [[self.doc, self.doc], [replace(self.doc, last_page=1)], [replace(self.doc, confirmed=False)]]:
            with self.assertRaises(IntakeHold): self.plan(docs)

    def test_unknown_type_and_missing_policy_hold(self):
        for doc in [replace(self.doc, code='PD'), replace(self.doc, policy_id='')]:
            with self.assertRaises(IntakeHold): self.plan([doc])

    def test_quotes_prohibit_sharing_and_recommendations_require_workflow_check(self):
        self.assertEqual(self.plan([replace(self.doc, code='QUOTE')])[0]['client_sharing'], 'prohibited')
        self.assertEqual(self.plan([replace(self.doc, code='RC')])[0]['task_decision'], 'check_existing_then_required_workflow')

    def test_production_refused(self):
        with patch.dict('os.environ', {'ROBIE_ENV': 'PRODUCTION'}):
            with self.assertRaises(IntakeHold): self.plan()


class PilotAssignmentTests(unittest.TestCase):
    setUp = fixtures.IntakePhase1Tests.setUp

    def run_pilot(self, cls, explicit=''):
        return cls(self.api, self.archive).perform(self.source, self.ids, due_at=self.due, assignee_id=explicit)

    def test_latest_pilot_targets_use_configured_exact_ids(self):
        for cls, key in [(HelloPilotIntake, 'hello_alejandro'), (CertificatesPilotIntake, 'certificates_user')]:
            self.api.tasks.clear()
            self.api.lookup_pilot_assignee = Mock(return_value=fixtures.read(dict(assignment_key=key, user_id='test-reviewer')))
            result = self.run_pilot(cls)
            self.assertTrue(result.succeeded, result.error)
            self.api.lookup_pilot_assignee.assert_called_once_with(key)

    def test_no_guess_or_override(self):
        self.assertFalse(self.run_pilot(HelloPilotIntake).succeeded)
        self.api.lookup_pilot_assignee = Mock(return_value=fixtures.read(dict(assignment_key='hello_alejandro', user_id='test-reviewer')))
        self.assertFalse(self.run_pilot(HelloPilotIntake, 'wrong').succeeded)
        self.assertEqual(self.api.writes, 0)
