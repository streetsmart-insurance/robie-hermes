import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'carrier_statements'))
from intake_snapshot import plan_intake

@pytest.fixture
def snapshots():
    return {'source_spreadsheet_id':'fake-sheet','read_at':'2026-10-04T00:00:00Z','carriers':[{'id':'fake-carrier','Insurance Carrier Name':'Invented Carrier','Billing Type':'Agency Billed','September':'Reconciled'}]}, {'documents':[{'id':'fake-doc','Insurance Carrier Name':'fake-carrier','Document Type':'Statement','Date':'9/30/2026','File':'relative/file.pdf','Remarks':'August statement reconciled'}]}

def run(s):return plan_intake(*s,agency_id='FAKE-AGENCY',month='2026-09')

def test_join_by_id(snapshots):
    r=run(snapshots)
    assert r['carriers'][0]['documents'][0]['document_id']=='fake-doc'
    assert r['carriers'][0]['coverage']=='UNVERIFIED'
    assert not r['live_intake'] and r['writes']==0

def test_date_status_remarks_never_qualify(snapshots):
    r=run(snapshots)['carriers'][0]
    assert r['monthly_obligation']=='UNVERIFIED' and r['coverage']=='UNVERIFIED'
    assert not r['financial_reconciled']

def test_orphan_fk(snapshots):
    snapshots[1]['documents'][0]['Insurance Carrier Name']='Invented Carrier'
    assert run(snapshots)['exceptions'][0]['reason']=='missing_or_ambiguous_carrier_fk'

def test_duplicate_carrier(snapshots):
    snapshots[0]['carriers'].append(dict(snapshots[0]['carriers'][0]))
    r=run(snapshots)
    assert r['exceptions'] and all(x['holds'] for x in r['carriers'])

def test_duplicate_doc(snapshots):
    snapshots[1]['documents'].append(dict(snapshots[1]['documents'][0]))
    assert len(run(snapshots)['exceptions'])==2

def test_direct_bill_not_monthly_payable(snapshots):
    snapshots[0]['carriers'][0]['Billing Type']='Direct Billed'
    assert not run(snapshots)['carriers'][0]['raw_monthly_priority']

def test_unreferenced_validation(snapshots):
    r=plan_intake(*snapshots,agency_id='FAKE',month='2026-09',validations={'unknown':{}})
    assert r['exceptions'][0]['reason']=='validation_document_not_in_snapshot'

def test_missing_agency(snapshots):
    with pytest.raises(ValueError):plan_intake(*snapshots,agency_id='',month='2026-09')

def test_missing_period(snapshots):
    with pytest.raises(ValueError):plan_intake(*snapshots,agency_id='FAKE',month='Sept')
