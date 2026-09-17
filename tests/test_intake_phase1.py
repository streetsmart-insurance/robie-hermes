"""Synthetic-only Phase 1 tests; never use a live API or browser session."""
import base64
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from robie_job_engine.certificates_intake import CertificatesIntake
from robie_job_engine.hello_intake import HelloIntake
from robie_job_engine.progressive_retrieval import ProgressiveRetrieval
from robie_job_engine.intake_core import (
    Identifiers, IntakeHold, IntakeVerifier, ReadResult, SourceArchive, SourceItem, resolve_match,
)
from robie_job_engine.intake_email import read_selected_message


def read(*rows, authoritative=True, complete=True):
    return ReadResult(tuple(rows), authoritative, complete)


class FakeApi:
    """No external calls. At-most-once behavior is a fixture, not API proof."""
    def __init__(self):
        self.candidates = read({'applicant_id': '220250093', 'policy_id': 'test-policy',
                                'policy_number': 'TEST-101', 'insured_email': 'insured@example.test'})
        self.assignees = read({'user_id': 'test-reviewer', 'active': True})
        self.tasks = {}
        self.related = read()
        self.writes = 0
        self.timeout_after_write = False
        self.read_available = True

    def lookup_candidates(self, identifiers): return self.candidates
    def lookup_assignee(self, user_id): return self.assignees
    def lookup_certificates_team_member(self, user_id):
        return read(dict(user_id=user_id, certificates_team_member=True))
    def lookup_hello_owner(self, applicant, policy, request_type):
        return read(dict(applicant_id=applicant, policy_id=policy, user_id='test-reviewer',
                         role='originating_producer' if request_type == 'new_business' else 'applicable_csr'))
    def find_source_tasks(self, key): return read(*[v for v in self.tasks.values() if v['source_key'] == key])
    def find_related_work(self, *args): return self.related
    def create_task_once(self, task):
        existing = self.find_source_tasks(task['source_key']).rows
        if existing: return existing[0]['task_id']
        self.writes += 1
        task_id = 'synthetic-task-' + str(self.writes)
        self.tasks[task_id] = dict(task, task_id=task_id)
        if self.timeout_after_write: raise TimeoutError('credential-must-never-appear')
        return task_id
    def read_task(self, task_id):
        return read(*([self.tasks[task_id]] if task_id in self.tasks else []), authoritative=self.read_available)


class IntakePhase1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict('os.environ', {'ROBIE_ENV': 'TEST'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.archive = SourceArchive(Path(self.tmp.name) / 'sources')
        self.api = FakeApi()
        self.source = SourceItem('gmail', 'intake@example.test', 'message-1',
                                 'https://mail.google.com/example-test-message',
                                 '2026-09-08T09:00:00-04:00', 'original.eml',
                                 b'From: insured@example.test\r\nSubject: Written request\r\n\r\nPlease review.')
        self.ids = Identifiers(applicant_id='220250093', policy_number='TEST-101')
        self.due = '2026-09-08T10:00:00-04:00'

    def run_worker(self, worker=CertificatesIntake, **kwargs):
        if worker is HelloIntake:
            kwargs.setdefault('request_type', 'new_business')
        return worker(self.api, self.archive).perform(
            kwargs.pop('source', self.source), kwargs.pop('identifiers', self.ids),
            assignee_id=kwargs.pop('assignee_id', 'test-reviewer'), due_at=kwargs.pop('due_at', self.due), **kwargs)

    def test_each_separate_flow_returns_receipt_and_requires_independent_readback(self):
        for worker in (CertificatesIntake, HelloIntake, ProgressiveRetrieval):
            with self.subTest(worker=worker):
                source = replace(self.source, system=worker.source_system, source_id=worker.process)
                result = self.run_worker(worker, source=source)
                self.assertTrue(result.succeeded, result.error)
                self.assertEqual(result.action, 'intake.' + worker.process)
                self.assertTrue(result.destination['manual_upload_required'])
                self.assertIn('MANUAL UPLOAD REQUIRED', result.destination['description'])
                self.assertEqual(Path(result.detail['source_artifact']).read_bytes(), source.content)
                verification = IntakeVerifier(self.api).verify({}, {'destination': result.destination})
                self.assertTrue(verification.verified)
                self.assertTrue(verification.evidence.authoritative)

    def test_replay_creates_only_one_task(self):
        first, second = self.run_worker(), self.run_worker()
        self.assertTrue(second.succeeded)
        self.assertEqual(first.destination['task_id'], second.destination['task_id'])
        self.assertEqual(self.api.writes, 1)

    def test_cross_flow_replay_does_not_create_second_task(self):
        self.run_worker()
        self.assertFalse(self.run_worker(HelloIntake).succeeded)
        self.assertEqual(self.api.writes, 1)

    def test_same_message_id_in_different_mailboxes_has_distinct_identity(self):
        self.assertNotEqual(self.source.key, replace(self.source, source_account='other@example.test').key)

    def test_original_bytes_cannot_change_under_same_source_identity(self):
        self.run_worker()
        self.assertFalse(self.run_worker(source=replace(self.source, content=b'changed')).succeeded)
        self.assertEqual(self.api.writes, 1)

    def test_archive_ignores_untrusted_filename_paths(self):
        result = self.run_worker(source=replace(self.source, filename='../../outside.exe'))
        path = Path(result.detail['source_artifact'])
        self.assertEqual(path.parent, self.archive.root)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_archive_refuses_existing_shared_directory(self):
        self.archive.root.mkdir(mode=0o755)
        self.archive.root.chmod(0o755)
        self.assertFalse(self.run_worker().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_selected_gmail_entrypoints_keep_flows_separate(self):
        gmail = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: SimpleNamespace(
            get=lambda **kwargs: SimpleNamespace(execute=lambda: {
                'id': kwargs['id'], 'raw': base64.urlsafe_b64encode(self.source.content).decode(),
                'internalDate': '1788872400000'}))))
        for cls in (CertificatesIntake, HelloIntake):
            result = cls(self.api, self.archive).run_selected(
                gmail, mailbox='intake@example.test', message_id=cls.process,
                identifiers=self.ids, assignee_id='test-reviewer', due_at=self.due,
                **({'request_type': 'new_business'} if cls is HelloIntake else {}))
            self.assertTrue(result.succeeded, result.error)
            self.assertEqual(result.destination['process'], cls.process)

    def test_unknown_or_ambiguous_match_is_held_with_preserved_source(self):
        for rows in (read(), read({'applicant_id': '220250093', 'policy_id': 'term-1', 'policy_number': 'TEST-101'},
                                 {'applicant_id': '220250093', 'policy_id': 'term-2', 'policy_number': 'TEST-101'})):
            self.api.candidates = rows
            result = self.run_worker()
            self.assertFalse(result.succeeded)
            self.assertTrue(Path(result.detail['source_artifact']).is_file())
        self.assertEqual(self.api.writes, 0)

    def test_name_only_matching_is_not_automatic(self):
        self.assertFalse(self.run_worker(identifiers=Identifiers(insured_name='Example')).succeeded)

    def test_conflicting_applicant_and_policy_are_not_accepted(self):
        self.assertFalse(self.run_worker(identifiers=Identifiers(applicant_id='220250093', policy_number='OTHER')).succeeded)

    def test_account_level_match_collapses_policy_rows_without_guessing_a_policy(self):
        result = resolve_match(Identifiers(applicant_id='220250093'), read(
            {'applicant_id': '220250093', 'policy_id': 'first'},
            {'applicant_id': '220250093', 'policy_id': 'second'}))
        self.assertEqual(result, ('220250093', ''))

    def test_incomplete_api_search_blocks_creation(self):
        self.api.candidates = read(*self.api.candidates.rows, complete=False)
        self.assertFalse(self.run_worker().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_inactive_or_missing_assignee_blocks_creation(self):
        self.api.assignees = read({'user_id': 'test-reviewer', 'active': False})
        self.assertFalse(self.run_worker().succeeded)
        self.assertFalse(self.run_worker(assignee_id='').succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_related_work_requires_review_instead_of_duplicate_workflow(self):
        self.api.related = read({'task_id': 'related-service-task'})
        self.assertFalse(self.run_worker().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_timeout_after_remote_write_can_reconcile_without_duplicate(self):
        self.api.timeout_after_write = True
        first = self.run_worker()
        self.assertFalse(first.succeeded)
        self.assertFalse(first.retryable)
        self.assertNotIn('credential', first.error)
        second = self.run_worker()
        self.assertTrue(second.succeeded)
        self.assertEqual(self.api.writes, 1)

    def test_missing_or_wrong_destination_cannot_verify(self):
        result = self.run_worker()
        verifier = IntakeVerifier(self.api)
        task = self.api.tasks[result.destination['task_id']]
        for field, value in [('assigned_user_id', 'wrong'), ('applicant_id', 'wrong'), ('manual_upload_required', False)]:
            saved = task[field]
            task[field] = value
            self.assertFalse(verifier.verify({}, {'destination': result.destination}).verified)
            task[field] = saved
        self.api.read_available = False
        evidence = verifier.verify({}, {'destination': result.destination})
        self.assertFalse(evidence.verified)
        self.assertFalse(evidence.evidence.authoritative)

    def test_production_and_unset_environment_refuse_before_any_api_write(self):
        for env in ('PRODUCTION', ''):
            with patch.dict('os.environ', {'ROBIE_ENV': env}):
                self.assertFalse(self.run_worker().succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_compiled_applicant_scope_is_preserved(self):
        self.api.candidates = read({'applicant_id': '999', 'policy_id': 'test-policy', 'policy_number': 'TEST-101'})
        with patch(
            "robie_job_engine.ezlynx_write_scope.ALLOWED_EZLYNX_WRITE_APPLICANT_IDS",
            frozenset({"220250093"}),
        ):
            self.assertFalse(self.run_worker(identifiers=Identifiers(applicant_id='999')).succeeded)
        self.assertEqual(self.api.writes, 0)

    def test_wrong_source_system_cannot_enter_email_flow(self):
        self.assertFalse(self.run_worker(source=replace(self.source, system='progressive')).succeeded)

    def test_gmail_reads_only_selected_raw_message(self):
        calls = []
        def get(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(execute=lambda: {'id': 'message-1', 'raw': base64.urlsafe_b64encode(self.source.content).decode(), 'internalDate': '1788872400000'})
        gmail = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: SimpleNamespace(get=get)))
        source = read_selected_message(gmail, mailbox='intake@example.test', message_id='message-1')
        self.assertEqual(source.content, self.source.content)
        self.assertEqual(calls, [{'userId': 'intake@example.test', 'id': 'message-1', 'format': 'raw'}])

    def test_gmail_response_for_wrong_message_is_rejected(self):
        gmail = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: SimpleNamespace(
            get=lambda **kwargs: SimpleNamespace(execute=lambda: {'id': 'other', 'raw': 'abc', 'internalDate': '1'}))))
        with self.assertRaises(IntakeHold):
            read_selected_message(gmail, mailbox='intake@example.test', message_id='message-1')

    def portal(self, **changes):
        row = dict(document_id='carrier-1', source_account='test-agency', requires_action=True,
                   already_delivered=False, processed=False, processed_or_effective_date='2026-09-06')
        row.update(changes)
        return SimpleNamespace(list_documents=lambda **kwargs: read(row), download_document=lambda doc:
                               replace(self.source, system='progressive', source_account='test-agency', source_id=doc))

    def retrieve(self, portal, **changes):
        args = dict(scope='policies_need_service', start=date(2026, 9, 4), end=date(2026, 9, 8), document_id='carrier-1')
        args.update(changes)
        return ProgressiveRetrieval(self.api, self.archive).retrieve_selected(portal, **args)

    def test_progressive_preserves_weekend_retrieval_and_scope(self):
        for scope in ('policies_need_service', 'fao_communications', 'bop_pending_cancel_nonpayment'):
            self.assertEqual(self.retrieve(self.portal(), scope=scope).source_id, 'carrier-1')

    def test_progressive_rejects_processed_already_delivered_or_non_actionable_items(self):
        for changes in ({'processed': True}, {'already_delivered': True}, {'requires_action': False}, {'requires_action': None}):
            with self.assertRaises(IntakeHold): self.retrieve(self.portal(**changes))

    def test_progressive_rejects_unbounded_dates_wrong_scope_or_wrong_agency(self):
        for changes in ({'start': date(2025, 1, 1)}, {'scope': 'guessed'}, {'end': date(2026, 9, 1)}):
            with self.assertRaises(IntakeHold): self.retrieve(self.portal(), **changes)
        with self.assertRaises(IntakeHold): self.retrieve(self.portal(source_account='other-agency'))


if __name__ == '__main__':
    unittest.main()
