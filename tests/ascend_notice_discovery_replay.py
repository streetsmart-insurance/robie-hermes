import importlib.util,json,tempfile,os
from pathlib import Path
spec=importlib.util.spec_from_file_location('discovery_tests','tests/test_ascend_notice_discovery.py');t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)
from robie_job_engine.ascend_notice_discovery import ReviewQueue,scan
msgs={str(i):t.msg(str(i),sender='staff@example.com' if i%2 else 'notice@useascend.com',body='From: Ascend <notice@useascend.com>\nPayment failed',subject='Fwd: Payment failed for synthetic Example',unread=i%3==0) for i in range(53)}
g=t.Gmail({'first':{'messages':[{'id':str(i)} for i in range(25)],'nextPageToken':'page2'},'page2':{'messages':[{'id':str(i)} for i in range(25,53)]}},msgs)
with tempfile.TemporaryDirectory() as td:
 p=Path(td)/'private-review.db';q=ReviewQueue(p);a=scan(g,'hello@example.com',q,start_date='2026-09-01',end_date='2026-10-04');b=scan(g,'hello@example.com',q,start_date='2026-09-01',end_date='2026-10-04');q.close();q=ReviewQueue(p)
 print(json.dumps({'synthetic_only':True,'first_scan':a,'repeat_scan':b,'persisted_review_rows':len(q.rows()),'read_messages_included':sum(x['item']['read_state']=='read' for x in q.rows()),'queue_permissions':oct(os.stat(p).st_mode&0o777),'gmail_calls':sorted(set(c[0] for c in g.calls))},indent=2));q.close()
