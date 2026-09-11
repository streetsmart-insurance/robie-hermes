import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from durable_temp import durable_temporary_directory
from robie_job_engine.email_agent_runner import run_scripted_email, RECOVERY_PROMPT
from robie_job_engine.store import JobStore


@pytest.fixture
def runtime():
    with durable_temporary_directory() as directory:
        root = Path(directory)
        home = root / 'hermes'
        home.mkdir()
        with sqlite3.connect(home / 'state.db') as db:
            db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,finish_reason TEXT,active INTEGER)')
        store = JobStore(str(root / 'jobs.db'))
        job = store.create_job('hermes.email_task', {'request_text': 'test task'})
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", (job['id'],))
        yield root, home, store, job['id']


def fake_runner(runtime, finishes, responses=None, sessions=None):
    root, home, store, job_id = runtime
    calls = []
    def run(command, **kwargs):
        i = len(calls)
        calls.append((command, kwargs))
        sid = sessions[i] if sessions else 'session-1'
        with sqlite3.connect(home / 'state.db') as db:
            db.execute('INSERT INTO messages (session_id,role,finish_reason,active) VALUES (?,?,?,?)', (sid,'assistant',finishes[i],1))
        usage = Path(command[command.index('--usage-file')+1])
        usage.write_text(json.dumps({'session_id':sid,'completed':True,'failed':False}))
        return SimpleNamespace(returncode=0,stdout=(responses or ['Final saved result']*2)[i])
    return run, calls


def execute(runtime, runner):
    root, home, store, job_id = runtime
    return run_scripted_email('original task', env={'ROBIE_JOB_ID':job_id},home=home,cwd=root,job_id=job_id,db_path=store.path,runner=runner)


def test_scripted_mode_returns_only_final_response(runtime):
    run, calls = fake_runner(runtime,['stop'],['Saved result'])
    assert execute(runtime,run) == 'Saved result'
    command, kwargs = calls[0]
    assert '-z' in command and 'chat' not in command
    assert kwargs['env']['ROBIE_JOB_ID'] == runtime[3]
    assert runtime[2].get_checkpoint(runtime[3], 'email_agent_runtime')['attempts'][0]['finish_reason'] == 'stop'


def test_malformed_call_repairs_same_session_once(runtime):
    run,calls = fake_runner(runtime,['malformed_function_call','stop'],['bad progress','Saved result'])
    assert execute(runtime,run) == 'Saved result'
    assert len(calls) == 2
    assert '--resume' not in calls[0][0]
    assert calls[1][0][-2:] == ['--resume','session-1']
    assert RECOVERY_PROMPT in calls[1][0]
    assert 'original task' not in calls[1][0]
    assert len(runtime[2].get_checkpoint(runtime[3], 'email_agent_runtime')['attempts']) == 2


def test_second_malformed_call_reports_explicit_blocker(runtime):
    run,calls = fake_runner(runtime,['malformed_function_call']*2)
    assert execute(runtime,run).startswith('ROBIE_EXECUTION_BLOCKED:')
    assert len(calls) == 2


def test_changed_recovery_session_is_unknown_not_success(runtime):
    run,calls = fake_runner(runtime,['malformed_function_call','stop'],sessions=['first','different'])
    assert execute(runtime,run).startswith('ROBIE_OUTCOME_UNKNOWN:')
    assert len(calls) == 2


def test_missing_usage_does_not_return_progress(runtime):
    assert execute(runtime,lambda *a,**kw: SimpleNamespace(returncode=0,stdout='I am working')).startswith('ROBIE_OUTCOME_UNKNOWN:')


@pytest.mark.parametrize('finish',['tool_calls','length','max_tokens',''])
def test_nonfinal_turns_are_not_accepted(runtime,finish):
    run,calls = fake_runner(runtime,[finish])
    assert execute(runtime,run).startswith('ROBIE_OUTCOME_UNKNOWN:')
    assert len(calls) == 1


def test_email_target_comes_from_request_even_when_worker_only_narrates(runtime):
    from robie_job_engine.email_guard import run_guarded_email_task
    from robie_job_engine.models import VerificationResult, VerificationEvidence
    root, home, store, job_id = runtime
    seen = []
    class Check:
        def verify(self,job,action):
            seen.append(job['payload'])
            return VerificationResult(False, VerificationEvidence('read','test',{}, {}, True, '2026-09-11T20:00:00Z'), retryable=False, error='Policy not found')
    run_guarded_email_task(db_path=store.path,gmail_message_id='new-email',
        prompt='Create policy number TEST-HO-20260911-E01 on applicant 220250093',
        run_agent=lambda prompt:'I am working',verifiers={'hermes.email_task':Check()})
    assert seen[0]['policy_number'] == 'TEST-HO-20260911-E01'
    assert seen[0]['applicant_id'] == '220250093'


def test_cancelled_job_does_not_start_recovery(runtime):
    run, calls = fake_runner(runtime, ['malformed_function_call'])
    def cancel(command, **kwargs):
        result = run(command, **kwargs)
        with sqlite3.connect(runtime[2].path) as db:
            db.execute("UPDATE jobs SET status='PAUSED' WHERE id=?", (runtime[3],))
        return result
    assert execute(runtime, cancel).startswith('ROBIE_EXECUTION_BLOCKED:')
    assert len(calls) == 1


def test_timeout_never_replays_task(runtime):
    import subprocess
    calls = []
    def timeout(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 600)
    assert execute(runtime, timeout).startswith('ROBIE_OUTCOME_UNKNOWN:')
    assert len(calls) == 1
