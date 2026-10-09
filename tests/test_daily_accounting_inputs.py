import json
from pathlib import Path
from robie_job_engine.daily_accounting_inputs import load_applied_batches, load_ezlynx_tasks, load_ascend_export


def test_applied_return_requires_complete_scope_and_line_provenance(tmp_path):
    p = tmp_path / 'applied.json'
    p.write_text(json.dumps({'complete': True, 'as_of': '2026-09-26T11:00:00Z',
      'scope': 'all expanded portal batches, 2026-09-25', 'batches': [{'ref': 'SYN-B1', 'lines': [
        {'type': 'ach_chargeback', 'psp_ref': 'SYN-R1', 'txn_date': '2026-09-25', 'amount': '-20.00', 'policy': 'SYN-P'},
        {'type': 'sale', 'psp_ref': 'SYN-R2', 'txn_date': '2026-09-25', 'amount': '100.00'}]}]}))
    s = load_applied_batches(p)
    assert s.complete and len(s.items) == 1 and s.items[0].source_id == 'SYN-B1:SYN-R1'
    data = json.loads(p.read_text()); data['scope'] = ''; p.write_text(json.dumps(data))
    assert not load_applied_batches(p).complete


def test_ezlynx_requires_task_id_status_due_date_and_scope(tmp_path):
    p = tmp_path / 'tasks.json'
    p.write_text(json.dumps({'complete': True, 'scope': 'all Accounting Team open tasks',
       'as_of': '2026-09-26T11:00:00Z', 'tasks': [
        {'id':'SYN-T1','assigned_team':'Accounting Team','status':'open','due_date':'2026-09-27','title':'Check return'},
        {'id':'SYN-T2','assigned_team':'Other','status':'open','due_date':'2026-09-27'},
        {'id':'SYN-T3','assigned_team':'Accounting Team','status':'closed','due_date':'2026-09-27'}]}))
    s = load_ezlynx_tasks(p)
    assert s.complete and len(s.items) == 1 and s.items[0].due_date == '2026-09-27'
    data = json.loads(p.read_text()); del data['tasks'][0]['due_date']; p.write_text(json.dumps(data))
    assert not load_ezlynx_tasks(p).complete


def test_ascend_export_requires_all_collection_pagination(tmp_path):
    p = tmp_path / 'ascend.json'
    p.write_text(json.dumps({'as_of':'2026-09-26T11:00:00Z','collections':{
     k:{'exhausted':True,'records':[]} for k in ('cancelation_returns','invoices','programs','loans','payouts')}}))
    s = load_ascend_export(p)
    assert s.complete and not s.items
    data=json.loads(p.read_text()); data['collections']['loans']['exhausted']=False
    p.write_text(json.dumps(data)); assert not load_ascend_export(p).complete
