"""Invented identifiers and amounts only. No live services."""
import hashlib
import sys
from decimal import Decimal
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'carrier_statements'))
from review_contract import validate_source, statement_holds, reconcile, cents

@pytest.fixture
def evidence(tmp_path):
    p=tmp_path/'invented.txt';p.write_text('invented statement')
    source=dict(agency_id='TEST-AGENCY',carrier_id='TEST-CARRIER',account_no='FAKE-001',statement_date='2026-08-31',printed_period='2026-08',statement_class='agency_payable',parsed_carrier='TEST-PARSER',currency='USD',path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest(),source_id='fake-source')
    line=dict(policy='FAKE-POLICY',invoice='FAKE-INVOICE',net=Decimal('10.00'),kind='charge',flags=[])
    stmt=dict(carrier='TEST-PARSER',account_no='FAKE-001',statement_date='2026-08-31',total_due=Decimal('10.00'),ties=True,lines=[line])
    record=dict(statement_class='agency_payable',currency='USD',record_id='FAKE-RECORD',agency_id='TEST-AGENCY',carrier_id='TEST-CARRIER',account_no='FAKE-001',policy='FAKE-POLICY',invoice='FAKE-INVOICE',net='10.00')
    return source,stmt,record

def run(e,records=None):
    s,t,r=e
    return reconcile(t,s,[r] if records is None else records,agency_id='TEST-AGENCY',month='2026-08')

def test_exact_candidate_never_pay_ready(evidence):
    result=run(evidence)
    assert result['lines'][0]['status']=='CANDIDATE_REVIEW_ONLY'
    assert not result['financial_reconciled'] and not result['pay_ready'] and result['writes']==0

@pytest.mark.parametrize('field,value',[('agency_id','OTHER'),('carrier_id','OTHER'),('account_no','OTHER'),('policy','OTHER'),('invoice','OTHER')])
def test_each_identity_dimension_required(evidence,field,value):
    evidence[2][field]=value
    assert run(evidence)['lines'][0]['record_id'] is None

def test_duplicate_invoice_held(evidence):
    r=run(evidence,[evidence[2],dict(evidence[2])])
    assert 'invoice_ambiguous' in r['lines'][0]['exceptions']

def test_no_amount_name_only_match(evidence):
    evidence[1]['lines'][0]['invoice']=None
    assert 'missing_policy_or_invoice' in run(evidence)['lines'][0]['exceptions']

def test_sign_matters(evidence):
    evidence[2]['net']='-10.00'
    assert 'signed_amount_mismatch' in run(evidence)['lines'][0]['exceptions']

def test_missing_net_is_not_zero(evidence):
    evidence[1]['lines'][0]['net']=None
    assert 'missing_line_net' in run(evidence)['holds']
    assert 'statement_held_no_match' in run(evidence)['lines'][0]['exceptions']

def test_derived_line_not_proof(evidence):
    evidence[1]['lines'][0]['txn_type']='NOT_ITEMIZED'
    r=run(evidence)
    assert 'unitemized_balance_difference' in r['holds']
    assert r['lines'][0]['record_id'] is None

def test_quarantine_hold(evidence):
    evidence[1]['lines'][0]['flags']=['quarantine credit']
    assert 'quarantined_credit' in run(evidence)['holds']

@pytest.mark.parametrize('field,value,reason',[('printed_period','2025-08','statement_month_mismatch'),('statement_date','2026-02-31','invalid_statement_date'),('sha256','bad','source_digest_mismatch'),('path','name.pdf','source_path_not_absolute'),('agency_id','OTHER','source_agency_mismatch')])
def test_source_validation(evidence,field,value,reason):
    evidence[0][field]=value
    assert reason in run(evidence)['holds']

def test_account_mismatch(evidence):
    evidence[1]['account_no']='OTHER'
    assert 'parsed_account_mismatch' in run(evidence)['holds']

def test_missing_record_id(evidence):
    evidence[2]['record_id']=None
    assert run(evidence)['lines'][0]['record_id'] is None

@pytest.mark.parametrize('value',[None,True,1.1,'NaN','Infinity','0.001'])
def test_bad_money_refused(value):
    with pytest.raises(ValueError):cents(value)

def test_signed_cents():
    assert cents('-10.25')==-1025

def test_no_source_file(evidence):
    Path(evidence[0]['path']).unlink()
    assert 'source_file_unavailable' in run(evidence)['holds']

def test_credit_allocation_not_assumed(evidence):
    evidence[1]['lines'][0]['kind']='credit'
    assert 'allocation_owner_unverified' in run(evidence)['lines'][0]['exceptions']

def test_repeat_stable(evidence):
    assert run(evidence)==run(evidence)


def test_issue_date_is_not_period(evidence):
    evidence[0]['statement_date']='2026-09-05'
    evidence[1]['statement_date']='2026-09-05'
    assert 'statement_month_mismatch' not in run(evidence)['holds']

@pytest.mark.parametrize('kind',['policy_invoice','finance_borrower_balance','unknown'])
def test_wrong_statement_class_held(evidence,kind):
    evidence[0]['statement_class']=kind
    assert 'not_monthly_agency_statement' in run(evidence)['holds']


def test_held_source_cannot_match(evidence):
    evidence[0]['sha256']='bad'
    assert run(evidence)['lines'][0]['record_id'] is None

def test_currency_held(evidence):
    evidence[0]['currency']='CAD'
    assert 'unsupported_or_unverified_currency' in run(evidence)['holds']

def test_duplicate_line_held(evidence):
    evidence[1]['lines'].append(dict(evidence[1]['lines'][0]))
    r=run(evidence)
    assert 'duplicate_statement_line' in r['lines'][1]['exceptions']
    assert r['lines'][1]['record_id'] is None


def test_parsed_carrier_mismatch(evidence):
    evidence[1]['carrier']='OTHER'
    assert 'parsed_carrier_mismatch' in run(evidence)['holds']

@pytest.mark.parametrize('field,value',[('statement_class','commission_receivable'),('currency','CAD')])
def test_record_class_currency_not_mixed(evidence,field,value):
    evidence[2][field]=value
    assert run(evidence)['lines'][0]['record_id'] is None

def test_model_missing_amount_fails_arithmetic():
    from model import statement,line
    s=statement('TEST', 'FAKE', '2026-08-31', Decimal('0'),[line(net=None)],{},'fake')
    assert not s['ties'] and not s['arithmetic_ties']

def test_model_invalid_date():
    from model import mdy
    with pytest.raises(ValueError):mdy('02/31/2026')
