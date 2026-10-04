"""Synthetic fixtures only. Never accesses bank or QBO."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import unittest
from applied_pay.wells_bank_evidence import compare, EvidenceError

NOW=datetime(2026,10,1,15,0,tzinfo=timezone.utc)
ARTIFACT=b'synthetic independently reviewed bank detail'
REF='SYNTRANSFER12345'


def fixture():
    payout={'ref':REF,'net':'100.00','payout_date':'2026-10-01','lines_tie':True}
    row={'bank_transaction_id':'SYN-BANK-1','account_last4':'3021','posted_date':'2026-10-01',
         'amount':'100.00','direction':'credit','status':'posted_cleared',
         'descriptor':'Applied Systems PAYOUT '+REF,'transfer_ref':REF}
    manifest={'source_url':'https://www.wellsfargo.com/synthetic-detail','source_kind':'wells_guest_transaction_detail',
              'access_mode':'view_only','account_last4':'3021','clearing_semantics_verified':True,
              'review_reference':'SYN-REVIEW','capture_id':'SYN-CAPTURE','captured_at':NOW.isoformat(),
              'artifact_sha256':hashlib.sha256(ARTIFACT).hexdigest()}
    return [payout],[row],manifest


class BankEvidenceSafety(unittest.TestCase):
    def run_case(self,p,r,m):return compare(p,r,m,artifact_bytes=ARTIFACT,now=NOW)
    def test_exact_verified_credit(self):
        x=self.run_case(*fixture());self.assertEqual(1,len(x['cleared_bank_deposits']))
        self.assertEqual(REF,x['cleared_bank_deposits'][0]['verified_payout_ref']);self.assertEqual(0,x['bank_actions'])
    def test_next_day_allowed(self):
        p,r,m=fixture();r[0]['posted_date']='2026-10-02';self.assertEqual(1,len(self.run_case(p,r,m)['cleared_bank_deposits']))
    def test_amount_only_no_binding(self):
        p,r,m=fixture();r[0]['transfer_ref']='';self.assertEqual([],self.run_case(p,r,m)['cleared_bank_deposits'])
    def test_substring_reference_not_binding(self):
        p,r,m=fixture();r[0]['descriptor']='Applied PAYOUT '+REF+'OTHER';self.assertEqual([],self.run_case(p,r,m)['cleared_bank_deposits'])
    def test_pending_posted_only_and_reversed_stop(self):
        for status in ['pending','scheduled','posted','reversed']:
            p,r,m=fixture();r[0]['status']=status;self.assertEqual([],self.run_case(p,r,m)['cleared_bank_deposits'])
    def test_wrong_account_row_stops(self):
        p,r,m=fixture();r[0]['account_last4']='3018';self.assertEqual([],self.run_case(p,r,m)['cleared_bank_deposits'])
    def test_wrong_account_capture_stops(self):
        p,r,m=fixture();m['account_last4']='3018'
        with self.assertRaises(EvidenceError):self.run_case(p,r,m)
    def test_duplicate_bank_id_stops(self):
        p,r,m=fixture();r.append(copy.deepcopy(r[0]))
        with self.assertRaises(EvidenceError):self.run_case(p,r,m)
    def test_same_reference_two_transactions_stops(self):
        p,r,m=fixture();r.append(dict(r[0],bank_transaction_id='SYN-REVERSE',direction='debit',status='reversed'))
        self.assertEqual([],self.run_case(p,r,m)['cleared_bank_deposits'])
    def test_duplicate_payout_ref_stops(self):
        p,r,m=fixture();p.append(copy.deepcopy(p[0]))
        with self.assertRaises(EvidenceError):self.run_case(p,r,m)
    def test_no_bank_id_stops(self):
        p,r,m=fixture();r[0]['bank_transaction_id']=''
        with self.assertRaises(EvidenceError):self.run_case(p,r,m)
    def test_debit_stops(self):
        p,r,m=fixture();r[0]['direction']='debit';self.assertEqual([],self.run_case(p,r,m)['cleared_bank_deposits'])
    def test_amount_and_date_conflict_stop(self):
        for key,value in [('amount','99.99'),('posted_date','2026-09-30'),('posted_date','2026-10-03')]:
            p,r,m=fixture();r[0][key]=value;self.assertEqual([],self.run_case(p,r,m)['cleared_bank_deposits'])
    def test_stale_future_or_naive_capture_stops(self):
        for value in [(NOW-timedelta(hours=25)).isoformat(),(NOW+timedelta(minutes=1)).isoformat(),NOW.replace(tzinfo=None).isoformat()]:
            p,r,m=fixture();m['captured_at']=value
            with self.assertRaises(EvidenceError):self.run_case(p,r,m)
    def test_tampered_artifact_stops(self):
        p,r,m=fixture();m['artifact_sha256']='0'*64
        with self.assertRaises(EvidenceError):self.run_case(p,r,m)
    def test_wrong_host_or_qbo_source_stops(self):
        for url in ['https://qbo.intuit.com/app/banking','https://www.wellsfargo.com.attacker.invalid/','http://www.wellsfargo.com/']:
            p,r,m=fixture();m['source_url']=url
            with self.assertRaises(EvidenceError):self.run_case(p,r,m)
    def test_missing_independent_review_or_semantics_stops(self):
        for key,value in [('review_reference',''),('clearing_semantics_verified',False),('access_mode','owner')]:
            p,r,m=fixture();m[key]=value
            with self.assertRaises(EvidenceError):self.run_case(p,r,m)
    def test_non_tying_settlement_stops(self):
        p,r,m=fixture();p[0]['lines_tie']=False;self.assertEqual([],self.run_case(p,r,m)['cleared_bank_deposits'])
    def test_nonfinite_or_fractional_cent_stops(self):
        for value in ['NaN','Infinity','1.001']:
            p,r,m=fixture();r[0]['amount']=value
            with self.assertRaises(EvidenceError):self.run_case(p,r,m)
    def test_unrelated_record_visible_not_used(self):
        p,r,m=fixture();r[0]['transfer_ref']='OTHERTRANSFER123';x=self.run_case(p,r,m)
        self.assertEqual(['SYN-BANK-1'],x['unbound_bank_transaction_ids']);self.assertEqual([],x['cleared_bank_deposits'])

if __name__=='__main__':unittest.main()
