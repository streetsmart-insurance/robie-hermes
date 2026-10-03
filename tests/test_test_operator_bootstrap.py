"""Local fixtures only: no connector comments, WIF, SSH or privileged host calls."""
import ast
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import types

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
bootstrap = runpy.run_path(str(ROOT / 'scripts/test_operator_bootstrap.py'))
proof = runpy.run_path(str(ROOT / 'scripts/test_operator_setup_proof.py'))
inspect = runpy.run_path(str(ROOT / 'scripts/test_operator_inspect.py'))
COMMIT = 'a' * 40


def req():
    return dict(operation='bootstrap-inspect', actor_id=320188404, issue=901, pr_id=12345, expires='2099-10-03T00:30:00Z',
                commit=inspect['RELEASE'], sha256=inspect['DIGEST'], comment_id=123, nonce='a'*32)


def test_render_only_fixed_data_and_inspection_config():
    source = b'print("inspection fixture")\n'
    program = bootstrap['render'](req(), COMMIT, COMMIT, source)
    tree = ast.parse(program)
    compile(tree, 'rendered', 'exec')
    prefix = program.split(bootstrap['REMOTE'])[0]
    namespace = {}
    exec(prefix, namespace)
    payload = namespace['PAYLOAD']
    assert base64.b64decode(payload['helper_base64']) == source
    assert payload['config']['enabled'] == 'INSPECT_ONLY_V1'
    assert payload['config']['actor_ids'] == [320188404]
    assert payload['controller_commit'] == COMMIT
    assert 'stopped-operator.py' not in program and 'deploy-test-release' not in program


@pytest.mark.parametrize('field,value', [('actor_id',42), ('operation','prepare-hold'), ('issue','901')])
def test_render_refuses_wrong_scope(field, value):
    request = req(); request[field] = value
    with pytest.raises(ValueError):
        bootstrap['render'](request, COMMIT, COMMIT, b'fixture')


def test_render_requires_exact_approved_controller():
    with pytest.raises(ValueError):
        bootstrap['render'](req(), COMMIT, 'b'*40, b'fixture')


def proof_fixture():
    run = dict(repository={'id':1343750842}, head_sha=COMMIT,
               path='.github/workflows/test-operator-event-proof.yml', event='issue_comment', head_branch='main',
               conclusion='success', status='completed', run_attempt=1,
               actor={'id':320188404}, triggering_actor={'id':320188404})
    jobs = dict(total_count=1, jobs=[dict(name='Prove connector event', conclusion='success', steps=[
        dict(name='Validate connector event without cloud credentials', conclusion='success')])])
    return run, jobs


def test_proof_requires_executed_validator_not_merely_green_workflow():
    run, jobs = proof_fixture()
    proof['validate'](run, jobs, COMMIT)
    jobs['jobs'][0]['steps'][0]['conclusion'] = 'skipped'
    with pytest.raises(ValueError):
        proof['validate'](run, jobs, COMMIT)


@pytest.mark.parametrize('field,value', [('head_sha','b'*40),('event','workflow_dispatch'),
    ('run_attempt',2), ('path','.github/workflows/other.yml'), ('actor',{'id':42}), ('head_branch','feature')])
def test_proof_wrong_identity_refused(field,value):
    run,jobs=proof_fixture(); run[field]=value
    with pytest.raises(ValueError):
        proof['validate'](run,jobs,COMMIT)


def test_event_proof_has_no_cloud_credentials_or_deployment_queue():
    path = ROOT / '.github/workflows/test-operator-event-proof.yml'
    raw=path.read_text(); workflow=yaml.safe_load(raw)
    assert workflow['permissions'] == {}
    assert workflow['jobs']['prove']['permissions'] == {'contents':'read','issues':'read','pull-requests':'read'}
    assert 'id-token' not in raw and 'gcloud' not in raw and 'environment:' not in raw
    assert 'concurrency' not in raw and 'google-github-actions' not in raw
    assert 'github.sha == vars.ROBIE_TEST_OPERATOR_SETUP_COMMIT' in raw


def test_bootstrap_cloud_job_is_exact_commit_gated_and_rechecks_proof():
    raw=(ROOT / '.github/workflows/test-operator-bootstrap.yml').read_text()
    workflow=yaml.safe_load(raw); jobs=workflow['jobs']
    assert 'github.sha == vars.ROBIE_TEST_OPERATOR_SETUP_COMMIT' in jobs['authorize']['if']
    assert jobs['bootstrap']['needs']=='authorize'
    assert jobs['bootstrap']['environment']=='Test-Operator-Setup'
    assert 'id-token' not in jobs['authorize']['permissions']
    assert raw.count('scripts/test_operator_setup_proof.py')==2
    assert raw.count('scripts/test_operator_request.py')==2
    assert '${{ github.event.comment.body }}' not in raw
    assert "--command='sudo -n /usr/bin/python3 -I -B -'" in raw
    assert 'deploy-test-release.sh' not in raw
    assert 'OSLOGIN_SSH_KEY_TTL: 1h' in raw


def test_bootstrap_refuses_existing_files_and_writes_exclusively():
    remote=bootstrap['REMOTE']
    assert 'O_EXCL | os.O_NOFOLLOW' in remote
    assert 'not helper.exists()' in remote and 'not config.exists()' in remote
    assert "inspection-bootstrap.json" in remote
    assert 'subprocess' not in remote and 'systemctl' not in remote
    assert 'stopped_operations_enabled_by_bootstrap=False' in remote


def test_diagnostics_redact_stop_commands_and_report_missing_properties():
    runner=lambda *a,**k: types.SimpleNamespace(stdout='ExecStop=secret-command\nKillMode=process\n')
    result=inspect['stop_diagnostic']('cron.service',runner)
    assert result['properties']['ExecStop']=={'present':True}
    assert 'secret-command' not in json.dumps(result)
    assert 'SendSIGKILL' in result['missing_properties']


def test_lock_diagnostics_read_metadata_only(tmp_path,monkeypatch):
    root=tmp_path/'root'; data=root/'robie-job-engine/data'; data.mkdir(parents=True)
    lock=data/'actual-worker.lock'; lock.write_text('DO NOT READ OR CHANGE THIS')
    before=lock.stat()
    proc=tmp_path/'proc'; proc.mkdir(); (proc/'locks').write_text(
        f'1: FLOCK ADVISORY WRITE 77 {os.major(before.st_dev):02x}:{os.minor(before.st_dev):02x}:{before.st_ino} 0 EOF\n')
    for name in ('self','1'):
        (proc/name/'ns').mkdir(parents=True); (proc/name/'ns/pid').symlink_to('pid:[1]')
    original=Path.read_text
    def read(path,*a,**k):
        assert path != lock
        return original(path,*a,**k)
    monkeypatch.setattr(Path,'read_text',read)
    result=inspect['lock_candidates'](root,proc)
    assert result['candidates'][0]['owners'][0]['pid']=='77'
    assert result['candidates'][0]['inode']==before.st_ino
    assert lock.stat().st_ino==before.st_ino


def test_known_six_auxiliary_units_match_disabled_hold_inventory():
    config=json.loads((ROOT/'deploy/test-stopped-approval.disabled.json').read_text())
    assert tuple(config['auxiliary_units'])==inspect['AUXILIARY_UNITS']
    assert len(config['auxiliary_units'])==6
    assert config['enabled']=='DISABLED' and config['prepare_hold_authorized'] is False


@pytest.fixture
def remote_host(tmp_path, monkeypatch):
    namespace = {}
    exec(bootstrap['REMOTE'].split('\ntry:\n    run(PAYLOAD)')[0], namespace)
    for name in ('usr/local', 'etc', 'var/lib'):
        (tmp_path/name).mkdir(parents=True)
    original_path = Path
    namespace['Path'] = lambda path: tmp_path / str(path).lstrip('/')
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(namespace['socket'], 'gethostname', lambda: 'hermes-test-01')
    # Tests run unprivileged. Exercise path/mode and write semantics in a private
    # temp tree; production safe() additionally enforces root ownership/ancestors.
    def safe(path, directory=False, private=False):
        assert path.is_relative_to(tmp_path) and not path.is_symlink()
        assert path.is_dir() if directory else path.is_file()
        assert not path.stat().st_mode & (0o077 if private else 0o022)
    namespace['safe'] = safe
    program = bootstrap['render'](req(), COMMIT, COMMIT, b'# inspected fixture\n')
    payload_ns = {}
    exec(program.split(bootstrap['REMOTE'])[0], payload_ns)
    return namespace, payload_ns['PAYLOAD'], tmp_path


def test_remote_bootstrap_installs_only_inspection_and_refuses_replay(remote_host):
    namespace, payload, root = remote_host
    namespace['run'](payload)
    helper = root/'usr/local/libexec/robie-test-operator.py'
    config = root/'etc/robie-test-operator.json'
    assert helper.read_bytes()==b'# inspected fixture\n'
    assert config.stat().st_mode & 0o777 == 0o600
    assert json.loads(config.read_text())['enabled']=='INSPECT_ONLY_V1'
    state=root/'var/lib/robie-test-operator'
    claim=(state/'inspection-bootstrap.json').read_bytes()
    with pytest.raises(ValueError):
        namespace['run'](payload)
    assert (state/'inspection-bootstrap.json').read_bytes()==claim
    assert not (root/'opt').exists()


def test_remote_bootstrap_preserves_existing_config_before_any_setup(remote_host):
    namespace,payload,root=remote_host
    config=root/'etc/robie-test-operator.json';config.write_text('prior setup')
    with pytest.raises(ValueError):
        namespace['run'](payload)
    assert config.read_text()=='prior setup'
    assert not (root/'var/lib/robie-test-operator').exists()


def test_remote_bootstrap_partial_install_retains_claim_and_never_overwrites(remote_host):
    namespace,payload,root=remote_host
    write=namespace['write']
    def fail_config(path,data,mode):
        if path.name=='robie-test-operator.json':
            raise OSError('fixture interruption')
        return write(path,data,mode)
    namespace['write']=fail_config
    with pytest.raises(OSError):
        namespace['run'](payload)
    claim=root/'var/lib/robie-test-operator/inspection-bootstrap.json'
    assert claim.is_file()
    assert not (claim.parent/'inspection-bootstrap-complete.json').exists()
    namespace['write']=write
    with pytest.raises(ValueError):
        namespace['run'](payload)


@pytest.mark.parametrize('mode,operation', [('EVENT_PROOF_V1','event-proof'),('BOOTSTRAP_INSPECT_V1','bootstrap-inspect')])
def test_setup_commands_bind_actor_mode_and_controller(mode,operation):
    import datetime as dt
    trigger=runpy.run_path(str(ROOT/'scripts/test_operator_request.py'))
    fresh=dict(id=100, body=f'/robie-test {operation} commit={inspect["RELEASE"]} sha256={inspect["DIGEST"]} nonce={"a"*32} expires=2026-10-03T00:20:00Z',
               user={'id':320188404,'type':'User'},created_at='2026-10-03T00:00:00Z',updated_at='2026-10-03T00:00:00Z',
               issue_url='https://api.github.com/repos/streetsmart-insurance/robie-hermes/issues/901')
    event=dict(action='created',repository={'id':1343750842,'full_name':'streetsmart-insurance/robie-hermes'},
               issue={'number':901, 'pull_request':{'url':'https://api.github.com/repos/streetsmart-insurance/robie-hermes/pulls/901'}},comment=copy.deepcopy(fresh),sender={'id':320188404})
    config=dict(enabled=mode,ref='refs/heads/main',attempt='1',issue='901',actor_ids='320188404',
                controller_commit=COMMIT,approved_controller_commit=COMMIT,pr_id='12345',
                controller_tree='b'*40, checkout_tree='b'*40, approved_controller_tree='b'*40)
    pr=dict(id=12345, number=901, merged=True, state='closed', merge_commit_sha=COMMIT,
            base={'ref':'main','repo':{'id':1343750842,'full_name':'streetsmart-insurance/robie-hermes'}})
    permission={'user':{'id':320188404},'permission':'admin'}
    now=dt.datetime(2026,10,3,tzinfo=dt.timezone.utc)
    assert trigger['validate'](event,fresh,permission,config,now,pr)['operation']==operation
    for changed in (dict(config,enabled='INSPECT_ONLY_V1'),dict(config,approved_controller_commit='b'*40),
                    dict(config,actor_ids='320188404,42')):
        with pytest.raises(ValueError):
            trigger['validate'](event,fresh,permission,changed,now,pr)
