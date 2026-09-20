"""Synthetic failure injection: no browser, credentials, or external writes."""
import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from durable_temp import durable_temporary_directory
from robie_job_engine.chat_ezlynx_destination_verifier import HermesChatEzlynxDestinationVerifier
from robie_job_engine.document_upload_reliability import document_request, upload_with_receipt, run_document_email
from robie_job_engine.email_guard import run_guarded_email_task
from robie_job_engine.message_verification import MessageOutcomeVerifier
from robie_job_engine.models import JobStatus
from robie_job_engine.store import JobStore

BODY = b'synthetic upload content'
REQUEST = 'Upload document "test.txt" to applicant 220250093.'


class Destination:
    def __init__(self):
        self.posts = 0
        self.rows = []
        self.body = BODY
        self.fail_download = False
        self.lose_receipt = False

    def upload_applicant_document(self, applicant, name, body, **kwargs):
        self.posts += 1
        self.rows = [{'id': '987', 'name': name}]
        self.body = body
        if self.lose_receipt:
            raise TimeoutError('synthetic timeout after save')
        return '987'

    def search_applicant_documents(self, applicant):
        return {'results': self.rows}

    def documents_for_applicant(self, applicant):
        return self.rows

    def download_document(self, doc_id):
        if self.fail_download:
            raise ConnectionError('synthetic read failure')
        return self.body

    def policy_by_number(self, policy):
        raise AssertionError('document upload must not read policies')


class DocumentReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = durable_temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'jobs.db')
        self.store = JobStore(self.db)
        self.job = self.store.create_job('hermes.email_task', {
            'request_text': REQUEST, 'applicant_id': '220250093',
            'document_names': ['test.txt'],
        }, idempotency_key='upload-test')
        self.store.transition(self.job['id'], JobStatus.RUNNING)
        self.env = {'ROBIE_JOB_DB': self.db, 'ROBIE_JOB_ID': self.job['id']}
        self.client = Destination()

    def upload(self, **kwargs):
        return upload_with_receipt(self.client, '220250093', 'test.txt', BODY, env=self.env, **kwargs)

    def verify(self):
        return MessageOutcomeVerifier(HermesChatEzlynxDestinationVerifier(self.client)).verify(
            self.store.get_job(self.job['id']), self.store.get_checkpoint(self.job['id'], 'action'))

    def test_document_only_completes_with_fresh_id_name_account_and_content(self):
        self.upload()
        result = self.verify()
        self.assertTrue(result.verified, result.error)
        self.assertEqual(result.evidence.observed['sha256'], hashlib.sha256(BODY).hexdigest())

    def test_retry_after_read_failure_does_not_repeat_post(self):
        self.client.fail_download = True
        with self.assertRaises(ConnectionError):
            self.upload()
        diagnostic = self.store.get_checkpoint(self.job['id'], 'document_upload_diagnostic')
        self.assertEqual(diagnostic['next_action'], 'READ_BACK_ONLY')
        self.assertFalse(diagnostic['safe_to_repeat_upload'])
        self.client.fail_download = False
        self.upload()
        self.assertEqual(self.client.posts, 1)
        self.assertTrue(self.verify().verified)

    def test_repeated_success_rechecks_without_duplicate_upload(self):
        self.upload()
        self.upload()
        self.assertEqual(self.client.posts, 1)

    def test_lost_post_response_stops_without_duplicate(self):
        self.client.lose_receipt = True
        with self.assertRaises(TimeoutError):
            self.upload()
        diagnostic = self.store.get_checkpoint(self.job['id'], 'document_upload_diagnostic')
        self.assertEqual(diagnostic['next_action'], 'RECONCILE_BEFORE_WRITE')
        with self.assertRaisesRegex(ValueError, 'OUTCOME_UNKNOWN'):
            self.upload()
        self.assertEqual(self.client.posts, 1)

    def test_source_changes_after_intent_are_refused(self):
        self.upload()
        with self.assertRaisesRegex(ValueError, 'source changed'):
            upload_with_receipt(self.client, '220250093', 'test.txt', b'changed', env=self.env)
        self.assertEqual(self.client.posts, 1)

    def test_wrong_contents_fail_independent_verification(self):
        self.upload()
        self.client.body = b'wrong but nonempty'
        self.assertFalse(self.verify().verified)

    def test_claimed_receipt_without_durable_source_receipt_fails(self):
        result = MessageOutcomeVerifier(HermesChatEzlynxDestinationVerifier(self.client)).verify(
            self.store.get_job(self.job['id']), {'destination': {
                'document_id': '987', 'sha256': hashlib.sha256(BODY).hexdigest(),
                'applicant_id': '220250093', 'document_name': 'test.txt'}})
        self.assertFalse(result.verified)

    def test_chat_job_completes_through_same_independent_verifier(self):
        from robie_job_engine.engine import JobEngine
        chat = self.store.create_job('hermes.google_chat_task', {
            'text': REQUEST, 'applicant_id': '220250093'}, idempotency_key='chat-upload')
        self.store.transition(chat['id'], JobStatus.RUNNING)
        upload_with_receipt(self.client, '220250093', 'test.txt', BODY,
                            env={'ROBIE_JOB_ID': chat['id'], 'ROBIE_JOB_DB': self.db})
        self.store.transition(chat['id'], JobStatus.VERIFYING)
        verifier = MessageOutcomeVerifier(HermesChatEzlynxDestinationVerifier(self.client))
        final = JobEngine(self.store, {}, {'hermes.google_chat_task': verifier}).run(chat['id'])
        self.assertEqual(final['status'], 'COMPLETE')

    def test_same_filename_different_id_fails(self):
        self.upload()
        self.client.rows = [{'id': '999', 'name': 'test.txt'}]
        self.assertFalse(self.verify().verified)

    def test_wrong_name_on_saved_id_fails(self):
        self.upload()
        self.client.rows = [{'id': '987', 'name': 'wrong.txt'}]
        self.assertFalse(self.verify().verified)

    def test_wrong_account_is_refused_before_upload(self):
        with self.assertRaisesRegex(ValueError, 'account differs'):
            upload_with_receipt(self.client, '999', 'test.txt', BODY, env=self.env)
        self.assertEqual(self.client.posts, 0)

    def test_changed_filename_or_policy_association_is_refused(self):
        with self.assertRaises(ValueError):
            upload_with_receipt(self.client, '220250093', 'other.txt', BODY, env=self.env)
        with self.assertRaises(ValueError):
            self.upload(policy_master_id='12')
        self.assertEqual(self.client.posts, 0)

    def test_incomplete_job_context_fails_before_upload(self):
        with self.assertRaises(ValueError):
            upload_with_receipt(self.client, '220250093', 'test.txt', BODY,
                                env={'ROBIE_JOB_ID': self.job['id']})
        self.assertEqual(self.client.posts, 0)

    def test_parallel_calls_have_only_one_post_owner(self):
        entered, release = Event(), Event()
        original = self.client.upload_applicant_document
        def slow(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)
        self.client.upload_applicant_document = slow
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.upload)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(ValueError, 'OUTCOME_UNKNOWN'):
                    self.upload()
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(self.client.posts, 1)

    def test_extra_outcomes_are_not_silently_covered(self):
        for text in (REQUEST + ' Add a note.', REQUEST + ' Bind the policy.',
                     'Subject: Cancel policy\n\n' + REQUEST):
            self.assertIsNone(document_request({'request_text': text}))
        self.assertIsNotNone(document_request({'request_text': 'Subject: Document upload\n\n' + REQUEST}))

    def test_deterministic_email_route_uses_only_named_attachment(self):
        path = Path(self.tmp.name) / 'source.txt'
        path.write_bytes(BODY)
        result = run_document_email(self.store, self.job['id'], [('test.txt', str(path))], client=self.client)
        self.assertIn('document 987', result)
        self.assertTrue(self.verify().verified)
        self.assertEqual(self.store.get_checkpoint(self.job['id'], 'email_route')['execution'], 'deterministic-api')

    def test_deterministic_route_holds_missing_or_ambiguous_attachments(self):
        for attachments in ([], [('wrong.txt', 'unused')], [('test.txt', 'unused'), ('extra.txt', 'unused')]):
            self.assertTrue(run_document_email(self.store, self.job['id'], attachments, client=self.client).startswith('ROBIE HITL:'))
        self.assertEqual(self.client.posts, 0)

    def test_deterministic_failure_does_not_fall_back_to_agent(self):
        path = Path(self.tmp.name) / 'source.txt'
        path.write_bytes(BODY)
        self.client.lose_receipt = True
        for _ in range(2):
            result = run_document_email(self.store, self.job['id'], [('test.txt', str(path))], client=self.client)
            self.assertTrue(result.startswith('ROBIE_OUTCOME_UNKNOWN:'))
        self.assertEqual(self.client.posts, 1)

    def test_email_job_completes_and_duplicate_message_does_not_reexecute(self):
        db = str(Path(self.tmp.name) / 'email.db')
        calls = []
        def run(prompt, job_id, db_path):
            calls.append(job_id)
            upload_with_receipt(self.client, '220250093', 'test.txt', BODY,
                                env={'ROBIE_JOB_ID': job_id, 'ROBIE_JOB_DB': db_path})
            return 'Upload saved.'
        verifier = MessageOutcomeVerifier(HermesChatEzlynxDestinationVerifier(self.client))
        with patch('robie_job_engine.email_guard.add_synced_context', side_effect=lambda x: x):
            for _ in range(2):
                result = run_guarded_email_task(db_path=db, gmail_message_id='synthetic-message',
                    prompt=REQUEST, run_agent=lambda _: '', run_agent_with_context=run,
                    verifiers={'hermes.email_task': verifier}, attachment_names=('test.txt',))
                self.assertIn('— COMPLETE', result)
                self.assertIn('contents match', result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.client.posts, 1)
