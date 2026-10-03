import unittest
from robie_job_engine.ascend_delivery_state import *

class FakeDestination:
 synthetic=True
 def __init__(self):self.sent=0;self.saved={};self.fail=False;self.corrupt=False
 def find_by_key(self,k):return self.saved.get(k,{})
 def send(self,e,k):
  self.sent+=1
  r={'ids':{n:f'{n}-{k}' for n in REQUIRED[e['kind']]}}
  self.saved[k]=r
  if self.fail:raise TimeoutError('lost receipt')
  self.event=e
  return r
 def readback(self,ids):
  e=getattr(self,'event',None)
  if e is None:return {}
  return {n:{'id':i,'source_key':e['key'] if not self.corrupt else 'wrong','applicant_id':e.get('applicant_id')} for n,i in ids.items()}

class Tests(unittest.TestCase):
 def event(self,kind='cancellation',**kw):return dict(key='a',kind=kind,applicant_id='123',**kw)
 def test_unmatched_can_retry_after_mapping_without_resend(self):
  p=FakeDestination();l={};r=ReliableDelivery(l,p);e=self.event();e['applicant_id']=None
  self.assertEqual(r.process(e),'unmatched_retryable');self.assertEqual(p.sent,0)
  e['applicant_id']='123';self.assertEqual(r.process(e),'delivered_readback');self.assertEqual(r.process(e),'delivered_skip');self.assertEqual(p.sent,1)
 def test_default_is_staged_not_delivered(self):
  l={};self.assertEqual(ReliableDelivery(l).process(self.event()),'staged_no_live_destination');self.assertFalse(l['a'].delivered)
 def test_supplier_disabled_even_with_port(self):
  p=FakeDestination();self.assertEqual(ReliableDelivery({},p).process(self.event('supplier_payout')),'supplier_accounting_disabled');self.assertEqual(p.sent,0)
 def test_fake_success_is_not_receipt(self):
  p=FakeDestination();p.send=lambda e,k:{'status':'success'};l={};r=ReliableDelivery(l,p)
  self.assertEqual(r.process(self.event()),'destination_unverified');self.assertFalse(l['a'].delivered)
 def test_readback_wrong_source_fails(self):
  p=FakeDestination();p.corrupt=True;l={};r=ReliableDelivery(l,p);self.assertEqual(r.process(self.event()),'destination_unverified');self.assertFalse(l['a'].delivered)
 def test_timeout_never_resends(self):
  p=FakeDestination();p.fail=True;l={};r=ReliableDelivery(l,p);e=self.event()
  self.assertEqual(r.process(e),'destination_error_TimeoutError');r.process(e);self.assertEqual(p.sent,1)
 def test_legacy_unknown_attempt_readback_only(self):
  p=FakeDestination();r=ReliableDelivery({},p);self.assertEqual(r.process(self.event(legacy_uncertain=True)),'attempt_uncertain_recovery_only');self.assertEqual(p.sent,0)
 def test_recovery_after_lost_receipt(self):
  p=FakeDestination();p.fail=True;l={};r=ReliableDelivery(l,p);e=self.event();r.process(e);p.event=e
  self.assertEqual(r.process(e),'delivered_readback');self.assertEqual(p.sent,1)
 def test_all_ids_required(self):
  p=FakeDestination();p.send=lambda e,k:{'ids':{'note_id':'only'}}
  self.assertEqual(ReliableDelivery({},p).process(self.event()),'destination_unverified')
 def test_pagination(self):
  calls=[]
  def get(path,q):
   calls.append(q);return {'data':[len(calls)],'pagination':{'next_cursor':'two'}} if len(calls)==1 else {'data':[2]}
  self.assertEqual(paginate(get,'/v1/payouts'),[1,2]);self.assertEqual(calls[1]['starting_after'],'two')
 def test_cursor_loop(self):
  with self.assertRaises(ValueError):paginate(lambda p,q:{'data':[],'next_cursor':'x'},'/v1/payouts')
 def test_external_next_url(self):
  with self.assertRaises(ValueError):paginate(lambda p,q:{'data':[],'next':'https://bad.invalid'},'/v1/payouts')
 def test_more_without_cursor(self):
  with self.assertRaises(ValueError):paginate(lambda p,q:{'data':[],'has_more':True},'/v1/payouts')
 def test_bad_readback_applicant(self):
  p=FakeDestination();p.readback=lambda ids:{n:{'id':i,'source_key':'a','applicant_id':'WRONG'} for n,i in ids.items()}
  self.assertEqual(ReliableDelivery({},p).process(self.event()),'destination_unverified')
 def test_persist_before_attempt(self):
  l={};p=FakeDestination();old=p.send
  def send(e,k):self.assertTrue(l[k].attempted);return old(e,k)
  p.send=send;self.assertEqual(ReliableDelivery(l,p).process(self.event()),'delivered_readback')

class ReplayTests(unittest.TestCase):
 def test_exact_53_shapes(self):
  from robie_job_engine.ascend_delivery_replay import replay
  self.assertEqual(replay()['source_shapes'],53)
 def test_unknown_is_not_delivered(self):
  l={};self.assertEqual(ReliableDelivery(l).process({'key':'u','kind':'unsupported'}),'unsupported_type_status');self.assertFalse(l['u'].delivered)

class DurabilityTests(unittest.TestCase):
 def test_attempt_survives_restart_and_precedes_send(self):
  import tempfile, os
  with tempfile.TemporaryDirectory() as td:
   path=os.path.join(td,'candidate.db'); ledger=DurableLedger(path); port=FakeDestination()
   def send(e,k):
    other=DurableLedger(path);self.assertTrue(other[k].attempted);other.close()
    raise TimeoutError('unknown')
   port.send=send
   e={'key':'durable','kind':'cancellation','applicant_id':'x'}
   ReliableDelivery(ledger,port).process(e);ledger.close()
   ledger=DurableLedger(path);port=FakeDestination()
   self.assertEqual(ReliableDelivery(ledger,port).process(e),'attempt_uncertain_recovery_only')
   self.assertEqual(port.sent,0);ledger.close()
 def test_live_port_requires_durable_storage(self):
  p=FakeDestination();p.synthetic=False
  self.assertEqual(ReliableDelivery({},p).process({'key':'x','kind':'commission_payout'}),'durable_storage_required')
  self.assertEqual(p.sent,0)

class FeedBoundaryTests(unittest.TestCase):
 def test_full_page_without_explicit_end_fails_closed(self):
  with self.assertRaises(ValueError): paginate(lambda p,q:{'data':[{}]*50},'/v1/payouts')
 def test_legacy_sync_disabled(self):
  import ast, pathlib
  src=pathlib.Path('robie_job_engine/ascend_sync.py').read_text()
  tree=ast.parse(src)
  method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_legacy_sync_once_DISABLED')
  self.assertIsInstance(method.body[0],ast.Raise)
 def test_no_live_writes_in_candidate_runner(self):
  import ast, pathlib
  tree=ast.parse(pathlib.Path('robie_job_engine/ascend_sync.py').read_text())
  method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='sync_once')
  attrs=[n.func.attr for n in ast.walk(method) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)]
  self.assertFalse(set(attrs)&{'post_note','create_task','record_trust_bank_deposit','record_supplier_payout_bill','log_event'})
