"""Synthetic safety proof. No vendor/network credentials or actual notes."""
import unittest,tempfile
from pathlib import Path
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from unittest.mock import Mock
from robie_job_engine.phone_controls import *

class Safety(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=Path.home(),prefix='phone-proof-');self.root=Path(self.temp.name);self.root.chmod(0o700)
  self.now=datetime(2026,10,1,15,tzinfo=timezone.utc)
  self.plan=Plan('SYN-CAMPAIGN','+15555550123','carrier','SYN-DIR','SYN-SCRIPT','','SYN-APP','SYN-DISC','+15555550124',2)
  self.grant=Grant('SYN-USER-MESSAGE',self.plan.digest(),self.now+timedelta(hours=2))
  self.approvals=Mock();self.approvals.resolve.side_effect=lambda _:self.grant
  self.directory=Mock();self.directory.resolve.return_value={'audience':'carrier','phone':self.plan.target}
  self.dispatch=Mock(side_effect=[{'call_id':'SYN-CALL-1'},{'call_id':'SYN-CALL-2'}]);self.notes=Mock()
  self.c=Controls(self.root/'calls.sqlite',Window('America/New_York',9,17),self.approvals,self.directory,self.dispatch,self.notes,lambda:self.now)
 def tearDown(self):self.temp.cleanup()
 def start(self):return self.c.start(self.plan,'SYN-GRANT')
 def detail(self):return {'call_id':'SYN-CALL-1','ended_at':self.now.isoformat(),'outcome':'voicemail_no_message'}
 def note(self):
  detail=self.detail();body=f"Call ended {detail['ended_at']}. Call ID: SYN-CALL-1. Outcome: voicemail_no_message."
  self.notes.find.return_value=None;self.notes.append.return_value='SYN-NOTE';self.notes.read.return_value={'applicant_id':'SYN-APP','discussion_id':'SYN-DISC','body':body}
  return self.c.finish(self.plan,'SYN-CALL-1',detail)
 def test_exact_script_gate(self):
  self.grant=replace(self.grant,plan_digest='WRONG')
  with self.assertRaises(Refused):self.start()
  self.dispatch.assert_not_called()
 def test_expired_approval(self):
  self.grant=replace(self.grant,expires=self.now)
  with self.assertRaises(Refused):self.start()
 def test_recipient_and_voicemail_changes_invalidate(self):
  for p in [replace(self.plan,target='+15555550125'),replace(self.plan,voicemail_script='changed'),replace(self.plan,script='changed')]:
   with self.assertRaises(Refused):self.c.start(p,'SYN-GRANT')
 def test_weekend_and_hours(self):
  for dt in [datetime(2026,10,3,15,tzinfo=timezone.utc),datetime(2026,10,1,22,tzinfo=timezone.utc)]:
   self.now=dt
   with self.assertRaises(Refused):self.start()
 def test_reviewed_unrestricted_hours_still_requires_approval(self):
  self.plan=replace(self.plan,unrestricted_hours=True)
  with self.assertRaises(Refused):self.start()
  self.now=datetime(2026,10,3,23,tzinfo=timezone.utc);self.grant=replace(self.grant,plan_digest=self.plan.digest(),expires=self.now+timedelta(hours=1));self.start()
 def test_unrestricted_hours_cannot_remove_cooldown(self):
  self.plan=replace(self.plan,unrestricted_hours=True);self.grant=replace(self.grant,plan_digest=self.plan.digest());self.start();self.plan=replace(self.plan,campaign="SYN-2");self.grant=replace(self.grant,evidence_id="SYN-2",plan_digest=self.plan.digest())
  with self.assertRaisesRegex(Refused,"cooldown"):self.start()
 def test_window_timezone(self):
  self.c.window=Window('America/Los_Angeles',9,17)
  with self.assertRaises(Refused):self.start()
 def test_cooldown_new_campaign(self):
  self.start();self.now+=timedelta(hours=1);self.plan=replace(self.plan,campaign='SYN-SECOND');self.grant=replace(self.grant,evidence_id='SYN-2',plan_digest=self.plan.digest())
  with self.assertRaisesRegex(Refused,'cooldown'):self.start()
 def test_24h_boundary(self):
  self.start();self.note();self.now+=timedelta(hours=24);self.plan=replace(self.plan,campaign='SYN-SECOND');self.grant=replace(self.grant,evidence_id='SYN-2',plan_digest=self.plan.digest(),expires=self.now+timedelta(hours=1));self.start();self.assertEqual(2,self.dispatch.call_count)
 def test_finance_exact_directory(self):
  self.plan=replace(self.plan,audience='finance');self.grant=replace(self.grant,plan_digest=self.plan.digest());self.directory.resolve.return_value={'audience':'finance','phone':self.plan.target};self.start()
 def test_unbound_finance_rejected(self):
  self.plan=replace(self.plan,audience='finance');self.grant=replace(self.grant,plan_digest=self.plan.digest())
  with self.assertRaises(Refused):self.start()
 def test_client_requires_one_call_exception(self):
  self.plan=replace(self.plan,audience='client',max_attempts=1);self.grant=replace(self.grant,plan_digest=self.plan.digest())
  with self.assertRaises(Refused):self.start()
  self.grant=replace(self.grant,allow_client=True);self.start();self.directory.resolve.assert_not_called()
 def test_client_redial_rejected(self):
  self.plan=replace(self.plan,audience='client');self.grant=replace(self.grant,allow_client=True,plan_digest=self.plan.digest())
  with self.assertRaises(Refused):self.start()
 def test_one_call_lift_consumed(self):
  self.start();self.start();self.assertEqual(1,self.dispatch.call_count)
 def test_lift_reuse_other_campaign(self):
  self.start();self.note();self.now+=timedelta(hours=24);self.plan=replace(self.plan,campaign='SYN-2');self.grant=replace(self.grant,plan_digest=self.plan.digest(),expires=self.now+timedelta(hours=1))
  with self.assertRaises(sqlite3.IntegrityError):self.start()
 def test_unknown_dispatch_not_retried(self):
  self.dispatch.side_effect=TimeoutError();r=self.start();self.assertEqual('unknown',r['status']);self.start();self.assertEqual(1,self.dispatch.call_count)
 def test_missing_call_id_not_retried(self):
  self.dispatch.side_effect=[{}];self.start();self.start();self.assertEqual(1,self.dispatch.call_count)
 def test_redial_once_hangup_by_default(self):
  self.start();detail=self.detail();self.now+=timedelta(seconds=10);self.c.redial(self.plan,'SYN-GRANT',detail);self.c.redial(self.plan,'SYN-GRANT',detail)
  self.assertEqual(2,self.dispatch.call_count);self.assertEqual({'action':'hangup'},self.dispatch.call_args.args[0]['voicemail'])
 def test_approved_voicemail_script_only_second(self):
  self.plan=replace(self.plan,voicemail_script='SYN-APPROVED-VM');self.grant=replace(self.grant,plan_digest=self.plan.digest());self.start();detail=self.detail();self.now+=timedelta(seconds=10);self.c.redial(self.plan,'SYN-GRANT',detail)
  self.assertEqual({'action':'hangup'},self.dispatch.call_args_list[0].args[0]['voicemail']);self.assertEqual('SYN-APPROVED-VM',self.dispatch.call_args.args[0]['voicemail']['message'])
 def test_redial_delay_and_expired_window(self):
  self.start();d=self.detail()
  with self.assertRaises(Refused):self.c.redial(self.plan,'SYN-GRANT',d)
  self.now+=timedelta(seconds=181)
  with self.assertRaises(Refused):self.c.redial(self.plan,'SYN-GRANT',d)
 def test_redial_human_rejected(self):
  self.start();d=self.detail();d['outcome']='human_reached';self.now+=timedelta(seconds=10)
  with self.assertRaises(Refused):self.c.redial(self.plan,'SYN-GRANT',d)
 def test_dated_bound_note_readback(self):
  self.start();self.assertEqual('verified',self.note()['status']);self.assertIn('2026-10-01',self.notes.append.call_args.args[2])
  self.c.finish(self.plan,'SYN-CALL-1',self.detail());self.assertEqual(1,self.notes.append.call_count)
 def test_note_unknown_not_posted_twice(self):
  self.start();self.notes.find.return_value=None;self.notes.append.side_effect=TimeoutError();self.c.finish(self.plan,'SYN-CALL-1',self.detail());self.c.finish(self.plan,'SYN-CALL-1',self.detail());self.assertEqual(1,self.notes.append.call_count)
 def test_note_destination_changed_rejected(self):
  self.start()
  with self.assertRaises(Refused):self.c.finish(replace(self.plan,applicant_id='WRONG'),'SYN-CALL-1',self.detail())
 def test_note_readback_mismatch(self):
  self.start();self.notes.find.return_value='SYN-NOTE';self.notes.read.return_value={}
  with self.assertRaises(Refused):self.c.finish(self.plan,'SYN-CALL-1',self.detail())
 def test_unresolved_note_holds_new_campaign(self):
  self.start();self.notes.find.return_value=None;self.notes.append.side_effect=TimeoutError();self.c.finish(self.plan,'SYN-CALL-1',self.detail());self.now+=timedelta(hours=24);self.plan=replace(self.plan,campaign='SYN-2',target='+15555550125');self.grant=replace(self.grant,evidence_id='SYN-2',plan_digest=self.plan.digest(),expires=self.now+timedelta(hours=1));self.directory.resolve.return_value={'audience':'carrier','phone':self.plan.target}
  with self.assertRaisesRegex(Refused,'note'):self.start()
 def test_private_directory(self):
  self.root.chmod(0o755)
  with self.assertRaises(Refused):Controls(self.root/'another.sqlite',self.c.window,self.approvals,self.directory,self.dispatch,self.notes,lambda:self.now)

if __name__=='__main__':unittest.main()
