import unittest,json,hmac,hashlib
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock
from robie_job_engine.phone_runtime_ports import *
class Ports(unittest.TestCase):
 def setUp(self):
  self.now=datetime(2026,10,1,15,tzinfo=timezone.utc);self.key=b'SYNTHETIC_TEST_KEY_NOT_A_SECRET_1234'
  self.payload={'grant_id':'SYN-GRANT','evidence_id':'SYN-MSG','principal':'owner','channel':'iMessage','plan_digest':'SYN-DIGEST','expires':(self.now+timedelta(hours=1)).isoformat(),'allow_client':True,'environment':'TEST'}
 def resolver(self):
  p=self.payload;raw=json.dumps(p,sort_keys=True,separators=(',',':')).encode();r={'payload':p,'signature':hmac.new(self.key,raw,hashlib.sha256).hexdigest()}
  return SignedApprovalResolver({'SYN-GRANT':r},self.key,lambda:self.now)
 def test_signed_binding(self):self.assertEqual('SYN-DIGEST',self.resolver().resolve('SYN-GRANT').plan_digest)
 def test_tampered_binding(self):
  r=self.resolver();r.records['SYN-GRANT']['payload']=dict(self.payload,plan_digest='CHANGED')
  with self.assertRaises(Refused):r.resolve('SYN-GRANT')
 def test_unsigned_refused(self):
  r=self.resolver();r.records['SYN-GRANT']['signature']=''
  with self.assertRaises(Refused):r.resolve('SYN-GRANT')
 def test_peer_source_refused(self):
  self.payload['channel']='agent_peer'
  with self.assertRaises(Refused):self.resolver().resolve('SYN-GRANT')
 def test_prod_and_expired_refused(self):
  for k,v in [('environment','PRODUCTION'),('expires',self.now.isoformat())]:
   old=self.payload[k];self.payload[k]=v
   with self.assertRaises(Refused):self.resolver().resolve('SYN-GRANT')
   self.payload[k]=old
 def test_missing_key_refused(self):
  with self.assertRaises(Refused):SignedApprovalResolver({},b'',lambda:self.now)
 def test_directory_freshness(self):
  r={'id':'SYN-DIR','audience':'finance','phone':'+15555550123','source_ref':'SYN-SOURCE','verified_at':self.now.isoformat()};d=VerifiedDirectory(lambda _:r,lambda:self.now,24)
  self.assertEqual('finance',d.resolve('SYN-DIR')['audience']);r['verified_at']=(self.now-timedelta(hours=25)).isoformat()
  with self.assertRaises(Refused):d.resolve('SYN-DIR')
 def test_source_error_and_unknown_schema_not_empty(self):
  for reader in [Mock(side_effect=TimeoutError()),lambda _:{}]:
   with self.assertRaises((Refused,TimeoutError)):DiscussionSource(reader,['SYN-APP']).cards('SYN-APP')
 def test_source_cross_applicant_refused(self):
  with self.assertRaises(Refused):DiscussionSource(lambda _:[{'applicantId':'WRONG','discussionNote':{}}],['SYN-APP']).cards('SYN-APP')
 def test_live_effects_disabled(self):
  d=LiveEffectsDisabled()
  with self.assertRaises(Refused):d({})
  with self.assertRaises(Refused):d.append()
if __name__=='__main__':unittest.main()
