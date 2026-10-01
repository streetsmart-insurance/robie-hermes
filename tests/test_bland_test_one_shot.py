"""Synthetic only. No call, credential access or remote traffic."""
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from robie_job_engine.bland_test_one_shot import payload,dispatch_once,TestCallRefused,TARGET,CALLER,main

class OneShotSafety(unittest.TestCase):
    def setUp(self):
        # Exercise the explicitly forbidden /tmp path on every platform;
        # macOS defaults tempfile to /var/folders, which is a different path.
        self.tmp=tempfile.TemporaryDirectory(dir='/tmp');self.path=Path(self.tmp.name);self.path.chmod(0o700)
        # Tests may use a private temp directory; the production contract refuses
        # /tmp explicitly, so use an isolated home-directory test location.
        self.home=tempfile.TemporaryDirectory(dir=str(Path.home()),prefix='bland-proof-');self.root=Path(self.home.name);self.root.chmod(0o700)
        self.body=payload(target=TARGET,voice_id='SYN-VOICE',caller_id=CALLER)
        self.request=Mock(side_effect=[{'call_id':'SYN-CALL'},{'status':'queued'}])
    def tearDown(self):self.tmp.cleanup();self.home.cleanup()
    def run_case(self,**kw):
        args=dict(db_path=self.root/'calls.sqlite',test_id='SYN-TEST',approved_day=date(2026,10,1),hostname='hermes-test-01',body=self.body,api_key='SYN-KEY',request=self.request,clock=lambda: datetime(2026,10,1,15,tzinfo=ZoneInfo('America/New_York')));args.update(kw);return dispatch_once(**args)
    def test_one_post_then_get(self):
        r=self.run_case();self.assertEqual('SYN-CALL',r['call_id']);self.assertEqual(['POST','GET'],[c.args[0] for c in self.request.call_args_list])
    def test_duplicate_never_dispatches(self):
        self.run_case();r=self.run_case();self.assertFalse(r['new_dispatch']);self.assertEqual(2,self.request.call_count)
    def test_different_test_id_never_redispatches(self):
        self.run_case();r=self.run_case(test_id="SYN-OTHER")
        self.assertFalse(r["new_dispatch"]);self.assertEqual(2,self.request.call_count)
    def test_uncertain_post_never_retries(self):
        self.request.side_effect=TimeoutError();r=self.run_case();self.assertEqual('dispatch_unknown',r['state']);self.run_case();self.assertEqual(1,self.request.call_count)
    def test_missing_call_id_never_retries(self):
        self.request.side_effect=[{}];r=self.run_case();self.assertFalse(r['retry_allowed']);self.run_case();self.assertEqual(1,self.request.call_count)
    def test_prod_host_refused(self):
        with self.assertRaises(TestCallRefused):self.run_case(hostname='hermes-poc-01')
        self.request.assert_not_called()
    def test_expired_day_refused(self):
        with self.assertRaises(TestCallRefused):self.run_case(clock=lambda: datetime(2026,10,2,15,tzinfo=ZoneInfo('America/New_York')))
    def test_approved_day_follows_eastern_clock_not_the_cli_value(self):
        # 2026-10-02 03:00 UTC is still 2026-10-01 in America/New_York.
        self.run_case(clock=lambda: datetime(2026,10,2,3,tzinfo=timezone.utc))
        with self.assertRaises(TestCallRefused):self.run_case(clock=lambda: datetime(2026,10,2,5,tzinfo=timezone.utc))
    def test_dry_run_prints_payload_without_transport(self):
        buf=StringIO()
        argv=['prog','--voice-id','SYN-VOICE','--test-id','dry','--approved-day','2026-10-01','--state-dir',str(self.root)]
        with patch('sys.argv',argv), patch('robie_job_engine.bland_transport.post_call',side_effect=AssertionError('post')), patch('robie_job_engine.bland_transport.get_call',side_effect=AssertionError('get')), redirect_stdout(buf):
            main()
        printed=json.loads(buf.getvalue())
        self.assertTrue(printed['dry_run']);self.assertEqual('SYN-VOICE',printed['payload']['voice']);self.assertEqual(1,printed['payload']['max_duration'])
    def test_execute_refuses_before_any_secret_read_when_live_flag_is_off(self):
        def urlopen(*args,**kwargs):
            raise AssertionError('secret or network read')
        argv=['prog','--voice-id','SYN-VOICE','--test-id','dry','--approved-day','2026-10-01','--state-dir',str(self.root),'--execute']
        for flag in ('0', None):
            env={'ROBIE_ENV':'TEST'}
            if flag is None:env.pop('ROBIE_PHONE_LIVE_CALLS',None)
            else:env['ROBIE_PHONE_LIVE_CALLS']=flag
            with patch('sys.argv',argv), patch('urllib.request.urlopen',urlopen), patch('socket.gethostname',return_value='hermes-test-01'), patch.dict(os.environ,env,clear=False):
                os.environ.pop('ROBIE_PHONE_LIVE_CALLS',None) if flag is None else None
                with self.assertRaises(TestCallRefused):
                    main()
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
