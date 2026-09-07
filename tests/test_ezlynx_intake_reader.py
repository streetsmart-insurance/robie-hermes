import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from robie_job_engine.ezlynx_intake_reader import DocumentedEzlynxReader, EzlynxReadTransport, _NoRedirect
from robie_job_engine.intake_core import Identifiers, IntakeHold


class EzlynxIntakeReaderTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'ROBIE_ENV': 'TEST'})
        env.start()
        self.addCleanup(env.stop)
        self.transport = Mock()
        self.reader = DocumentedEzlynxReader(self.transport)

    def test_documented_applicant_policy_fields_and_relationship(self):
        self.transport.get.side_effect = [
            {'Id': '220250093', 'BusinessName': 'Synthetic LLC', 'BusinessEmail': 'insured@example.test'},
            {'Id': 'policy-1', 'ApplicantId': '220250093', 'PolicyNumber': 'TEST-1', 'EffectiveDate': '2026-09-01T00:00:00'}]
        result = self.reader.lookup_candidates(Identifiers(applicant_id='220250093', policy_id='policy-1'))
        self.assertEqual(result.rows[0]['policy_effective_date'], '2026-09-01')
        self.assertEqual(result.rows[0]['insured_name'], 'Synthetic LLC')
        self.assertEqual([c.args[0] for c in self.transport.get.call_args_list],
                         ['Applicant/v2/220250093', 'Policy/policy-1?encryptPolicyIdAndApplicantId=false'])

    def test_policy_belonging_to_different_applicant_is_refused(self):
        self.transport.get.side_effect = [{'Id': '220250093'}, {'Id': 'policy-1', 'ApplicantId': 'other'}]
        with self.assertRaises(IntakeHold):
            self.reader.lookup_candidates(Identifiers(applicant_id='220250093', policy_id='policy-1'))

    def test_undocumented_search_is_not_guessed(self):
        with self.assertRaises(IntakeHold): self.reader.lookup_candidates(Identifiers(insured_email='insured@example.test'))
        self.transport.get.assert_not_called()

    def test_policy_number_without_id_is_held(self):
        self.transport.get.return_value = {'Id': '220250093'}
        with self.assertRaises(IntakeHold): self.reader.lookup_candidates(Identifiers(applicant_id='220250093', policy_number='TEST-1'))
        self.assertEqual(self.transport.get.call_count, 1)

    def test_active_user_is_normalized_from_documented_response(self):
        self.transport.get.return_value = {'Id': 42, 'IsActive': True}
        self.assertEqual(self.reader.lookup_assignee('42').rows[0], {'user_id': '42', 'active': True})
        self.transport.get.assert_called_once_with('User/42')

    def test_login_lookup_retains_ambiguity_and_omits_unneeded_pii(self):
        self.transport.get.return_value = [
            {'Id': 42, 'UserName': 'synthetic-user', 'IsActive': True, 'Email': 'private@example.test'},
            {'Id': 43, 'UserName': 'Synthetic-User', 'IsActive': False}]
        result = self.reader.find_users_by_login('test-org', 'synthetic-user')
        self.assertEqual(len(result.rows), 2)
        self.assertNotIn('Email', result.rows[0])

    def test_task_operations_stay_unavailable(self):
        for function, args in ((self.reader.find_source_tasks, ('key',)),
                               (self.reader.find_related_work, ('app', 'policy', None)),
                               (self.reader.create_task_once, ({},)), (self.reader.read_task, ('task',))):
            with self.assertRaises(IntakeHold): function(*args)
        self.transport.get.assert_not_called()

    def test_insecure_or_credential_bearing_base_url_is_rejected(self):
        for url in ('http://api.example.test/', 'https://user:password@api.example.test/', 'https://api.example.test/?token=x'):
            with self.assertRaises(IntakeHold): EzlynxReadTransport(url, lambda: {})

    def test_production_refuses_before_credential_provider(self):
        provider = Mock()
        transport = EzlynxReadTransport('https://api.example.test/', provider)
        with patch.dict('os.environ', {'ROBIE_ENV': 'PRODUCTION'}):
            with self.assertRaises(IntakeHold): transport.get('User/42')
        provider.assert_not_called()

    def test_transport_does_not_forward_credentials_on_redirect(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example.test/'))

    def test_transport_http_error_does_not_expose_response_details(self):
        transport = EzlynxReadTransport('https://api.example.test/', lambda: {
            'EZAppSecret': 'synthetic', 'EZToken': 'synthetic', 'AccountUsername': 'synthetic'})
        with patch('robie_job_engine.ezlynx_intake_reader.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError('https://api.example.test', 401, 'secret-example', {}, None)
            with self.assertRaisesRegex(IntakeHold, '^EZLynx read unavailable \\(HTTP 401\\)$'):
                transport.get('User/42')


if __name__ == '__main__':
    unittest.main()
