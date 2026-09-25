"""Synthetic safety regressions, no real customer or transaction data."""
import copy, json, pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from match import Matcher

def scenario():
    return {'cutoff':'2026-09-25', 'payouts':[{'ref':'SYN-PAYOUT-A','payout_date':'2026-09-24','net':'100.00','status':'scheduled','lines':[{'type':'sale','amount':'100.00','payer':'Example Bakery','business':'Example Bakery','txn_date':'2026-09-24'}]}], 'ledger':[{'je_id':'SYN-JE-A','receipt_no':'SYN-RECEIPT-A','customer':'Example Bakery','kind':'receipt','amount':'100.00','date':'2026-09-24','bank_account':'Undeposited Funds'}], 'bank_deposits':[], 'cleared_bank_deposits':[]}
def run(s): return Matcher(s).run()[0]
class Safety(unittest.TestCase):
    def test_scheduled_is_not_bank_cleared(self):
        f=run(scenario()); self.assertEqual('scheduled_unlanded',f.bucket); self.assertIsNone(f.proposed_deposit)
    def test_wrong_same_amount_posted_deposit_stops(self):
        s=scenario();s['bank_deposits']=[{'id':'QBO-DEP-SYN-B','date':'2026-09-24','amount':'100.00','groups':['OTHER-JE']}]
        f=run(s);self.assertEqual('unmatched',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_existing_posted_exact_groups_not_new_proposal(self):
        s=scenario();s['bank_deposits']=[{'id':'QBO-DEP-SYN-A','date':'2026-09-24','amount':'100.00','groups':['SYN-JE-A']}];s['ledger'][0]['deposited_in']='SYN-A'
        f=run(s);self.assertEqual('already_posted',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_cleared_external_bank_without_qbo_deposit_can_propose(self):
        s=scenario();s['payouts'][0]['status']='reconciled';s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared'}]
        f=run(s);self.assertEqual('ready',f.bucket);self.assertTrue(f.proposed_deposit['ties'])
    def test_scheduled_bank_not_cleared(self):
        s=scenario();s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'scheduled'}]
        self.assertEqual('scheduled_unlanded',run(s).bucket)
    def test_duplicate_bank_candidates_stop(self):
        s=scenario();s['cleared_bank_deposits']=[{'id':f'SYN-BANK-{n}','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared'} for n in 'AB']
        self.assertEqual('unmatched',run(s).bucket)
    def test_line_mismatch_stops_even_if_bank_cleared(self):
        s=scenario();s['payouts'][0]['net']='99.00';s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'99.00','account':'10002 Trust Checking WF (3021)','status':'cleared'}]
        self.assertEqual('unmatched',run(s).bucket)
    def test_no_status_and_no_cleared_source_is_unmatched(self):
        s=scenario();s['payouts'][0].pop('status');self.assertEqual('unmatched',run(s).bucket)
    def test_duplicate_transfer_ids_stop_before_matching(self):
        s=scenario();s['payouts'].append(copy.deepcopy(s['payouts'][0]))
        self.assertTrue(all(f.bucket=='unmatched' for f in Matcher(s).run()))
    def test_scrubbed_fixture_never_claims_bank_cleared(self):
        s=json.loads((pathlib.Path(__file__).resolve().parents[1]/'fixtures'/'snapshot_sept.json').read_text())
        self.assertFalse(s['bank_deposits'])
        self.assertTrue(all(b['status']=='scheduled' for b in s['cleared_bank_deposits']))
        fs=Matcher(s).run(); self.assertTrue(fs)
        self.assertFalse(any(f.bucket in ('ready','already_posted') for f in fs))
    def test_grouped_receipt_without_matching_qbo_deposit_stops(self):
        s=scenario();s['ledger'][0]['deposited_in']='OTHER'
        s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared'}]
        f=run(s);self.assertEqual('unmatched',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_payment_with_premium_and_fee_never_auto_proposes(self):
        s=scenario(); s['payouts'][0]['net']='115.00'; line=s['payouts'][0]['lines'][0]
        line.update(amount='115.00',premium_amount='100.00',fee_components=[{'type':'agency_fee','amount':'15.00'}],description='Premium and agency fee')
        s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'115.00','account':'10002 Trust Checking WF (3021)','status':'cleared'}]
        f=run(s);self.assertEqual('waiting_approval',f.bucket);self.assertIsNone(f.proposed_deposit)
        self.assertIn('premium 100.00 + fee 15.00',' '.join(f.lines[0]['notes']))
    def test_fee_only_payment_requires_review(self):
        s=scenario();s['payouts'][0]['net']='16.00';line=s['payouts'][0]['lines'][0]
        line.update(amount='16.00',premium_amount='0.00',fee_components=[{'type':'unclassified_fee','amount':'16.00'}])
        s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'16.00','account':'10002 Trust Checking WF (3021)','status':'cleared'}]
        f=run(s);self.assertEqual('waiting_approval',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_mislabeled_premium_with_fee_note_is_held(self):
        s=scenario();s['fee_rules']={'note_terms':['agency fee','MVR']}
        s['payouts'][0]['lines'][0]['description']='Premium plus agency fee, allocation unavailable'
        s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared'}]
        f=run(s);self.assertEqual('waiting_approval',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_fee_split_mismatch_stops(self):
        s=scenario();s['payouts'][0]['lines'][0].update(premium_amount='90.00',fee_components=[{'type':'agency_fee','amount':'15.00'}])
        self.assertEqual('unmatched',run(s).bucket)
    def test_legacy_trust_stops(self):
        s=scenario();s['ledger'][0]['bank_account']='Trust';self.assertEqual('unmatched',run(s).bucket)
if __name__=='__main__': unittest.main()
