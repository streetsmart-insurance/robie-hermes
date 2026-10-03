import json
import sqlite3
from pathlib import Path
import pytest
from robie_job_engine.desk_snapshot import snapshot


def database(tmp_path):
 p=tmp_path/'jobs.db';c=sqlite3.connect(p)
 c.executescript('''CREATE TABLE jobs(id TEXT,action_type TEXT,status TEXT,attempt_count INTEGER,verification_count INTEGER,created_at TEXT,updated_at TEXT,next_wakeup_at TEXT,lease_owner TEXT,payload_json TEXT,last_error TEXT);
 CREATE TABLE checkpoints(job_id TEXT,kind TEXT,data_json TEXT);
 CREATE TABLE verification_evidence(id INTEGER,job_id TEXT,verified INTEGER,authoritative INTEGER,expected_json TEXT,observed_json TEXT);''')
 c.execute("INSERT INTO jobs VALUES('j1','playground.task','AWAITING_HUMAN_INPUT',0,0,'a','b',NULL,NULL,?,?)",('{"token":"secret","client":"private name"}','private error'))
 c.execute("INSERT INTO checkpoints VALUES('j1','playground_confirmation',?)",('{"body":"private note"}',));c.commit();c.close();return p


def test_snapshot_does_not_expose_request_or_checkpoint_bodies(tmp_path):
 p=database(tmp_path);result=snapshot(p);text=json.dumps(result)
 for value in ['secret','private name','private error','private note','payload_json','data_json']:
  assert value not in text
 assert result['jobs'][0]['approval_pending'] and result['jobs'][0]['confirmation_present']
 assert result['jobs'][0]['verification']=='no_evidence'
 assert result['read_only'] and not result['approval_available'] and not result['execution_available']


def test_snapshot_leaves_database_bytes_and_schema_unchanged(tmp_path):
 p=database(tmp_path);before=p.read_bytes();snapshot(p);assert p.read_bytes()==before
 c=sqlite3.connect(p);assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==1;c.close()

@pytest.mark.parametrize('verified,authoritative,expected',[(1,1,'verified'),(1,0,'unverified'),(0,1,'unverified')])
def test_evidence_requires_verified_and_authoritative(tmp_path,verified,authoritative,expected):
 p=database(tmp_path);c=sqlite3.connect(p);c.execute('INSERT INTO verification_evidence VALUES(1,?,?,?,?,?)',('j1',verified,authoritative,'private expected','private observed'));c.commit();c.close()
 assert snapshot(p)['jobs'][0]['verification']==expected


def test_newest_evidence_wins_and_complete_is_not_inferred_proof(tmp_path):
 p=database(tmp_path);c=sqlite3.connect(p);c.execute("UPDATE jobs SET status='COMPLETE'")
 c.executemany('INSERT INTO verification_evidence VALUES(?,?,?,?,?,?)',[(1,'j1',1,1,'',''),(2,'j1',0,0,'','')]);c.commit();c.close()
 row=snapshot(p)['jobs'][0];assert row['status']=='COMPLETE' and row['verification']=='unverified' and not row['approval_pending']


def test_limit_reports_truncation_and_total_counts(tmp_path):
 p=database(tmp_path);c=sqlite3.connect(p);c.execute("INSERT INTO jobs SELECT 'j2',action_type,'FAILED',0,0,created_at,'c',NULL,'lease',payload_json,last_error FROM jobs LIMIT 1");c.commit();c.close()
 result=snapshot(p,limit=1);assert result['total_jobs']==2 and result['shown_jobs']==1 and result['truncated']
 assert result['counts_by_status']=={'AWAITING_HUMAN_INPUT':1,'FAILED':1} and result['jobs'][0]['leased']

@pytest.mark.parametrize('limit',[0,201,True,'10'])
def test_bad_limits_rejected(tmp_path,limit):
 with pytest.raises(ValueError):snapshot(database(tmp_path),limit=limit)


def test_missing_database_is_not_created(tmp_path):
 p=tmp_path/'missing.db'
 with pytest.raises(FileNotFoundError):snapshot(p)
 assert not p.exists()


def test_missing_tables_fail_without_migration(tmp_path):
 p=tmp_path/'empty.db';sqlite3.connect(p).close();before=p.read_bytes()
 with pytest.raises(sqlite3.OperationalError):snapshot(p)
 assert p.read_bytes()==before


def test_global_completion_evidence_rollup_is_not_page_or_productivity_count(tmp_path):
 p=database(tmp_path);c=sqlite3.connect(p)
 c.execute("UPDATE jobs SET status='COMPLETE'")
 for jid,status in [('j2','COMPLETE'),('j3','COMPLETE'),('j4','FAILED'),('j5','NEEDS_AUTH')]:
  c.execute("INSERT INTO jobs SELECT ?,action_type,?,0,0,created_at,?,NULL,NULL,payload_json,last_error FROM jobs LIMIT 1",(jid,status,jid))
 c.executemany('INSERT INTO verification_evidence VALUES(?,?,?,?,?,?)',[
  (1,'j1',1,1,'private','private'),(2,'j2',1,1,'private','private'),
  (3,'j2',0,0,'private','private'),(4,'j4',1,1,'private','private')])
 c.commit();c.close();before=p.read_bytes()
 result=snapshot(p,limit=1)
 assert result['completion_evidence']=={'complete_jobs':3,
  'with_latest_authoritative_verified_evidence':1,
  'without_latest_authoritative_verified_evidence':2,'useful_work_count_verified':False}
 assert result['attention_counts_by_status']['FAILED']==1
 assert result['attention_counts_by_status']['NEEDS_AUTH']==1
 assert result['attention_counts_by_status']['NEEDS_CLARIFICATION']==0
 assert result['shown_jobs']==1 and result['truncated'] and p.read_bytes()==before
 assert 'private' not in json.dumps(result)


def test_empty_database_has_zero_completion_and_attention_counts(tmp_path):
 p=database(tmp_path);c=sqlite3.connect(p);c.execute('DELETE FROM jobs');c.commit();c.close()
 result=snapshot(p)
 assert result['completion_evidence']['complete_jobs']==0
 assert result['completion_evidence']['with_latest_authoritative_verified_evidence']==0
 assert all(value==0 for value in result['attention_counts_by_status'].values())
