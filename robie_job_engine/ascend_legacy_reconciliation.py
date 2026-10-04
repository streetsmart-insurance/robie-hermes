"""Read-only reconciliation plan. Never migrate or update the source ledger."""
import json,sqlite3
from pathlib import Path

def classify(row):
    result=row.get('raw_data_json') or row.get('response_json') or row.get('details') or {}
    if isinstance(result,str):
        try:result=json.loads(result)
        except ValueError:result={}
    status=str(row.get('status','')).upper()
    if status in ('UNMATCHED','SYNCED_UNMATCHED') and not row.get('applicant_id'):
        action='remap_then_destination_lookup'
    elif status=='SUCCESS' and 'staged' in json.dumps(result).lower():
        action='staged_not_delivered_destination_lookup'
    else:action='uncertain_destination_lookup_only'
    return {'source_key':row.get('event_id'),'legacy_status':status,
            'action':action,'may_send':False,'may_update_legacy':False,
            'candidate_import':'not_authorized','verified_delivered':False}

def plan_rows(rows):return [classify(dict(row)) for row in rows]

def read_only_plan(path):
    # Require a supplied DB path, reject non-file URLs and open SQLite mode=ro.
    p=Path(path).resolve(strict=True)
    db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        return plan_rows(db.execute('SELECT * FROM ascend_synced_events'))
    finally:db.close()
