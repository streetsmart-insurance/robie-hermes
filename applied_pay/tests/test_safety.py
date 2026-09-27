"""Synthetic safety regressions, no real customer or transaction data."""
import copy, json, pathlib, sys, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from match import Matcher

def scenario():
    return {'cutoff':'2026-09-25', 'payouts':[{'ref':'SYN-PAYOUT-A','payout_date':'2026-09-24','net':'100.00','status':'scheduled','lines':[{'type':'sale','amount':'100.00','payer':'Example Bakery','business':'Example Bakery','txn_date':'2026-09-24','psp_ref':'SYN-PSP-A','ezlynx_note_check':{'status':'checked','source':'ezlynx','payment_ref':'SYN-PSP-A','note_ids':['SYN-NOTE-BASE'],'notes':[{'id':'SYN-NOTE-BASE','text':'Premium payment'}]}}]}], 'ledger':[{'je_id':'SYN-JE-A','receipt_no':'SYN-RECEIPT-A','customer':'Example Bakery','kind':'receipt','amount':'100.00','date':'2026-09-24','bank_account':'Undeposited Funds'}], 'bank_deposits':[], 'cleared_bank_deposits':[]}
def checked_note(s, text):
    line=s['payouts'][0]['lines'][0]
    line['psp_ref']='SYN-PSP-A'
    line['ezlynx_note_check']={'status':'checked','source':'ezlynx','payment_ref':'SYN-PSP-A','note_ids':['SYN-NOTE-A'],'notes':[{'id':'SYN-NOTE-A','text':text}]}

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
    def test_existing_posted_without_note_check_needs_source_review(self):
        s=scenario();s['payouts'][0]['lines'][0].pop('ezlynx_note_check')
        s['bank_deposits']=[{'id':'QBO-DEP-SYN-A','date':'2026-09-24','amount':'100.00','groups':['SYN-JE-A']}]
        s['ledger'][0]['deposited_in']='SYN-A'
        f=run(s)
        self.assertEqual('already_posted_needs_review',f.bucket)
        self.assertEqual('QBO-DEP-SYN-A',f.bank['id'])
        self.assertEqual('SYN-JE-A',f.lines[0]['je']['je_id'])
        self.assertIsNone(f.proposed_deposit)
        self.assertIn('source-bound', ' '.join(f.reasons))
    def test_missing_note_without_matching_deposit_remains_unmatched(self):
        s=scenario();s['payouts'][0]['lines'][0].pop('ezlynx_note_check')
        s['bank_deposits']=[{'id':'QBO-DEP-SYN-X','date':'2026-09-24','amount':'100.00','groups':['SYN-UNRELATED']}]
        f=run(s);self.assertEqual('unmatched',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_missing_note_no_deposit_never_proposes_even_with_bank_proof(self):
        s=scenario();s['payouts'][0]['lines'][0].pop('ezlynx_note_check')
        s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'}]
        f=run(s);self.assertEqual('waiting_approval',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_missing_note_does_not_hide_ambiguous_grouped_jes(self):
        s=scenario();s['payouts'][0]['lines'][0].pop('ezlynx_note_check')
        s['payouts'][0]['net']='200.00'
        s['payouts'][0]['lines'].append(copy.deepcopy(s['payouts'][0]['lines'][0]))
        s['ledger'].append(dict(s['ledger'][0],je_id='SYN-JE-B',receipt_no='SYN-RECEIPT-B',deposited_in='SYN-A'))
        s['ledger'][0]['deposited_in']='SYN-A'
        s['bank_deposits']=[{'id':'QBO-DEP-SYN-A','date':'2026-09-24','amount':'200.00','groups':['SYN-JE-A','SYN-JE-B']}]
        f=run(s);self.assertEqual('unmatched',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_missing_note_never_bypasses_fee_components(self):
        s=scenario();s['payouts'][0]['lines'][0].pop('ezlynx_note_check')
        s['payouts'][0]['lines'][0]['fee_components']=[{'type':'agency_fee','amount':'10.00'}]
        s['bank_deposits']=[{'id':'QBO-DEP-SYN-A','date':'2026-09-24','amount':'100.00','groups':['SYN-JE-A']}]
        s['ledger'][0]['deposited_in']='SYN-A'
        f=run(s);self.assertEqual('unmatched',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_cleared_external_bank_without_qbo_deposit_can_propose(self):
        s=scenario();s['payouts'][0]['status']='reconciled';checked_note(s,'Premium payment');s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'}]
        f=run(s);self.assertEqual('ready',f.bucket);self.assertTrue(f.proposed_deposit['ties'])
    def test_scheduled_bank_not_cleared(self):
        s=scenario();s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'scheduled'}]
        self.assertEqual('scheduled_unlanded',run(s).bucket)
    def test_duplicate_bank_candidates_stop(self):
        s=scenario();s['cleared_bank_deposits']=[{'id':f'SYN-BANK-{n}','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'} for n in 'AB']
        self.assertEqual('unmatched',run(s).bucket)
    def test_line_mismatch_stops_even_if_bank_cleared(self):
        s=scenario();s['payouts'][0]['net']='99.00';s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'99.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'}]
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
    def test_same_amount_cleared_but_unrelated_reference_never_ready(self):
        s=scenario();s['cleared_bank_deposits']=[{'id':'SYN-BANK-X','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-OTHER-PAYOUT','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-X'}]
        f=run(s);self.assertEqual('scheduled_unlanded',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_unproven_cleared_flag_never_ready(self):
        s=scenario();s['cleared_bank_deposits']=[{'id':'SYN-BANK-X','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared'}]
        f=run(s);self.assertEqual('scheduled_unlanded',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_grouped_receipt_without_matching_qbo_deposit_stops(self):
        s=scenario();s['ledger'][0]['deposited_in']='OTHER'
        s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'}]
        f=run(s);self.assertEqual('unmatched',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_payment_with_premium_and_fee_never_auto_proposes(self):
        s=scenario(); s['payouts'][0]['net']='115.00'; checked_note(s,'Premium 100 and agency fee 15'); line=s['payouts'][0]['lines'][0]
        line.update(amount='115.00',premium_amount='100.00',fee_components=[{'type':'agency_fee','amount':'15.00'}],description='Premium and agency fee')
        s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'115.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'}]
        f=run(s);self.assertEqual('waiting_approval',f.bucket);self.assertIsNone(f.proposed_deposit)
        self.assertIn('premium 100.00 + fee 15.00',' '.join(f.lines[0]['notes']))
    def test_fee_only_payment_requires_review(self):
        s=scenario();s['payouts'][0]['net']='16.00';checked_note(s,'MBR fee 16');line=s['payouts'][0]['lines'][0]
        line.update(amount='16.00',premium_amount='0.00',fee_components=[{'type':'unclassified_fee','amount':'16.00'}])
        s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'16.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'}]
        f=run(s);self.assertEqual('waiting_approval',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_mislabeled_premium_with_fee_note_is_held(self):
        s=scenario();s['fee_rules']={'note_terms':['agency fee','MBR']}
        checked_note(s,'agency fee, allocation unavailable')
        s['payouts'][0]['lines'][0]['description']='Premium plus agency fee, allocation unavailable'
        s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'}]
        f=run(s);self.assertEqual('waiting_approval',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_fee_split_mismatch_stops(self):
        s=scenario();checked_note(s,'agency fee 15');s['payouts'][0]['lines'][0].update(premium_amount='90.00',fee_components=[{'type':'agency_fee','amount':'15.00'}])
        self.assertEqual('unmatched',run(s).bucket)
    def test_late_bank_deposit_not_auto_matched(self):
        s=scenario();s['cleared_bank_deposits']=[{'id':'SYN-BANK-LATE','date':'2026-09-28','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'}]
        f=run(s);self.assertEqual('scheduled_unlanded',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_missing_settlement_attachment_fails_parse(self):
        from email_parse import parse
        with self.assertRaises(FileNotFoundError):
            parse('Transfer ID: SYN123\nTotal Deposit: $100.00', 'Thu, 24 Sep 2026 08:00:00 -0400', '/tmp/nonexistent-synthetic-applied-pay.xlsx')
    def test_ezlynx_note_check_missing_blocks_new_ready(self):
        s=scenario();s['payouts'][0]['lines'][0].pop('ezlynx_note_check');s['cleared_bank_deposits']=[{'id':'SYN-BANK-A','date':'2026-09-24','amount':'100.00','account':'10002 Trust Checking WF (3021)','status':'cleared','verified_payout_ref':'SYN-PAYOUT-A','verification_source':'bank_record','bank_transaction_id':'SYN-BANK-TXN-A'}]
        f=run(s);self.assertEqual('waiting_approval',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_mbr_ezlynx_note_is_fee_review_not_automatic(self):
        s=scenario();checked_note(s,'MBR fee retained, see billing notes')
        f=run(s);self.assertEqual('unmatched',f.bucket);self.assertIsNone(f.proposed_deposit)
    def test_shirley_payable_note_not_fee(self):
        s=scenario();checked_note(s,'Shirley payable, not agency fee')
        f=run(s);self.assertEqual('unmatched',f.bucket)
        self.assertIn('fee and payable',' '.join(f.lines[0]['notes']))
    def test_payable_note_alone_held_as_payable(self):
        s=scenario();checked_note(s,'Shirley payable')
        f=run(s);self.assertEqual('unmatched',f.bucket)
        self.assertIn('payable, not a fee',' '.join(f.lines[0]['notes']))
    def test_unknown_small_line_not_guessed_fee(self):
        s=scenario();s['payouts'][0]['net']='15.00';s['payouts'][0]['lines'][0]['amount']='15.00';s['ledger'][0]['amount']='15.00';checked_note(s,'Payment received')
        f=run(s);self.assertEqual('unmatched',f.bucket)
        self.assertIn('small payment',' '.join(f.lines[0]['notes']))
    def test_fee_component_without_bound_note_stops(self):
        s=scenario();s['payouts'][0]['net']='115.00';s['payouts'][0]['lines'][0].update(amount='115.00',premium_amount='100.00',fee_components=[{'type':'agency_fee','amount':'15.00'}]);checked_note(s,'Premium payment')
        f=run(s);self.assertEqual('unmatched',f.bucket)
    def test_legacy_trust_stops(self):
        s=scenario();s['ledger'][0]['bank_account']='Trust';self.assertEqual('unmatched',run(s).bucket)
if __name__=='__main__': unittest.main()
