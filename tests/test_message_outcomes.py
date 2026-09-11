"""Message receipts must retain both verified facts and honest gaps."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from robie_job_engine import email_guard
from robie_job_engine.chat_ezlynx_destination_verifier import HermesChatEzlynxDestinationVerifier
from robie_job_engine.message_results import verification_summary
from robie_job_engine.message_verification import MessageOutcomeVerifier
from robie_job_engine.store import JobStore
from test_chat_ezlynx_destination_verifier import FakePort, real_policy_row, BOND_APPLICANT, BOND_POLICY


class MessageOutcomeTests(unittest.TestCase):
    def run_email(self, port, request="Create the policy and upload Bond.pdf"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / 'jobs.db')
        calls = []
        def worker(prompt, job_id, db_path):
            calls.append(job_id)
            store = JobStore(db_path)
            store.add_playwright_exec(job_id, 'playwright_exec', 'ok',
                result={'url': f'https://app.ezlynx.com/web/account/{BOND_APPLICANT}'})
            return f'Created policy {BOND_POLICY}. Uploaded Bond.pdf.'
        verifier = MessageOutcomeVerifier(HermesChatEzlynxDestinationVerifier(port))
        with mock.patch.object(email_guard, '_default_email_verifiers', return_value={'hermes.email_task': verifier}):
            first = email_guard.run_guarded_email_task(db_path=db, gmail_message_id='message',
                prompt=request, run_agent=lambda p: self.fail(),
                run_agent_with_context=worker)
            second = email_guard.run_guarded_email_task(db_path=db, gmail_message_id='message',
                prompt=request, run_agent=lambda p: self.fail(),
                run_agent_with_context=worker)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)
        return first, JobStore(db), calls[0]

    def test_email_reports_checked_policy_and_documents_without_overstating_coverage(self):
        result, store, job_id = self.run_email(FakePort(policies=[real_policy_row()], documents=[{'name': 'Bond.pdf'}]))
        self.assertEqual(store.get_job(job_id)['status'], 'UNVERIFIED')
        self.assertIn(f'ROBIE Job {job_id} — UNVERIFIED', result)
        self.assertIn('Checked: documents found: Bond.pdf', result)
        self.assertFalse(store.list_evidence(job_id)[-1]['verified'])

    def test_missing_document_reports_checked_policy_and_gap(self):
        result, store, job_id = self.run_email(FakePort(policies=[real_policy_row()]))
        self.assertEqual(store.get_job(job_id)['status'], 'UNVERIFIED')
        self.assertIn('Checked: policy', result)
        self.assertIn('Not confirmed: documents missing: Bond.pdf', result)

    def test_wrong_applicant_does_not_become_a_confirmed_fact(self):
        row = real_policy_row(); row['ApplicantId'] = 'wrong'
        result, store, job_id = self.run_email(FakePort(policies=[row], documents=[{'name': 'Bond.pdf'}]))
        self.assertEqual(store.get_job(job_id)['status'], 'UNVERIFIED')
        self.assertNotIn('Checked: policy', result)

    def test_claim_without_independent_records_does_not_complete(self):
        result, store, job_id = self.run_email(FakePort())
        self.assertEqual(store.get_job(job_id)['status'], 'UNVERIFIED')
        self.assertNotIn('Checked:', result)

    def test_stale_partial_evidence_is_not_reported_as_checked(self):
        result, store, job_id = self.run_email(FakePort(policies=[real_policy_row()]))
        with mock.patch.object(store, 'list_evidence', return_value=[{
            'authoritative': True, 'method': 'EZLYNX_API_DESTINATION_READBACK',
            'captured_at': '2000-01-01T00:00:00+00:00', 'observed': {'policy_found': True}, 'expected': {}}]):
            self.assertEqual(verification_summary(store, job_id), '')

    def test_complete_when_entire_request_is_policy_presence_check(self):
        result, store, job_id = self.run_email(FakePort(policies=[real_policy_row()], documents=[{'name': 'Bond.pdf'}]),
            request=f'Verify policy {BOND_POLICY} exists on applicant {BOND_APPLICANT}')
        self.assertEqual(store.get_job(job_id)['status'], 'COMPLETE')

    def test_cancellation_and_outgoing_email_are_not_verified_by_existing_policy(self):
        result, store, job_id = self.run_email(FakePort(policies=[real_policy_row()], documents=[{'name': 'Bond.pdf'}]),
            request='Cancel this policy and email the client')
        self.assertEqual(store.get_job(job_id)['status'], 'UNVERIFIED')

    def test_near_filename_does_not_satisfy_requested_document(self):
        result, store, job_id = self.run_email(FakePort(policies=[real_policy_row()], documents=[{'name': 'Bond.pdf.backup'}]))
        self.assertIn('documents missing: Bond.pdf', result)

    def test_evidence_before_latest_action_is_not_reported_as_current(self):
        result, store, job_id = self.run_email(FakePort(policies=[real_policy_row()]))
        rows = store.list_evidence(job_id)
        with mock.patch.object(store, 'get_checkpoint_record', return_value={'created_at': '2099-01-01T00:00:00+00:00'}):
            self.assertEqual(verification_summary(store, job_id), '')

    def test_requested_attachment_cannot_be_omitted_from_worker_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'jobs.db')
            def worker(prompt, job_id, db_path):
                JobStore(db_path).add_playwright_exec(job_id, 'playwright_exec', 'ok', result={'url':f'https://app.ezlynx.com/web/account/{BOND_APPLICANT}'})
                return f'Policy {BOND_POLICY} is present.'
            verifier=MessageOutcomeVerifier(HermesChatEzlynxDestinationVerifier(FakePort(policies=[real_policy_row()])))
            response=email_guard.run_guarded_email_task(db_path=db,gmail_message_id='attachment',prompt='File this attachment',
                attachment_names=('Required.pdf',),run_agent=lambda p:self.fail(),run_agent_with_context=worker,
                verifiers={'hermes.email_task':verifier})
            self.assertIn('Required.pdf',response)
            self.assertIn('documents missing',response)

    def test_subject_instructions_are_not_discarded_for_body_presence_check(self):
        result, store, job_id=self.run_email(FakePort(policies=[real_policy_row()],documents=[{'name':'Bond.pdf'}]),
            request=f'Subject: Cancel this policy and email client\n\nVerify policy {BOND_POLICY} exists on applicant {BOND_APPLICANT}')
        self.assertEqual(store.get_job(job_id)['status'],'UNVERIFIED')
