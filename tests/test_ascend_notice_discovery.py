import unittest,base64,tempfile,json
from pathlib import Path
from robie_job_engine.ascend_notice_discovery import *
def msg(mid='m',subject='Past due payment for Example LLC',sender='notice@useascend.com',body='A past due payment',unread=True,mime='text/plain'):
 return {'id':mid,'internalDate':'1','labelIds':['UNREAD'] if unread else [],'payload':{'mimeType':mime,'headers':[{'name':'Subject','value':subject},{'name':'From','value':sender}],'body':{'data':base64.urlsafe_b64encode(body.encode()).decode()}}}
class Result:
 def __init__(self,v):self.v=v
 def execute(self):
  if isinstance(self.v,Exception):raise self.v
  return self.v
class Gmail:
 def __init__(self,pages,messages):self.pages=pages;self.msgs=messages;self.calls=[]
 def users(self):return self
 def messages(self):return self
 def list(self,**kw):self.calls.append(('list',kw));return Result(self.pages[kw.get('pageToken','first')])
 def get(self,**kw):self.calls.append(('get',kw));return Result(self.msgs[kw['id']])
 def modify(self,**kw):raise AssertionError('no Gmail writes')
 def send(self,**kw):raise AssertionError('no Gmail sends')
class Tests(unittest.TestCase):
 def test_direct_sender_is_not_authorized(self):
  d=discovery(msg());self.assertEqual(d['classification'],'review');self.assertFalse(d['source_verified']);self.assertFalse(d['may_file'])
 def test_forward(self):
  d=discovery(msg(sender='staff@example.com',subject='Fwd: Payment failed for Example',body='From: Ascend <notice@useascend.com>\nPayment failed'));self.assertEqual(d['notice_family'],'payment_failed');self.assertEqual(d['sender_claim'],'forward_or_link')
 def test_html_forward_link(self):
  d=discovery(msg(sender='staff@example.com',subject='Fwd: Notice',mime='text/html',body='<div>Past due payment</div><a href="https://dashboard.useascend.com/programs/abc">Program</a>'));self.assertEqual(d['notice_family'],'past_due')
 def test_spoof_domain(self):
  self.assertEqual(discovery(msg(sender='notice@useascend.com.evil.invalid'))['classification'],'unrelated')
 def test_spoof_url(self):
  self.assertEqual(discovery(msg(sender='x@example.com',body='https://useascend.com.evil.invalid'))['classification'],'unrelated')
 def test_unrelated_cancellation(self):
  self.assertEqual(discovery(msg(sender='carrier@example.com',subject='Notice of cancellation'))['classification'],'unrelated')
 def test_unknown_ascend_reviews(self):
  self.assertEqual(discovery(msg(subject='New unfamiliar notice',body=''))['notice_family'],'unknown')
 def test_multiple_families_reviews(self):
  self.assertEqual(discovery(msg(subject='Notice of cancellation',body='Payment failed'))['notice_family'],'ambiguous')
 def test_refund_family(self):
  self.assertEqual(discovery(msg(subject='A refund to your customer is on the way',body=''))['notice_family'],'return_premium')
 def test_read_message_included(self):
  self.assertEqual(discovery(msg(unread=False))['read_state'],'read')
 def test_attachment_not_downloaded(self):
  m=msg();m['payload']['parts']=[{'filename':'notice.pdf','mimeType':'application/pdf','body':{'attachmentId':'secret'}}];self.assertTrue(discovery(m)['attachment_text_unread'])
 def test_script_ignored(self):
  d=discovery(msg(subject='Unknown',mime='text/html',body='<script>Payment failed</script>'));self.assertEqual(d['notice_family'],'unknown')
 def test_queue_durable_idempotent(self):
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/'queue';q=ReviewQueue(path);q.put('hello@example.com','m',discovery(msg()));q.put('hello@example.com','m',discovery(msg()));q.close();q=ReviewQueue(path);self.assertEqual(len(q.rows()),1);q.close()
 def test_queue_mailbox_identity(self):
  q=ReviewQueue(':memory:');q.put('one','m',{});q.put('two','m',{});self.assertEqual(len(q.rows()),2)
 def scan(self,g,q=None,**kw):return scan(g,'hello@example.com',q or ReviewQueue(':memory:'),start_date='2026-09-01',end_date='2026-10-04',**kw)
 def test_all_pages_more_than25(self):
  ms={str(i):msg(str(i)) for i in range(53)};g=Gmail({'first':{'messages':[{'id':str(i)} for i in range(25)],'nextPageToken':'two'},'two':{'messages':[{'id':str(i)} for i in range(25,53)]}},ms);r=self.scan(g);self.assertTrue(r['complete']);self.assertEqual(r['review'],53);self.assertEqual(r['pages'],2)
 def test_no_unread_or_recent_cap(self):
  query=search_query('2026-09-01','2026-10-04');self.assertNotIn('is:unread',query);self.assertNotIn('newer_than',query)
 def test_duplicate_pages_not_duplicate_queue(self):
  g=Gmail({'first':{'messages':[{'id':'m'}],'nextPageToken':'two'},'two':{'messages':[{'id':'m'}]}},{'m':msg()});self.assertEqual(self.scan(g)['fetched'],1)
 def test_loop_incomplete(self):
  g=Gmail({'first':{'nextPageToken':'two'},'two':{'nextPageToken':'two'}},{});self.assertFalse(self.scan(g)['complete'])
 def test_get_error_not_silent(self):
  g=Gmail({'first':{'messages':[{'id':'m'}]}},{'m':TimeoutError()});r=self.scan(g);self.assertFalse(r['complete']);self.assertTrue(r['errors'])
 def test_list_error_not_silent(self):
  self.assertFalse(self.scan(Gmail({'first':TimeoutError()},{}))['complete'])
 def test_page_limit_incomplete(self):
  self.assertFalse(self.scan(Gmail({'first':{'nextPageToken':'two'}},{}),max_pages=1)['complete'])
 def test_wrong_returned_id(self):
  self.assertFalse(self.scan(Gmail({'first':{'messages':[{'id':'m'}]}},{'m':msg('wrong')}))['complete'])
 def test_window_validation(self):
  with self.assertRaises(ValueError):search_query('2026-10-04','2026-09-01')
class CliTests(unittest.TestCase):
 def args(self,path):return ['--mailbox','hello@example.com','--start-date','2026-09-01','--end-date','2026-10-04','--queue-db',path,'--delegation-service-account','sa@example.com']
 def test_cli_requires_test_before_factory(self):
  from unittest.mock import patch,MagicMock
  f=MagicMock()
  with patch.dict('os.environ',{'ROBIE_ENV':'PRODUCTION'}):
   with self.assertRaises(SystemExit) as e:main(self.args('unused'),service_factory=f)
  self.assertEqual(e.exception.code,2);f.assert_not_called()
 def test_cli_only_readonly_scope(self):
  from unittest.mock import patch,MagicMock
  with tempfile.TemporaryDirectory() as td:
   f=MagicMock(return_value=Gmail({'first':{}},{}))
   with patch.dict('os.environ',{'ROBIE_ENV':'TEST'}):self.assertEqual(main(self.args(str(Path(td)/'q')),service_factory=f),0)
   f.assert_called_once_with('sa@example.com','hello@example.com',modify=False)
 def test_brand_without_verified_sender_still_review(self):
  d=discovery(msg(sender='staff@example.com',body='Ascend: Payment failed'));self.assertEqual(d['classification'],'review');self.assertFalse(d['source_verified'])
 def test_error_stays_in_queue(self):
  q=ReviewQueue(':memory:');g=Gmail({'first':{'messages':[{'id':'m'}]}},{'m':TimeoutError()});r=scan(g,'hello@example.com',q,start_date='2026-09-01',end_date='2026-10-04');self.assertFalse(r['complete']);self.assertEqual(q.rows()[0]['item']['reason'],'fetch_or_decode_failed')
