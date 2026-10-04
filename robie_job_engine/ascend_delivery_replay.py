"""Synthetic shape replay. No copied customer data, secrets or network."""
import json,collections
from .ascend_delivery_state import Delivery,ReliableDelivery

def replay():
 events=[]
 for i in range(25):events.append({'key':f'cancel-{i}','kind':'cancellation','legacy_status':'UNMATCHED' if i<6 else 'SYNCED_UNMATCHED'})
 for i in range(3):events.append({'key':f'signed-{i}','kind':'agreement_signed','legacy_status':'UNMATCHED'})
 for i in range(7):events.append({'key':f'issue-{i}','kind':'accounting_issue','legacy_status':'FLAGGED','legacy_uncertain':True})
 events += [{'key':'comm-paid','kind':'commission_payout','legacy_status':'SUCCESS','legacy_qbo':'staged'}, {'key':'supp-paid','kind':'supplier_payout','legacy_status':'SUCCESS','legacy_qbo':'staged'}]
 for typ,status,count in [('commission','unpaid',7),('take_rate','unpaid',2),('full_premium','unpaid',1),('supplier','canceled',3),('commission','canceled',3)]:
  for i in range(count):events.append({'key':f'{typ}-{status}-{i}','kind':'unsupported','source_type':typ,'source_status':status})
 ledger={};r=ReliableDelivery(ledger);counts=collections.Counter(r.process(e) for e in events)
 assert len(events)==53
 assert counts=={'unmatched_retryable':28,'staged_no_live_destination':8,'supplier_accounting_disabled':1,'unsupported_type_status':16},counts
 assert not any(s.delivered for s in ledger.values())
 # On a later read, mappings can improve without touching historical ledger.
 improved=[dict(e,applicant_id='synthetic-app') if e['kind'] in ('cancellation','agreement_signed') else e for e in events]
 later=collections.Counter(r.process(e) for e in improved)
 assert later['staged_no_live_destination']==36
 return {'source_shapes':53,'first_pass':dict(counts),'mapping_retry_pass':dict(later),'destination_writes':0,'delivered':0,'legacy_ledger_edits':0,'network_calls':0,'data':'synthetic counts and shapes only'}
if __name__=='__main__':print(json.dumps(replay(),indent=2))
