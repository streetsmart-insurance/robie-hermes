from copy import deepcopy
from datetime import date, timedelta
import pytest
from applied_pay.movable_earned_fee_report import report, FeeReportError

def case(method='ach', payment_date='2026-10-01'):
    cw={'mode':'test_candidate_review_only','findings':[{'email_transfer_id':'P1','bank_trn':'B1','status':'candidate_review_only','qualifies_cleared_funds':False,'psp_return_chain':[]}]}
    p={'psp_ref':'PSP1','payout_id':'P1','method':method,'payment_date':payment_date,'source_reference':'synthetic:payment','fee_source_reference':'synthetic:earnings','fee_kind':'agency_earned','earned_verified':True,'earned_fee':'100.00','already_swept':'0.00','net_payment':'1000.00','clearance':{'verified':True,'source_reference':'synthetic:clearance','bank_transaction_id':'STABLE1','account_last4':'3021','email_transfer_id':'P1','bank_trn':'B1'}}
    e={k:{'complete':True,'as_of_date':'2026-10-08','source_reference':'synthetic:'+k} for k in ['earnings','returns','clearance','trust_capacity']}
    e['trust_capacity']['earned_fee_capacity']='500.00'
    e['payout_line_nets']={'P1':{'expected_positive_payment_net':'1000.00','source_reference':'synthetic:line-net'}}
    return cw,[p],[],e

def run(x): return report(*x,as_of='2026-10-08')
def liability(x,method='card',amount='40.00',applied='0.00',remaining='40.00',state='open'):
    x[2].append({'return_id':'R1','event_date':'2026-10-08','psp_ref':'PSP1','kind':'chargeback','method':method,'state':state,'source_reference':'synthetic:return','gross_liability':amount,'already_debited':applied,'remaining_liability':remaining,'debit_proof_reference':'synthetic:debit'})

@pytest.mark.parametrize('age,amount',[(6,'0.00'),(7,'100.00'),(8,'100.00')])
def test_ach_boundary(age,amount):
    x=case(payment_date=(date(2026,10,8)-timedelta(days=age)).isoformat());r=run(x)
    assert r['movable_earned_fee']==amount and not r['transfer_allowed']
def test_card_zero_hold(): assert run(case('card','2026-10-08'))['movable_earned_fee']=='100.00'
def test_card_chargeback_net_and_flag():
    x=case('card');liability(x)
    p=deepcopy(x[1][0]);p['psp_ref']='PSP2';x[1].append(p);x[3]['payout_line_nets']['P1']['expected_positive_payment_net']='2000.00'
    r=run(x);assert r['movable_earned_fee']=='60.00';assert r['diagnostics_only']['excluded_return_fees']=='100.00';assert r['return_ledger'][0]['return_id']=='R1'
def test_card_reconciled_not_debited_twice():
    x=case('card');liability(x,amount='40.00',applied='40.00',remaining='0.00',state='reconciled');r=run(x)
    assert r['movable_earned_fee']=='0.00' and r['diagnostics_only']['uncovered_return_liability']=='0.00'
def test_open_ach_blocks_all():
    x=case();liability(x,method='ach');assert run(x)['movable_earned_fee'] is None
@pytest.mark.parametrize('scope',['earnings','returns','clearance','trust_capacity'])
def test_incomplete_or_stale(scope):
    x=case();x[3][scope]['as_of_date']='2026-10-07';assert run(x)['movable_earned_fee'] is None
@pytest.mark.parametrize('field,value',[('method','applepay'),('earned_fee','NaN'),('earned_fee','0.001'),('earned_fee','-1'),('earned_fee','1001'),('already_swept','101'),('fee_kind','convenience'),('earned_verified',False),('fee_source_reference',None),('payment_date','bad'),('payment_date','2026-10-09')])
def test_payment_failures(field,value):
    x=case();x[1][0][field]=value;assert run(x)['movable_earned_fee'] is None
@pytest.mark.parametrize('field,value',[('verified',False),('account_last4','3018'),('bank_trn','wrong'),('bank_transaction_id',None),('source_reference',None)])
def test_no_candidate_upgrade(field,value):
    x=case();x[1][0]['clearance'][field]=value;assert run(x)['movable_earned_fee'] is None
def test_duplicate_payment():
    x=case();x[1].append(deepcopy(x[1][0]));assert run(x)['movable_earned_fee'] is None
def test_duplicate_return():
    x=case('card');liability(x);x[2].append(deepcopy(x[2][0]));assert run(x)['movable_earned_fee'] is None
def test_unknown_return_psp():
    x=case('card');liability(x);x[2][0]['psp_ref']='missing';assert run(x)['movable_earned_fee'] is None
def test_crosswalk_chain_missing():
    x=case();x[0]['findings'][0]['psp_return_chain']=[{'psp_ref':'lost'}];assert run(x)['movable_earned_fee'] is None
def test_line_coverage_missing():
    x=case();x[3]['payout_line_nets']={};assert run(x)['movable_earned_fee'] is None
def test_capacity_caps_and_prior_sweep():
    x=case();x[1][0].update(already_swept='20.00',sweep_source_reference='synthetic:sweep');x[3]['trust_capacity']['earned_fee_capacity']='50.00';assert run(x)['movable_earned_fee']=='50.00'
def test_later_date_used():
    x=case();x[1][0]['alternate_payment_date']='2026-10-02';assert run(x)['movable_earned_fee']=='0.00'
def test_liability_missing_amount():
    x=case('card');liability(x);x[2][0]['remaining_liability']=None;assert run(x)['movable_earned_fee'] is None
def test_liability_arithmetic():
    x=case('card');liability(x,remaining='39.00');assert run(x)['movable_earned_fee'] is None
def test_prod_refused():
    with pytest.raises(FeeReportError): report(*case(),as_of='2026-10-08',environment='PROD')
def test_no_actions():
    r=run(case());assert all(r[k]==0 for k in ['bank_actions','transfers','qbo_posts','ezlynx_writes'])

def test_return_date_missing():
    x=case('card');liability(x);x[2][0].pop('event_date');assert run(x)['movable_earned_fee'] is None

def test_pending_unallocated_return_blocks():
    x=case('card');liability(x);x[2][0]['psp_ref']=None;assert run(x)['movable_earned_fee'] is None
