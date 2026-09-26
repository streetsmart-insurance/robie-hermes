"""Entirely synthetic Gmail/QBO inputs. Never records real settlement data."""
import base64
import copy
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from applied_pay import worker

class Request:
    def __init__(self,result): self.result=result
    def execute(self):return self.result
class FakeGmail:
    def __init__(self,messages,attachments):self._messages=messages;self._attachments=attachments;self.calls=[]
    def users(self):return self
    def messages(self):return self
    def attachments(self):return self
    def list(self,**kwargs):self.calls.append('list');return Request({'messages':[{'id':k} for k in self._messages]})
    def get(self,**kwargs):
        if 'messageId' in kwargs:
            self.calls.append('attachment');return Request({'data':enc(self._attachments[kwargs['messageId']])})
        self.calls.append('message');return Request(self._messages[kwargs['id']])
def enc(raw):return base64.urlsafe_b64encode(raw).decode().rstrip('=')
def message(mid='SYN-MSG', name='Batch Settlement Details - 2026-09-24.xlsx'):
    return {'id':mid,'payload':{'headers':[{'name':'From','value':'Applied <noreply_pay@mail.myappliedproducts.com>'},{'name':'Subject','value':'Batch Settlement Details for Primary Account Reconciled on 9/24/2026'},{'name':'Date','value':'Fri, 25 Sep 2026 11:51:48 +0000'}], 'parts':[{'mimeType':'text/plain','body':{'data':enc(b'Transfer ID: SYNTRANSFER123\nTotal Deposit: $100.00')}},{'filename':name,'mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','body':{'attachmentId':'SYN-ATT'}}]}}
class WorkerSafety(unittest.TestCase):
    def setUp(self):
        self.g=FakeGmail({'SYN-MSG':message()},{'SYN-MSG':b'PKsynthetic xlsx'})
        self.parsed={'ref':'SYNTRANSFER123','payout_date':'2026-09-25','net':'100.00','status':'scheduled','lines':[{'type':'sale','amount':'100.00','business':'Example Bakery','payer':'Example Bakery','psp_ref':'SYN-PSP'}],'lines_tie':True}
    @patch.object(worker,'parse')
    def test_source_and_attachment_tie(self,parse):
        parse.return_value=self.parsed
        payouts,proof=worker.fetch_payouts(self.g,start=date(2026,9,24),end=date(2026,9,25))
        self.assertEqual(1,len(payouts));self.assertEqual('SYN-MSG',proof[0]['message_id']);self.assertIn('attachment',self.g.calls)
    @patch.object(worker,'parse')
    def test_non_tying_source_fails_before_qbo(self,parse):
        parse.return_value=dict(self.parsed,lines_tie=False)
        with self.assertRaises(worker.PullError):worker.fetch_payouts(self.g,start=date(2026,9,24),end=date(2026,9,25))
    def test_wrong_or_duplicate_attachment_fails(self):
        self.g._messages['SYN-MSG']['payload']['parts'].append(copy.deepcopy(self.g._messages['SYN-MSG']['payload']['parts'][-1]))
        with self.assertRaises(worker.PullError):worker.fetch_payouts(self.g,start=date(2026,9,24),end=date(2026,9,25))
    def test_sender_spoof_stops(self):
        self.g._messages['SYN-MSG']['payload']['headers'][0]['value']='Fake <other@example.org>'
        with self.assertRaises(worker.PullError):worker.fetch_payouts(self.g,start=date(2026,9,24),end=date(2026,9,25))
    @patch.object(worker,'qbo_build')
    @patch.object(worker,'fetch_payouts')
    def test_no_mail_does_not_call_qbo(self,fetch,qbo):
        fetch.return_value=([],[])
        result=worker.run(self.g,start=date(2026,9,24),end=date(2026,9,25))
        self.assertEqual('needs_human',result['status']);qbo.assert_not_called()
    @patch.object(worker,'qbo_build')
    @patch.object(worker,'fetch_payouts')
    def test_no_bank_or_notes_never_proposes(self,fetch,qbo):
        fetch.return_value=([self.parsed],[{'message_id':'SYN-MSG'}]);qbo.return_value={'ledger':[],'bank_deposits':[],'skipped_jes':[]}
        result=worker.run(self.g,start=date(2026,9,24),end=date(2026,9,25))
        self.assertEqual('unmatched',result['findings'][0]['bucket']);self.assertEqual(0,result['posts_made']);self.assertEqual(0,result['ezlynx_notes_written']);self.assertFalse(result['findings'][0]['proposal_only'])
if __name__=='__main__':unittest.main()
