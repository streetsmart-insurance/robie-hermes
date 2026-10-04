"""Read-only id-based AppSheet snapshot intake. No downloads, writes or inferred periods."""
from collections import Counter
import argparse,json
from datetime import datetime


def plan_intake(roster, document_export, *, agency_id, month, validations=None):
    """Join stable IDs; only supplied, source-backed validation can qualify coverage.

    validations maps document id to a source contract (review_contract.validate_source).
    AppSheet dates, names, remarks and status columns do not establish printed period.
    Snapshot ownership/agency scope must be checked by the caller before use.
    """
    from review_contract import validate_source
    if not agency_id or not isinstance(agency_id,str):raise ValueError('agency_id_required')
    datetime.strptime(month+'-01','%Y-%m-%d')
    validations=validations or {}
    carriers=roster['carriers'];documents=document_export['documents']
    counts=Counter(c.get('id') for c in carriers)
    doccounts=Counter(d.get('id') for d in documents)
    joined={};exceptions=[]
    for d in documents:
        did=d.get('id');cid=d.get('Insurance Carrier Name')
        if not did or doccounts[did]!=1:
            exceptions.append({'document_id':did,'reason':'missing_or_duplicate_document_id'});continue
        if not cid or counts[cid]!=1:
            exceptions.append({'document_id':did,'reason':'missing_or_ambiguous_carrier_fk'});continue
        joined.setdefault(cid,[]).append(d)
    rows=[]
    for c in carriers:
        cid=c.get('id');holds=[]
        if not cid or counts[cid]!=1:holds.append('missing_or_duplicate_carrier_id')
        docs=[]
        for d in joined.get(cid,[]):
            item={'document_id':d['id'],'document_type':d.get('Document Type'),
                  'stored_file':d.get('File'),'status':'PERIOD_CLASS_SOURCE_UNVERIFIED','holds':[]}
            v=validations.get(d['id'])
            if v is not None:
                item['holds']=validate_source(v,agency_id=agency_id,month=month)
                if v.get('carrier_id')!=cid:item['holds'].append('source_carrier_fk_mismatch')
                if v.get('source_id')!=d['id']:item['holds'].append('source_document_id_mismatch')
                if d.get('Document Type')!='Statement':item['holds'].append('not_statement_record')
                item['status']='SOURCE_VALIDATED_REVIEW_ONLY' if not item['holds'] else 'HELD'
                item['statement_class']=v.get('statement_class')
            docs.append(item)
        billing=c.get('Billing Type')
        # Raw roster category is a review priority, not a ruling about statement obligation.
        priority=billing=='Agency Billed' or billing=='Finance Company'
        validated=any(d['status']=='SOURCE_VALIDATED_REVIEW_ONLY' for d in docs)
        rows.append({'carrier_id':cid,'carrier_name':c.get('Insurance Carrier Name'),
                     'billing_type':billing,'monthly_obligation':'UNVERIFIED',
                     'raw_monthly_priority':priority,'documents':docs,'holds':holds,
                     'coverage':'SOURCE_AVAILABLE_REVIEW_ONLY' if validated and not holds else 'UNVERIFIED',
                     'acquisition_required':not validated,'financial_reconciled':False})
    unknown=[k for k in validations if k not in doccounts]
    for did in unknown:exceptions.append({'document_id':did,'reason':'validation_document_not_in_snapshot'})
    return {'agency_id':agency_id,'month':month,'source_spreadsheet_id':roster.get('source_spreadsheet_id'),
            'snapshot_read_at':roster.get('read_at'),'snapshot_only':True,'live_intake':False,
            'writes':0,'carrier_count':len(rows),'document_count':len(documents),
            'statement_type_count':sum(d.get('Document Type')=='Statement' for d in documents),
            'billing_counts':dict(Counter(c.get('Billing Type') for c in carriers)),
            'carriers':rows,'exceptions':exceptions}


def main():
    ap=argparse.ArgumentParser()
    for arg in ['roster','documents','agency-id','month']:ap.add_argument('--'+arg,required=True)
    ap.add_argument('--validations')
    a=ap.parse_args()
    result=plan_intake(json.load(open(a.roster)),json.load(open(a.documents)),agency_id=a.agency_id,month=a.month,validations=json.load(open(a.validations)) if a.validations else None)
    print(json.dumps(result,indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
