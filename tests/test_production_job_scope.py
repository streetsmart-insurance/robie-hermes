"""Exercise the Production scope against a real ledger, with no browser writes."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from robie_job_engine import ezlynx_write_scope as scope
from robie_job_engine.store import JobStore
from durable_temp import durable_temporary_directory
from robie_job_engine.action_gate import hold_reason_for_job
from robie_job_engine.playwright_write_guard import attested_test_form_entry_block_reason

CLIENT = '440000001'
OTHER = '440000002'
REQUEST = f'Upload the attachment to https://app.ezlynx.com/web/account/{CLIENT}/documents'


@pytest.mark.parametrize('payload,expected', [
    ({'text': REQUEST}, CLIENT),
    ({'request_text': f'Please update applicant:{CLIENT}'}, CLIENT),
    ({'request_text': f'Please update applicant ID #{CLIENT}'}, CLIENT),
    ({'request_text': REQUEST, 'applicant_id': CLIENT}, CLIENT),
    ({'request_text': REQUEST, 'applicant_id': OTHER}, None),
    ({'request_text': REQUEST, 'account_id': OTHER}, None),
    ({'text': REQUEST + f' and applicant {OTHER}'}, None),
    ({'request_text': 'Please help', 'text': REQUEST, 'applicant_id': CLIENT}, None),
    ({'prompt': REQUEST, 'applicant_id': CLIENT}, None),
    ({'text': f'https://app.ezlynx.com.evil.invalid/web/account/{CLIENT}/overview'}, None),
    ({'text': f'https://evil.invalid/web/account/{CLIENT}/overview'}, None),
    ({'text': f'https://app.ezlynx.com/web/account/{CLIENT}/overview and applicant {CLIENT}'}, CLIENT),
    ({'request_text': 'Create policy number TEST-HO-20260911-E01 on applicant 220250093'}, '220250093'),
    ({'request_text': 'Create policy number TEST-HO-20260911-E01'}, None),
])
def test_original_message_resolves_one_client(payload, expected):
    assert scope.requested_message_applicant(payload) == expected


@pytest.fixture
def running_ledger(tmp_path, monkeypatch):
    directory = durable_temporary_directory()
    path = __import__('pathlib').Path(directory.name) / 'jobs.db'
    store = JobStore(str(path))
    job = store.create_job('hermes.google_chat_task', {'text': REQUEST})
    with sqlite3.connect(path) as db:
        db.execute('UPDATE jobs SET status=? WHERE id=?', ('RUNNING', job['id']))
    monkeypatch.setattr(scope, 'PRODUCTION_JOB_DB', path)
    monkeypatch.setattr(scope, '_is_installed_production_runtime', lambda: True)
    # Job-scope tests pin a restricted compiled list so production_job_applicant()
    # is what grants CLIENT — not the agency-wide unset default.
    monkeypatch.setattr(scope, 'ALLOWED_EZLYNX_WRITE_APPLICANT_IDS', frozenset({'220250093'}))
    monkeypatch.setenv('ROBIE_JOB_DB', str(path))
    monkeypatch.setenv('ROBIE_CURRENT_JOB_ID', job['id'])
    monkeypatch.delenv('ROBIE_JOB_ID', raising=False)
    monkeypatch.delenv('JOB_ID', raising=False)
    yield path
    directory.cleanup()


def test_running_original_client_allowed_but_other_page_refused(running_ledger):
    assert scope.production_job_applicant() == CLIENT
    assert scope.applicant_is_write_allowed(CLIENT)
    assert not scope.applicant_is_write_allowed(OTHER)
    assert scope.ezlynx_control_scope_block_reason(
        f'https://app.ezlynx.com/web/account/{CLIENT}/documents', requested_applicant_id=CLIENT) is None
    assert scope.ezlynx_control_scope_block_reason(
        f'https://app.ezlynx.com/web/account/{OTHER}/documents', requested_applicant_id=CLIENT)


def test_bound_production_job_stays_fail_closed_even_when_allowlist_unrestricted(running_ledger, monkeypatch):
    monkeypatch.setattr(scope, 'ALLOWED_EZLYNX_WRITE_APPLICANT_IDS', None)
    assert scope.write_allowlist_is_unrestricted()
    assert scope.production_job_applicant() == CLIENT
    assert scope.applicant_is_write_allowed(CLIENT)
    assert not scope.applicant_is_write_allowed(OTHER)
    assert scope.applicant_is_write_allowed('220250093') is False


@pytest.mark.parametrize('status', ['PENDING', 'COMPLETE', 'VERIFYING', 'FAILED', 'PAUSED'])
def test_only_running_jobs_grant_write_scope(running_ledger, status):
    with sqlite3.connect(running_ledger) as db:
        db.execute('UPDATE jobs SET status=?', (status,))
    assert not scope.applicant_is_write_allowed(CLIENT)


def test_missing_or_wrong_job_db_fails_closed(running_ledger, monkeypatch):
    monkeypatch.setenv('ROBIE_CURRENT_JOB_ID', 'missing')
    assert scope.production_job_applicant() is None
    monkeypatch.setenv('ROBIE_CURRENT_JOB_ID', 'job-1')
    monkeypatch.setenv('ROBIE_JOB_DB', str(running_ledger.parent / 'other.db'))
    assert scope.production_job_applicant() is None


def test_environment_flag_does_not_enable_local_or_test_writes(running_ledger, monkeypatch):
    monkeypatch.setattr(scope, '_is_installed_production_runtime', lambda: False)
    monkeypatch.setenv('ROBIE_ENV', 'PRODUCTION')
    monkeypatch.setenv('ROBIE_EZLYNX_WRITE_APPLICANT_ID', CLIENT)
    assert not scope.applicant_is_write_allowed(CLIENT)
    assert scope.applicant_is_write_allowed('220250093')


def test_conflicting_durable_binding_fails_closed(running_ledger):
    with sqlite3.connect(running_ledger) as db:
        db.execute('UPDATE jobs SET payload_json=?', (json.dumps({'text': REQUEST, 'applicant_id': OTHER}),))
    assert scope.production_job_applicant() is None


@pytest.mark.parametrize('href,count,visible,text,allowed', [
    (f'https://app.ezlynx.com/web/account/{CLIENT}/overview', 1, True, 'Example Client', True),
    (f'https://app.ezlynx.com/web/account/{OTHER}/overview', 1, True, 'Example Client', False),
    (f'https://evil.invalid/web/account/{CLIENT}/overview', 1, True, 'Example Client', False),
    (f'https://app.ezlynx.com/web/account/{CLIENT}/overview', 2, True, 'Example Client', False),
    (f'https://app.ezlynx.com/web/account/{CLIENT}/overview', 1, False, 'Example Client', False),
    (f'https://app.ezlynx.com/web/account/{CLIENT}/overview', 1, True, '', False),
])
def test_form_entry_requires_fresh_exact_client(running_ledger, href, count, visible, text, allowed):
    account = SimpleNamespace(count=lambda: count, is_visible=lambda: visible,
                              inner_text=lambda: text, get_attribute=lambda name: href)
    page = SimpleNamespace(locator=lambda selector: account)
    reason = attested_test_form_entry_block_reason(page,
        url='https://app.ezlynx.com/applicantportal/Policy/123/FormEntry/Index/456',
        requested_applicant_id=CLIENT)
    assert (reason is None) == allowed


def test_policy_start_resolves_original_request_but_does_not_grant_browser_scope(monkeypatch):
    monkeypatch.setattr(scope, 'ALLOWED_EZLYNX_WRITE_APPLICANT_IDS', frozenset({'220250093'}))
    job = {'id': 'new-job', 'action_type': 'ezlynx.policy_setup', 'payload': {
        'text': f'Create policy on applicant {CLIENT}', 'applicant_id': CLIENT}}
    assert hold_reason_for_job(job, env='PRODUCTION') is None
    assert not scope.applicant_is_write_allowed(CLIENT)
    job['payload']['applicant_id'] = OTHER
    assert hold_reason_for_job(job, env='PRODUCTION')


def test_conflicting_job_ids_refuse(running_ledger, monkeypatch):
    monkeypatch.setenv('ROBIE_JOB_ID', 'another-job')
    assert scope.production_job_applicant() is None


def test_worker_cannot_redirect_original_request(running_ledger):
    with sqlite3.connect(running_ledger) as db:
        db.execute('UPDATE jobs SET payload_json=?', (json.dumps({'text': f'Update applicant {OTHER}'}),))
    assert scope.production_job_applicant() == CLIENT
    assert not scope.applicant_is_write_allowed(OTHER)
    with sqlite3.connect(running_ledger) as db:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute('UPDATE job_intake SET payload_json=?', ('{}',))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute('DELETE FROM job_intake')


def test_worker_cannot_repair_conflicting_immutable_binding(running_ledger, monkeypatch):
    store = JobStore(str(running_ledger))
    job = store.create_job('hermes.google_chat_task', {'text': REQUEST, 'applicant_id': OTHER})
    with sqlite3.connect(running_ledger) as db:
        db.execute('UPDATE jobs SET status=?, payload_json=? WHERE id=?',
                   ('RUNNING', json.dumps({'text': REQUEST, 'applicant_id': CLIENT}), job['id']))
    monkeypatch.setenv('ROBIE_CURRENT_JOB_ID', job['id'])
    assert scope.production_job_applicant() is None
