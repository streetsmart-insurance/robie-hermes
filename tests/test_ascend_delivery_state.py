import unittest,tempfile,os
from pathlib import Path
from robie_job_engine.ascend_delivery_state import *
class Port:
 synthetic=True
 def __init__(self,event):self.event=event;self.ids={};self.sent=[];self.fail=None;self.bad=False;self.complete=True
 def find_by_key(self,key):
  fields=PAYOUT_BINDING if self.event['kind']=='commission_payout' else ('applicant_id',)
  return {'ids':dict(self.ids),'source_key':key,'binding':{f:self.event.get(f) for f in fields},'authoritative_absent':[c for c in REQUIRED[self.event['kind']] if c not in self.ids] if self.complete else []}
 def send_component(self,e,k,c):
  self.sent.append(c)
  if self.fail==c:raise TimeoutError('unknown')
  self.ids[c]=c+'-1';return {'ids':dict(self.ids)}
 def readback(self,ids):
  return {c:dict(self.event,id=i,source_key='wrong' if self.bad else self.event['key']) for c,i in ids.items()}
class Tests(unittest.TestCase):
 def event(self,kind='cancellation'):
  return dict(key='e',kind=kind,applicant_id='app',realm_id='r',account_id='a',income_account_id='i',payee_type='Vendor',payee_id='p',amount_cents=123,currency='USD')
 def test_unmatched_then_mapping(self):
  e=self.event();p=Port(e);l=DurableLedger(':memory:');r=ReliableDelivery(l,p);e['applicant_id']=None
  self.assertEqual(r.process(e),'unmatched_retryable');e['applicant_id']='app';self.assertEqual(r.process(e),'delivered_readback');self.assertEqual(p.sent,['note_id','task_id']);r.process(e);self.assertEqual(len(p.sent),2)
 def test_default_stage(self):
  l={};self.assertEqual(ReliableDelivery(l).process(self.event()),'staged_no_live_destination');self.assertFalse(l['e'].delivered)
 def test_supplier_disabled(self):
  e=self.event('supplier_payout');p=Port(e);self.assertEqual(ReliableDelivery({},p).process(e),'supplier_accounting_disabled');self.assertEqual(p.sent,[])
 def test_synthetic_attribute_cannot_bypass(self):
  e=self.event();p=Port(e);self.assertEqual(ReliableDelivery({},p).process(e),'durable_storage_required');self.assertEqual(p.sent,[])
 def test_partial_note_sends_only_task(self):
  e=self.event();p=Port(e);p.ids={'note_id':'old-note'};l=DurableLedger(':memory:')
  self.assertEqual(ReliableDelivery(l,p).process(e),'delivered_readback');self.assertEqual(p.sent,['task_id']);self.assertEqual(l['e'].destination_ids['note_id'],'old-note')
 def test_partial_unknown_absence_never_sends(self):
  e=self.event();p=Port(e);p.ids={'note_id':'old'};p.complete=False
  self.assertEqual(ReliableDelivery(DurableLedger(':memory:'),p).process(e),'component_absence_unverified');self.assertEqual(p.sent,[])
 def test_timeout_restart_never_resends(self):
  e=self.event();p=Port(e);p.fail='task_id'
  with tempfile.TemporaryDirectory() as td:
   path=os.path.join(td,'d.db');l=DurableLedger(path);r=ReliableDelivery(l,p);self.assertEqual(r.process(e),'destination_error_TimeoutError');l.close()
   l=DurableLedger(path);self.assertEqual(ReliableDelivery(l,p).process(e),'attempt_uncertain_recovery_only');self.assertEqual(p.sent,['note_id','task_id']);p.ids['task_id']='landed';self.assertEqual(ReliableDelivery(l,p).process(e),'delivered_readback');l.close()
 def test_commit_precedes_each_send(self):
  e=self.event();p=Port(e)
  with tempfile.TemporaryDirectory() as td:
   path=os.path.join(td,'d.db');l=DurableLedger(path);old=p.send_component
   def send(e,k,c):
    other=DurableLedger(path);self.assertIn(c,other[k].attempted_components);other.close();return old(e,k,c)
   p.send_component=send;self.assertEqual(ReliableDelivery(l,p).process(e),'delivered_readback');l.close()
 def test_bad_source_no_delivery(self):
  e=self.event();p=Port(e);p.ids={'note_id':'old'};p.bad=True
  self.assertEqual(ReliableDelivery(DurableLedger(':memory:'),p).process(e),'existing_destination_unverified');self.assertEqual(p.sent,[])
 def test_fake_success_no_delivery(self):
  e=self.event();p=Port(e);p.send_component=lambda *a:{'status':'success'};l=DurableLedger(':memory:');self.assertEqual(ReliableDelivery(l,p).process(e),'destination_unverified');self.assertFalse(l['e'].delivered)
 def test_payout_null_applicant_not_identity(self):
  e=self.event('commission_payout');e['applicant_id']=None;e.pop('payee_id');p=Port(e);self.assertEqual(ReliableDelivery(DurableLedger(':memory:'),p).process(e),'destination_binding_required');self.assertEqual(p.sent,[])
 def test_payout_all_exact_bindings(self):
  e=self.event('commission_payout');e['applicant_id']=None;p=Port(e);self.assertEqual(ReliableDelivery(DurableLedger(':memory:'),p).process(e),'delivered_readback')
 def test_wrong_payout_amount(self):
  e=self.event('commission_payout');p=Port(e);p.ids={'deposit_id':'d'};old=p.readback
  p.readback=lambda ids:{k:dict(v,amount_cents=999) for k,v in old(ids).items()}
  self.assertEqual(ReliableDelivery(DurableLedger(':memory:'),p).process(e),'existing_destination_unverified')
 def test_legacy_unknown_never_sends(self):
  e=self.event();e['legacy_uncertain']=True;p=Port(e);self.assertEqual(ReliableDelivery(DurableLedger(':memory:'),p).process(e),'attempt_uncertain_recovery_only');self.assertEqual(p.sent,[])
 def test_pagination(self):
  calls=[]
  def get(p,q):calls.append(q);return {'data':[1],'next_cursor':'two'} if len(calls)==1 else {'data':[2]}
  self.assertEqual(paginate(get,'/v1/payouts'),[1,2]);self.assertEqual(calls[1]['starting_after'],'two')
 def test_cursor_loop(self):
  with self.assertRaises(ValueError):paginate(lambda p,q:{'data':[],'next_cursor':'x'},'/v1/payouts')
 def test_next_url(self):
  with self.assertRaises(ValueError):paginate(lambda p,q:{'data':[],'next':'https://bad.invalid'},'/v1/payouts')
 def test_more_no_cursor(self):
  with self.assertRaises(ValueError):paginate(lambda p,q:{'data':[],'has_more':True},'/v1/payouts')
 def test_full_page_no_contract(self):
  with self.assertRaises(ValueError):paginate(lambda p,q:{'data':[{}]*50},'/v1/payouts')
 def test_replay_53(self):
  from robie_job_engine.ascend_delivery_replay import replay
  self.assertEqual(replay()['source_shapes'],53)
class ReconciliationTests(unittest.TestCase):
 def test_readonly_db_unchanged(self):
  import hashlib
  from robie_job_engine.ascend_legacy_reconciliation import read_only_plan
  with tempfile.TemporaryDirectory() as td:
   p=os.path.join(td,'legacy.db');db=sqlite3.connect(p);db.execute('CREATE TABLE ascend_synced_events(event_id TEXT,status TEXT,applicant_id TEXT,raw_data_json TEXT)');db.execute('INSERT INTO ascend_synced_events VALUES (?,?,?,?)',('x','SUCCESS',None,'{"qbo":{"status":"staged"}}'));db.commit();db.close()
   before=open(p,'rb').read();result=read_only_plan(p);self.assertEqual(before,open(p,'rb').read());self.assertEqual(result[0]['action'],'staged_not_delivered_destination_lookup');self.assertFalse(result[0]['may_send'])
 def test_unmatched_and_flagged(self):
  from robie_job_engine.ascend_legacy_reconciliation import plan_rows
  r=plan_rows([{'event_id':'u','status':'UNMATCHED'},{'event_id':'f','status':'FLAGGED'}]);self.assertEqual(r[0]['action'],'remap_then_destination_lookup');self.assertEqual(r[1]['action'],'uncertain_destination_lookup_only')
class DisabledScheduledTests(unittest.TestCase):
 def test_daemon_stops_before_store(self):
  from unittest.mock import patch
  from robie_job_engine import ascend_sync
  with patch.object(ascend_sync,'AscendSyncStore') as store:
   with self.assertRaisesRegex(RuntimeError,'ASCEND_SYNC_DISABLED'):ascend_sync.run_daemon()
   store.assert_not_called()
 def test_cli_nonzero_no_store(self):
  from unittest.mock import patch
  from robie_job_engine import ascend_sync
  with patch.object(ascend_sync,'AscendSyncStore') as store,patch('sys.argv',['ascend_sync','--once']):
   with self.assertRaises(SystemExit) as exit:ascend_sync.main()
   self.assertEqual(exit.exception.code,2);store.assert_not_called()

class PreviewTests(unittest.TestCase):
 def test_read_stage_preview_no_destinations_or_ledger(self):
  from unittest.mock import MagicMock
  from robie_job_engine.ascend_sync import AscendEZLynxSyncManager
  api=MagicMock();api.fetch_cancelation_returns.return_value=[{'id':'c','billable':{'policy_number':'p'}}];api.fetch_programs.return_value=[];api.fetch_payouts.return_value=[]
  matcher=MagicMock();matcher.match_account.return_value=('app',{})
  manager=object.__new__(AscendEZLynxSyncManager);manager.api=api;manager.matcher=matcher;manager.poster=MagicMock();manager.store=MagicMock()
  result=manager.preview_once();self.assertEqual(result['writes'],0);self.assertEqual(result['delivered'],0);self.assertEqual(result['source_seen'],1);manager.poster.post_note.assert_not_called();manager.poster.create_task.assert_not_called();manager.store.record_synced_event.assert_not_called()
class HonestAdaptersTests(unittest.TestCase):
 def test_supplier_adapter_never_claims_success(self):
  import inspect
  from robie_job_engine.quickbooks_api import QuickBooksApiClient
  src=inspect.getsource(QuickBooksApiClient.record_supplier_payout_bill)
  self.assertIn('"status": "disabled"',src);self.assertNotIn('"status": "success"',src)
 def test_task_fallback_never_reports_success_without_a_client(self):
  # #759's contract: a failed task create is an error, never success.
  from robie_job_engine.ezlynx_note_poster import EZLynxAgreementPoster
  with tempfile.TemporaryDirectory() as td:
   r=EZLynxAgreementPoster(renewal_root=str(Path(td)/'missing')).create_task('1','t','d')
  self.assertEqual(r['status'],'error');self.assertNotEqual(r.get('status'),'success')
 def test_reconciliation_actual_store_schema(self):
  from robie_job_engine.ascend_sync import AscendSyncStore
  from robie_job_engine.ascend_legacy_reconciliation import read_only_plan
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'db';store=AscendSyncStore(p);store.record_synced_event('staged','payout',status='SUCCESS',raw_data={'qbo':{'status':'staged'}})
   r=read_only_plan(p);self.assertEqual(r[0]['action'],'staged_not_delivered_destination_lookup')
