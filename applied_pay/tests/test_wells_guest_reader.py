import hashlib,json
from datetime import datetime,timezone
import pytest
from applied_pay.wells_guest_reader import read_capture,CaptureError
NOW=datetime(2026,10,3,22,0,tzinfo=timezone.utc)
def case(account='3021',more=False):
 p={'page_number':1,'account_last4':account,'has_next':more,'rows':[['Pending Transactions'],['row1','10/05/26','Ascend','433.00',''],['Posted Transactions'],['row2','10/02/26','Applied TRN*1*BANKREF123**BANKREF123','$1,000.00','']]}
 original={'pages':[p],'account_last4':account};b=json.dumps(original).encode()
 c={**original,'source_url':'https://connect.secure.wellsfargo.com/accounts/','access_mode':'view_only','identity_review_reference':'synthetic-review','captured_at':NOW.isoformat(),'artifact_sha256':hashlib.sha256(b).hexdigest()}
 return c,b
def test_posted_not_cleared():
 c,b=case();r=read_capture(c,artifact_bytes=b,now=NOW)
 assert r['complete'] and r['cleared_bank_deposits']==[]
 assert [x['bank_status'] for x in r['rows']]==['pending','posted']
 assert all(x['bank_transaction_id'] is None for x in r['rows'])
 assert r['rows'][1]['bank_reference_candidates']==['BANKREF123']
def test_operating_separate():
 c,b=case('3018');r=read_capture(c,artifact_bytes=b,now=NOW)
 assert len(r['operating_observations'])==2 and not r['cleared_bank_deposits']
def test_partial():
 c,b=case(more=True);assert not read_capture(c,artifact_bytes=b,now=NOW)['complete']
@pytest.mark.parametrize('key,value',[('account_last4','9999'),('access_mode','admin'),('source_url','https://evil.invalid'),('captured_at','2026-09-01T00:00:00+00:00'),('artifact_sha256','bad')])
def test_reject_metadata(key,value):
 c,b=case();c[key]=value
 with pytest.raises(CaptureError):read_capture(c,artifact_bytes=b,now=NOW)
def test_source_binding():
 c,b=case();c['pages'][0]['rows'][1][2]='altered'
 with pytest.raises(CaptureError):read_capture(c,artifact_bytes=b,now=NOW)
def test_prod_refused():
 c,b=case()
 with pytest.raises(CaptureError):read_capture(c,artifact_bytes=b,now=NOW,environment='PRODUCTION')
def test_duplicate_kept():
 c,b=case();c['pages'][0]['rows'].append(c['pages'][0]['rows'][-1]);b=json.dumps({'pages':c['pages'],'account_last4':'3021'}).encode();c['artifact_sha256']=hashlib.sha256(b).hexdigest()
 r=read_capture(c,artifact_bytes=b,now=NOW);assert len(r['rows'])==3 and 'duplicate-looking' in r['rows'][-1]['review_reasons'][-1]
