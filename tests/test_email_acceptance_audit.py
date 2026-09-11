import importlib.util
import json
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location('probe', Path(__file__).resolve().parents[1] / 'scripts/diagnose-email-acceptance-readonly.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_probe_reports_markers_without_leaking_content():
    secret = 'secret-value-must-never-print'
    assert probe.markers(secret + ' PLAYWRIGHT_BLOCKED') == ['PLAYWRIGHT_BLOCKED']


def test_connection_is_read_only(tmp_path):
    p = tmp_path / 'test.db'
    with sqlite3.connect(p) as db:
        db.execute('CREATE TABLE test (id TEXT)')
    with probe.connect(p) as db:
        import pytest
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO test VALUES ('write')")


def test_runtime_inspection_does_not_emit_source_secrets(tmp_path):
    root = tmp_path / 'hermes-agent'
    root.mkdir()
    (root / 'cli.py').write_text("api_key='secret-do-not-print'\ndef run():\n    pass\n")
    result = json.dumps(probe.inspect_runtime(tmp_path))
    assert 'secret-do-not-print' not in result
    assert 'run' in result


def test_job_report_excludes_request_and_worker_content(monkeypatch):
    from durable_temp import durable_temporary_directory
    from robie_job_engine.store import JobStore
    with durable_temporary_directory() as directory:
        path = Path(directory) / 'jobs.db'
        store = JobStore(str(path))
        job = store.create_job('hermes.email_task', {'request_text': probe.POLICY + ' private-content', 'gmail_message_id': 'test-message'})
        monkeypatch.setattr(probe, 'JOB_ID', job['id'])
        store.checkpoint(job['id'], 'action', {'response_text': 'private-content PLAYWRIGHT_BLOCKED'})
        result = probe.inspect_job(path)
        assert result['matches_requested_test'] is True
        assert result['checkpoints'][0]['markers'] == ['PLAYWRIGHT_BLOCKED']
        assert 'private-content' not in json.dumps(result)
        assert result['browser_calls'] == []


def test_session_probe_is_scoped_and_never_emits_messages(tmp_path):
    path = tmp_path / 'state.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sessions (id TEXT,source TEXT,model TEXT,started_at TEXT,ended_at TEXT,end_reason TEXT,message_count INTEGER,tool_call_count INTEGER,api_call_count INTEGER)')
        db.execute('CREATE TABLE messages (id INTEGER,session_id TEXT,role TEXT,content TEXT,tool_name TEXT,tool_calls TEXT,finish_reason TEXT,reasoning TEXT,reasoning_content TEXT)')
        for sid in ('target','other'):
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)', (sid,'cli','model','start','end','done',1,0,1))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)', (1,'target','assistant',probe.POLICY + ' secret-message PLAYWRIGHT_BLOCKED',None,'[]','stop','private-thought',None))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)', (2,'other','user','unrelated private message',None,'[]',None,None,None))
    result = probe.inspect_acceptance_sessions(path)
    assert [s['id'] for s in result] == ['target']
    assert result[0]['messages'][0]['markers'] == ['PLAYWRIGHT_BLOCKED']
    rendered = json.dumps(result)
    assert 'secret-message' not in rendered
    assert 'private-thought' not in rendered
    assert 'unrelated private' not in rendered
