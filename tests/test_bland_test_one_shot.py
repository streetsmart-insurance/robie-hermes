"""Synthetic only. No call, credential access or remote traffic."""
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from robie_job_engine.bland_test_one_shot import payload,dispatch_once,TestCallRefused,TARGET,CALLER

class OneShotSafety(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.path.chmod(0o700)
        # Tests may use a private temp directory; the production contract refuses
        # /tmp explicitly, so use an isolated home-directory test location.
        self.home=tempfile.TemporaryDirectory(dir=str(Path.home()),prefix='bland-proof-');self.root=Path(self.home.name);self.root.chmod(0o700)
        self.body=payload(target=TARGET,voice_id='SYN-VOICE',caller_id=CALLER)
        self.request=Mock(side_effect=[{'call_id':'SYN-CALL'},{'status':'queued'}])
    def tearDown(self):self.tmp.cleanup();self.home.cleanup()
    def run_case(self,**kw):
        args=dict(db_path=self.root/'calls.sqlite',test_id='SYN-TEST',approved_day=date(2026,10,1),today=date(2026,10,1),hostname='hermes-test-01',body=self.body,api_key='SYN-KEY',request=self.request);args.update(kw);return dispatch_once(**args)
    def test_one_post_then_get(self):
        r=self.run_case();self.assertEqual('SYN-CALL',r['call_id']);self.assertEqual(['POST','GET'],[c.args[0] for c in self.request.call_args_list])
    def test_duplicate_never_dispatches(self):
        self.run_case();r=self.run_case();self.assertFalse(r['new_dispatch']);self.assertEqual(2,self.request.call_count)
    def test_uncertain_post_never_retries(self):
        self.request.side_effect=TimeoutError();r=self.run_case();self.assertEqual('dispatch_unknown',r['state']);self.run_case();self.assertEqual(1,self.request.call_count)
    def test_missing_call_id_never_retries(self):
        self.request.side_effect=[{}];r=self.run_case();self.assertFalse(r['retry_allowed']);self.run_case();self.assertEqual(1,self.request.call_count)
    def test_prod_host_refused(self):
        with self.assertRaises(TestCallRefused):self.run_case(hostname='hermes-poc-01')
        self.request.assert_not_called()
    def test_expired_day_refused(self):
        with self.assertRaises(TestCallRefused):self.run_case(today=date(2026,10,2))
    def test_extra_retry_voicemail_or_recipient_refused(self):
        for k,v in [('retry',{'wait':10}),('phone_number','+15555550123'),('record',True),('voicemail',{'action':'leave_message'})]:
            with self.assertRaises(TestCallRefused):self.run_case(body=dict(self.body,**{k:v}))
    def test_missing_voice_and_wrong_caller_refused(self):
        for voice,caller in [('',CALLER),('SYN-VOICE','+15555550123')]:
            with self.assertRaises(TestCallRefused):payload(target=TARGET,voice_id=voice,caller_id=caller)
    def test_no_redial_or_voicemail(self):
        self.assertNotIn('retry',self.body);self.assertEqual({'action':'hangup'},self.body['voicemail']);self.assertFalse(self.body['record']);self.assertEqual(1,self.body['max_duration'])
    def test_public_state_directory_refused(self):
        self.root.chmod(0o755)
        with self.assertRaises(TestCallRefused):self.run_case()
    def test_tmp_state_refused(self):
        with self.assertRaises(TestCallRefused):self.run_case(db_path=self.path/'calls.sqlite')
    def test_get_failure_not_duplicate(self):
        self.request.side_effect=[{'call_id':'SYN-CALL'},TimeoutError()];r=self.run_case();self.assertEqual('accepted_readback_unverified',r['state']);self.run_case();self.assertEqual(2,self.request.call_count)

if __name__=='__main__':unittest.main()
