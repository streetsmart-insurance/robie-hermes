from copy import deepcopy
import pytest
from applied_pay.wells_candidate_crosswalk import candidates,CrosswalkError

def case():
 p={'email_transfer_id':'38EAAA6ABCDEXYZ1','account_last4':'3021','settlement_date':'2026-10-01','net_amount':'100.00','lines_tie':True,'source_reference':'synthetic:email1','psp_return_chain':[{'psp_ref':'SYNPSP','kind':'refund','amount':'-10.00'}]}
 b={'bank_trn':'38EBBB6ABCDEZZZ2','bank_payout_descriptor':'38EBBB6ABCDEZZZ','account_last4':'3021','bank_date':'2026-10-02','amount':'100.00','direction':'credit','bank_status':'posted','source_reference':'synthetic:bank1'}
 return p,b

def test_candidate_never_cleared():
 p,b=case();r=candidates([p],[b]);f=r['findings'][0]
 assert f['status']=='candidate_review_only' and not f['qualifies_cleared_funds'] and not f['posting_allowed']
 assert f['psp_return_chain']==p['psp_return_chain'] and f['bank_trn']==b['bank_trn'] and f['email_transfer_id']==p['email_transfer_id']
 assert r['cleared_bank_deposits']==[] and r['bank_actions']==r['qbo_posts']==r['ezlynx_writes']==0
@pytest.mark.parametrize('field,value',[('email_transfer_id',''),('account_last4','3018'),('lines_tie',False),('grouped',True),('source_reference',None),('net_amount','NaN'),('psp_return_chain',{})])
def test_payout_hold(field,value):
 p,b=case();p[field]=value;assert candidates([p],[b])['findings'][0]['status']=='needs_review'
@pytest.mark.parametrize('field,value',[('bank_trn',''),('account_last4','3018'),('amount','101.00'),('amount','100.001'),('direction','debit'),('bank_status','pending'),('bank_date','2026-10-07'),('bank_date','2026-10-01'),('bank_date','bad'),('grouped',True),('return_or_reversal_candidate',True),('source_reference',None)])
def test_bank_hold(field,value):
 p,b=case();b[field]=value;assert candidates([p],[b])['findings'][0]['status']=='needs_review'
def test_bank_collision_even_wrong_account():
 p,b=case();z=deepcopy(b);z['account_last4']='3018'
 assert candidates([p],[b,z])['findings'][0]['status']=='needs_review'
def test_email_collision():
 p,b=case();z=deepcopy(p);z['email_transfer_id']='38EZZZ6ABCDEZZZ3'
 assert all(f['status']=='needs_review' for f in candidates([p,z],[b])['findings'])
def test_duplicate_stable_id():
 p,b=case();z=deepcopy(b);z['bank_trn']='38EBBB6OTHERZZZ2';b['bank_transaction_id']=z['bank_transaction_id']='same'
 assert candidates([p],[b,z])['findings'][0]['status']=='needs_review'
def test_no_bank():
 p,b=case();assert candidates([p],[])['findings'][0]['status']=='needs_review'
@pytest.mark.parametrize('lag',[1,2,3,4,5])
def test_observed_lags(lag):
 p,b=case();b['bank_date']=f'2026-10-{1+lag:02d}';assert candidates([p],[b])['findings'][0]['status']=='candidate_review_only'
def test_prod_refused():
 p,b=case()
 with pytest.raises(CrosswalkError):candidates([p],[b],environment='PRODUCTION')
