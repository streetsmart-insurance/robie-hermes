"""Stopped Test install: fixture databases and fake systemd only."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import hashlib
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stopped_install', ROOT / 'scripts/test_stopped_install.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def database(path):
    with sqlite3.connect(path) as con:
        for table, fields in guard.REQUIRED.items():
            con.execute('CREATE TABLE ' + table + ' (' + ','.join(f'{f} TEXT' for f in sorted(fields)) + ')')
        con.execute('ALTER TABLE jobs ADD COLUMN updated_at TEXT')
        con.execute("INSERT INTO jobs(id,status,action_type) VALUES ('note','PENDING','ezlynx.discussion_note')")
        con.execute("INSERT INTO chat_event_queue(event_id,state) VALUES ('note-event','QUEUED')")
    return path


@pytest.mark.parametrize('table,field,value', [
    ('jobs','status','RUNNING'), ('jobs','status','VERIFYING'),
    ('jobs','lease_owner','owner'), ('jobs','lease_expires_at','2000-01-01'),
    ('chat_event_queue','state','INFLIGHT'), ('chat_event_queue','lease_owner','owner'),
    ('chat_event_queue','lease_expires_at','2000-01-01'),
])
def test_nonidle_and_expired_leases_are_refused_without_mutation(tmp_path, table, field, value):
    path = database(tmp_path / 'jobs.db')
    with sqlite3.connect(path) as con:
        con.execute(f'UPDATE {table} SET {field}=?', (value,))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Nonidle'):
        guard.database_snapshot(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('expiry', [None, '2000-01-01', '2100-01-01'])
def test_any_active_reservation_refused_never_abandoned(tmp_path, expiry):
    path = database(tmp_path / 'jobs.db')
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO isolated_runs(id,status,lease_expires_at) VALUES ('existing','ACTIVE',?)", (expiry,))
    with pytest.raises(ValueError, match='isolated_runs'):
        guard.database_snapshot(path)
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT status FROM isolated_runs').fetchone()[0] == 'ACTIVE'


def test_unknown_schema_fails_closed(tmp_path):
    path = database(tmp_path / 'jobs.db')
    with sqlite3.connect(path) as con:
        con.execute('DROP TABLE conversation_job_links')
    with pytest.raises(ValueError, match='schema'):
        guard.database_snapshot(path)


def test_queue_preservation_detects_changed_deleted_and_new_events(tmp_path):
    path = database(tmp_path / 'jobs.db')
    before = guard.database_snapshot(path)
    guard.verify_preserved(before, guard.database_snapshot(path))
    with sqlite3.connect(path) as con:
        con.execute("UPDATE jobs SET status='CANCELLED' WHERE id='note'")
    with pytest.raises(ValueError, match='durable row'):
        guard.verify_preserved(before, guard.database_snapshot(path))
    with sqlite3.connect(path) as con:
        con.execute("UPDATE jobs SET status='PENDING'")
        con.execute("INSERT INTO chat_event_queue(event_id,state) VALUES ('extra','QUEUED')")
    with pytest.raises(ValueError, match='Queue/run changed'):
        guard.verify_preserved(before, guard.database_snapshot(path))
    with sqlite3.connect(path) as con:
        con.execute('DELETE FROM chat_event_queue')
    with pytest.raises(ValueError, match='durable row'):
        guard.verify_preserved(before, guard.database_snapshot(path))


def test_unit_hold_requires_persistent_masks_and_stopped_processes(tmp_path):
    for unit in guard.UNITS:
        (tmp_path / unit).symlink_to('/dev/null')
    state = dict(LoadState='masked', ActiveState='inactive', SubState='dead',
                 MainPID='0', ControlPID='0', UnitFileState='masked')
    def run(*args, **kwargs):
        return SimpleNamespace(stdout='\n'.join(f'{k}={v}' for k,v in state.items()))
    guard.unit_snapshot(run, tmp_path)
    for key,value in [('UnitFileState','masked-runtime'), ('ActiveState','active'),
                      ('MainPID','42'), ('LoadState','loaded')]:
        old=state[key]; state[key]=value
        with pytest.raises(ValueError, match='Persistent'):
            guard.unit_snapshot(run, tmp_path)
        state[key]=old
    (tmp_path / guard.UNITS[0]).unlink()
    with pytest.raises(ValueError, match='Persistent'):
        guard.unit_snapshot(run, tmp_path)


def executable(path, content):
    path.write_text('#!/bin/bash\n' + content)
    path.chmod(0o755)
    return path


def installer_fixture(tmp_path, mode='success'):
    root=tmp_path/'opt'; root.mkdir()
    (root/'releases').mkdir()
    prior=root/'releases/prior'; prior.mkdir()
    (root/'current').symlink_to(prior)
    (root/'releases/current').symlink_to(prior)
    db=root/'robie-job-engine/data/jobs.db'; db.parent.mkdir(parents=True)
    database(db)
    commit='a'*40; short=commit[:12]
    source=tmp_path/f'robie-hermes-{short}'; (source/'scripts/lib').mkdir(parents=True)
    shutil.copy(ROOT/'scripts/lib/test-release-rollback.sh',source/'scripts/lib')
    executable(source/'scripts/verify-release.sh','exit 0\n')
    # Run the real guard against isolated paths; only host/uid/systemd are simulated.
    unit_dir=tmp_path/'units'; unit_dir.mkdir()
    for unit in guard.UNITS:
        (unit_dir/unit).symlink_to('/dev/null')
    guard_source=(ROOT/'scripts/test_stopped_install.py').read_text()
    guard_source=guard_source.replace('/opt/streetsmart-hermes-test',str(root))
    guard_source=guard_source.replace('/etc/systemd/system/robie-gateway.service.d',str(tmp_path/'dropin'))
    guard_source=guard_source.replace("Path('/etc/systemd/system')", "Path("+repr(str(unit_dir))+")")
    guard_source=guard_source.replace('info.st_uid != 0','info.st_uid != os.getuid()')
    guard_source=guard_source.replace("os.geteuid() != 0 or socket.gethostname().split('.')[0] != 'hermes-test-01'",'False')
    guard_source=guard_source.replace('    args = parser.parse_args()', """    args = parser.parse_args()
    if args.verify:
        if os.environ['MODE'] == 'verify-fail':
            raise ValueError('injected verification failure')
        if os.environ['MODE'] == 'death':
            Path(os.environ['WAITING']).touch()
            __import__('time').sleep(60)""")
    (source/'scripts/test_stopped_install.py').write_text(guard_source)
    executable(source/'scripts/install-official-release.sh','''echo "official $1" >> "$LOG"
if [[ "$1" != install ]]; then exit 99; fi
shift
while [[ $# -gt 0 ]]; do case "$1" in
 --opt-root) root="$2";; --release-root) release="$2";; esac; shift 2; done
ln -sfn "$release" "$root/current"
ln -sfn "$release" "$root/releases/current"
echo '{"flip_at":"2000-01-01T00:00:00+00:00"}' > "$release/official-install-flip.json"
[[ "$MODE" == install-fail ]] && exit 1
exit 2
''')
    (source/'.gateway-runtime').mkdir()
    (source/'.gateway-runtime/runtime').write_text('fixture')
    req=source/'deploy/requirements-test-gateway-playwright.txt'; req.parent.mkdir()
    req.write_text('fixture')
    archive=tmp_path/f'robie-hermes-{short}.tgz'
    with tarfile.open(archive,'w:gz') as tar:
        tar.add(source,arcname=source.name)
    checksum=Path(str(archive)+'.sha256')
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
    bins=tmp_path/'bin'; bins.mkdir()
    executable(bins/'hostname','echo hermes-test-01\n')
    interpreter=executable(bins/'gateway-python','echo interpreter-invoked >> "$LOG"\ncat >/dev/null\n')
    receipt={'version':1,'host':'hermes-test-01',
             'release_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
             'external_producers_fenced':True,'approved_outage_reference':'fixture',
             'gateway_interpreter':guard.interpreter_identity(str(interpreter)),
             'prior_units':{unit:{'active':'inactive','enabled':'disabled',
                 'mask_preexisting':False,'unit_backup':''} for unit in guard.UNITS}}
    (root/'deployments').mkdir()
    receipt_path=root/'deployments/stopped-install-hold.json'
    receipt_path.write_text(json.dumps(receipt)); receipt_path.chmod(0o600)
    mask=tmp_path/'gateway-mask'; mask.symlink_to('/dev/null')
    executable(bins/'systemctl',f'''echo "$*" >> "$LOG"
case "$*" in
 *ExecStart*) echo '';;
 *LoadState*) printf '%s\\n' LoadState=masked ActiveState=inactive SubState=dead MainPID=0 ControlPID=0 UnitFileState=masked;;
 *ActiveState*) echo inactive;;
 restart*) test ! -L "$MASK";;
esac
''')
    installer=tmp_path/'installer.sh'
    content=(ROOT/'scripts/deploy-test-release.sh').read_text()
    content=content.replace('/opt/streetsmart-hermes-test', str(root))
    content=content.replace('/etc/systemd/system/robie-gateway.service.d',str(tmp_path/'dropin'))
    content=content.replace('[[ "${EUID}" -eq 0 ]]','true')
    installer.write_text(content)
    env={**os.environ,'PATH':str(bins)+':'+os.environ['PATH'],'MODE':mode,
         'PYTHONPATH':str(ROOT), 'LOG':str(tmp_path/'log'),'MASK':str(mask),'WAITING':str(tmp_path/'waiting')}
    command=['bash',str(installer),'--archive',str(archive),'--checksum',str(checksum),
             '--commit',commit,'--skip-policy-setup','--keep-stopped']
    return command,env,root,prior,mask


@pytest.mark.parametrize('mode,success', [('success',True),('install-fail',False),('verify-fail',False)])
def test_install_and_rollback_never_start_or_restore_services(tmp_path,mode,success):
    cmd,env,root,prior,mask=installer_fixture(tmp_path,mode)
    result=subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=20)
    assert (result.returncode==0)==success, result.stdout+result.stderr
    log=Path(env['LOG']).read_text()
    assert 'restart ' not in log and ' start ' not in log and 'unmask' not in log
    assert 'official prove' not in log
    assert mask.is_symlink()
    if success:
        assert 'NO LIVE PROOF' in result.stdout
        assert (root/'current').resolve()!=prior
    else:
        assert (root/'current').resolve()==prior
        assert (root/'releases/current').resolve()==prior


@pytest.mark.parametrize('prior_config', [None, '[Service]\nEnvironment=PRIOR=1\n'])
def test_controller_kill_cannot_expire_hold_or_restart_gateway(tmp_path, prior_config):
    cmd,env,root,prior,mask=installer_fixture(tmp_path,'death')
    dropin=tmp_path/'dropin/zz-robie-test-release-runtime.conf'
    if prior_config is not None:
        dropin.parent.mkdir(); dropin.write_text(prior_config)
    proc=subprocess.Popen(cmd,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        deadline=time.monotonic()+10
        while not Path(env['WAITING']).exists() and time.monotonic()<deadline:
            time.sleep(.02)
        assert Path(env['WAITING']).exists()
        import signal
        os.killpg(proc.pid,signal.SIGKILL); proc.wait(timeout=5)
        assert mask.is_symlink()
        # Models PID1's refusal of a start against the persistent mask.
        attempted=subprocess.run(['systemctl','restart','robie-gateway'],env=env)
        assert attempted.returncode!=0
        assert 'unmask' not in Path(env['LOG']).read_text()
        # Recover in a fresh process using ONLY persisted inputs after SIGKILL.
        snapshot=root/'releases'/('a'*12)/'stopped-install-before.json'
        saved=json.loads(snapshot.read_text())['rollback']
        config=saved['runtime_dropin']
        if config['state']=='present':
            assert hashlib.sha256(Path(config['snapshot']).read_bytes()).hexdigest()==config['sha256']
        command=['bash','-c', 'source "$1"; restore_file_snapshot "$2" "$3" "$4"; systemctl daemon-reload; shift 4; rollback_test_release "$@"',
                 'recovery',str(ROOT/'scripts/lib/test-release-rollback.sh'),
                 config['state'],config['snapshot'],config['destination'],
                 saved['old_current'],saved['old_releases_current'],saved['old_policy_skill_target'],
                 saved['current_link'],saved['releases_current_link'],saved['policy_skill_link'],
                 saved['gateway_unit'],str(saved['restore_policy_skill']).lower(),
                 str(saved['restart_gateway']).lower()]
        recovered=subprocess.run(command,env=env,capture_output=True,text=True)
        assert recovered.returncode==0, recovered.stderr
        assert (root/'current').resolve()==prior
        assert (root/'releases/current').resolve()==prior
        assert dropin.read_text()==prior_config if prior_config is not None else not dropin.exists()
        assert mask.is_symlink()
        # The only attempted restart was our explicit PID1 refusal assertion.
        assert Path(env['LOG']).read_text().count('restart robie-gateway')==1
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid,9); proc.wait()


def test_hold_receipt_binds_digest_prior_states_and_external_fence(tmp_path, monkeypatch):
    path=tmp_path/'receipt.json'
    data={'version':1,'host':'hermes-test-01','release_sha256':'a'*64,
          'external_producers_fenced':True,'approved_outage_reference':'approved-window',
          'gateway_interpreter':guard.interpreter_identity(str(executable(tmp_path/'python','exit 0'))),
          'prior_units':{unit:{'active':'inactive','enabled':'disabled','mask_preexisting':False,
                              'unit_backup':''} for unit in guard.UNITS}}
    path.write_text(json.dumps(data)); path.chmod(0o600)
    original=Path.lstat
    def lstat(p):
        s=original(p)
        return os.stat_result((s.st_mode,s.st_ino,s.st_dev,s.st_nlink,0,s.st_gid,s.st_size,s.st_atime,s.st_mtime,s.st_ctime))
    monkeypatch.setattr(Path,'lstat',lstat)
    assert guard.hold_receipt(path,'a'*64)==data
    with pytest.raises(ValueError,match='mismatched'):
        guard.hold_receipt(path,'b'*64)
    data['external_producers_fenced']=False; path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Incomplete'):
        guard.hold_receipt(path,'a'*64)
    data['external_producers_fenced']=True
    data['prior_units'][guard.UNITS[0]]['enabled']='unknown'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Unknown prior'):
        guard.hold_receipt(path,'a'*64)
    path.chmod(0o644)
    with pytest.raises(ValueError,match='Private root-owned'):
        guard.hold_receipt(path,'a'*64)


def test_stopped_workflow_does_not_issue_live_package_or_add_access_route():
    import yaml
    workflow=yaml.safe_load((ROOT/'.github/workflows/deploy-test.yml').read_text())
    steps={step['name']:step for step in workflow['jobs']['deploy-test']['steps']}
    assert steps['Retain installed Test package for independent QA']['if']=="inputs.operation == 'deploy'"
    assert steps['Retain exact installed bytes; this does not authorize promotion']['if']=="inputs.operation == 'deploy'"
    held=steps['Retain stopped bytes without live certification']
    assert held['if']=="inputs.operation == 'install-stopped'"
    assert held['with']['retention-days']==30
    source=(ROOT/'scripts/test_stopped_install.py').read_text()
    assert 'runs.start(' not in source and 'renew_lease(' not in source and '.terminate(' not in source


def test_unknown_durable_status_refused(tmp_path):
    path=database(tmp_path/'jobs.db')
    with sqlite3.connect(path) as con:
        con.execute("UPDATE jobs SET status='NEW_UNREVIEWED_STATE'")
    with pytest.raises(ValueError,match='Unknown durable state'):
        guard.database_snapshot(path)


def test_optional_reply_sender_lease_refused(tmp_path):
    path=database(tmp_path/'jobs.db')
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE chat_reply_outbox(id TEXT,state TEXT,lease_token TEXT,lease_until REAL)')
        con.execute("INSERT INTO chat_reply_outbox VALUES ('reply','sending','owner',0)")
    with pytest.raises(ValueError,match='Nonidle reply'):
        guard.database_snapshot(path)


def test_only_exact_candidate_install_proof_may_be_appended(tmp_path):
    path=database(tmp_path/'jobs.db')
    before=guard.database_snapshot(path,'official-install-proof:abc')
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO jobs(id,status,action_type,idempotency_key) VALUES ('proof','PENDING','robie.official_install','official-install-proof:abc')")
    guard.verify_preserved(before,guard.database_snapshot(path,'official-install-proof:abc'))
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO jobs(id,status,action_type) VALUES ('unrelated','PENDING','ezlynx.discussion_note')")
    with pytest.raises(ValueError,match='Unexpected appended'):
        guard.verify_preserved(before,guard.database_snapshot(path,'official-install-proof:abc'))


@pytest.mark.parametrize('defect', ['missing', 'missing-executable', 'executable', 'venv', 'missing-venv'])
def test_interpreter_receipt_refuses_before_any_install_mutation(tmp_path, defect):
    cmd,env,root,prior,mask=installer_fixture(tmp_path)
    receipt_path=root/'deployments/stopped-install-hold.json'
    data=json.loads(receipt_path.read_text())
    invocation=Path(data['gateway_interpreter']['invocation'])
    config=invocation.parent.parent/'pyvenv.cfg'
    config.write_text('home = original\n')
    data['gateway_interpreter']=guard.interpreter_identity(str(invocation))
    if defect == 'missing':
        del data['gateway_interpreter']
    elif defect == 'missing-executable':
        invocation.unlink()
    elif defect == 'executable':
        invocation.write_text('#!/bin/bash\nexit 99\n')
    elif defect == 'venv':
        config.write_text('home = tampered\n')
    else:
        config.unlink()
    receipt_path.write_text(json.dumps(data))
    before=(root/'robie-job-engine/data/jobs.db').read_bytes()
    result=subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode != 0
    assert (root/'current').resolve()==prior
    assert not (root/'releases'/('a'*12)).exists()
    assert (root/'robie-job-engine/data/jobs.db').read_bytes()==before
    assert 'official' not in Path(env['LOG']).read_text()
    assert 'interpreter-invoked' not in Path(env['LOG']).read_text()


def real_database(path, monkeypatch):
    from robie_job_engine.store import JobStore
    from robie_job_engine.chat_queue import DurableChatEventQueue
    from robie_job_engine.runs import IsolatedRunStore
    from robie_job_engine import chat_reply_outbox, chat_queue
    # Only the ephemeral-path rule is bypassed for disposable fixtures.
    monkeypatch.setattr(chat_reply_outbox, 'assert_durable_path', lambda p: p)
    monkeypatch.setattr(chat_queue, 'assert_durable_path', lambda p: p)
    store=JobStore(path)
    note=store.create_job('ezlynx.discussion_note', {'fixture':True})
    queue=DurableChatEventQueue(path)
    queue.enqueue(event_id='note-event',conversation_id='fixture',message_id='message',
                  payload={'fixture':True},job_id=note['id'])
    runs=IsolatedRunStore(path)
    run=runs.start(owner='fixture',job_id=note['id'])
    runs.terminate(run['id'],'BLOCKED')
    outbox=chat_reply_outbox.ChatReplyOutbox(str(path))
    outbox.prepare(note['id'],'spaces/fixture','fixture',
                   [{'text':'fixture','thread':{'name':'spaces/fixture/threads/test'}}])
    return store


@pytest.mark.parametrize('existing', [True, False])
def test_real_proof_install_preserves_prior_durable_rows(tmp_path, monkeypatch, existing):
    from robie_job_engine.deploy_truth import persist_install_proof_row
    cmd,env,root,prior,mask=installer_fixture(tmp_path)
    db=root/'robie-job-engine/data/jobs.db'; db.unlink()
    store=real_database(db,monkeypatch)
    source=tmp_path/('robie-hermes-'+'a'*12)
    official=source/'scripts/install-official-release.sh'
    official.write_text(official.read_text().replace('exit 2', '''python3 - "$root/robie-job-engine/data/jobs.db" <<'PROOF'
import sys
from robie_job_engine.deploy_truth import persist_install_proof_row
persist_install_proof_row(sys.argv[1], {'sha':'aaaaaaaaaaaa','live':False})
PROOF
exit 2'''))
    archive=tmp_path/('robie-hermes-'+'a'*12+'.tgz')
    with tarfile.open(archive,'w:gz') as tar:
        tar.add(source,arcname=source.name)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(str(archive)+'.sha256').write_text(digest+'  '+archive.name+'\n')
    receipt_path=root/'deployments/stopped-install-hold.json'
    receipt=json.loads(receipt_path.read_text()); receipt['release_sha256']=digest
    receipt_path.write_text(json.dumps(receipt))
    if existing:
        proof=persist_install_proof_row(db, {'sha':'a'*12,'live':True,'evidence':{'fixture':True}})
    saved=guard.database_snapshot(db,'official-install-proof:'+'a'*12)
    with sqlite3.connect(db) as con:
        before=list(con.iterdump())
    result=subprocess.run(cmd,env=env,capture_output=True,text=True,timeout=20)
    if not existing:
        assert result.returncode==0, result.stdout+result.stderr
        guard.verify_preserved(saved,guard.database_snapshot(db,'official-install-proof:'+'a'*12))
        assert 'TEST INSTALLED STOPPED' in result.stdout
        return
    assert result.returncode != 0
    assert 'Existing official install proof key' in result.stderr
    with sqlite3.connect(db) as con:
        assert list(con.iterdump())==before
    assert store.get_checkpoint(proof['job_id'],'install_proof')['live'] is True
    assert not (root/'releases'/('a'*12)).exists()
    assert (root/'current').resolve()==prior
    assert 'official' not in Path(env['LOG']).read_text()


def test_original_venv_invocation_preserved(tmp_path):
    import sys
    venv=tmp_path/'venv'
    subprocess.run([sys.executable,'-m','venv','--without-pip',str(venv)],check=True)
    invocation=venv/'bin/python'
    identity=guard.interpreter_identity(str(invocation))
    verified=guard.verify_interpreter(identity)
    assert verified==str(invocation)
    assert identity['resolved']==str(invocation.resolve())
    result=subprocess.run([verified,'-c','import sys; print(sys.prefix)'],capture_output=True,text=True,check=True)
    assert result.stdout.strip()==str(venv)
