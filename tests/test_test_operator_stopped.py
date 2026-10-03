"""Host simulation only: temporary files, fixture SQLite, fake systemd/installer."""
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


op = load('stopped_operator', 'scripts/test_operator_stopped.py')
guard = load('approved_guard_fixture', 'scripts/test_stopped_install.py')
NOW = dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc)


def approval(target):
    return dict(version=1, enabled='STOPPED_OPERATOR_V1', host='hermes-test-01',
                commit=op.COMMIT, sha256=op.DIGEST, operations=['hold', 'install', 'verify'],
                actor_ids=[42], issue=900, not_before='2026-10-03T00:00:00Z',
                expires='2026-10-03T04:00:00Z', approved_outage_reference='fixture-approval',
                external_fence_reference='fixture-fence', drain_evidence_reference='fixture-drain',
                operator_handoff_reference='fixture-handoff', external_producers_fenced=True,
                manual_runners_fenced=True, workers_drained=True, exclusive_operator_handoff=True,
                gateway_already_inactive=True, cron_outage_included=True, no_resume=True,
                expected_prior_target=str(target))


def request(operation='hold'):
    return dict(version=1, operation=operation, commit=op.COMMIT, sha256=op.DIGEST,
                actor_id=42, issue=900, comment_id=123, nonce='a'*32,
                expires='2026-10-03T00:20:00Z')


@pytest.mark.parametrize('field,value', [('enabled',''), ('host','hermes-poc-01'),
    ('sha256','0'*64), ('actor_ids',[43]), ('issue',901), ('operations',['resume']),
    ('external_producers_fenced',False), ('manual_runners_fenced',False),
    ('workers_drained',False), ('exclusive_operator_handoff',False),
    ('cron_outage_included',False), ('no_resume',False), ('external_fence_reference',''),
    ('expires','2026-10-03T00:00:00Z'), ('expires','2026-10-05T00:00:00Z')])
def test_issue_command_never_substitutes_for_root_approval(field, value):
    plan = approval('/opt/streetsmart-hermes-test/releases/prior')
    op.validate_approval(plan, request(), NOW)
    plan[field] = value
    with pytest.raises(ValueError):
        op.validate_approval(plan, request(), NOW)


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / 'opt'
    units = tmp_path / 'units'
    state = tmp_path / 'state'
    package = state / 'approved' / op.COMMIT
    for path in (root / 'deployments', root / 'releases/prior', units, package):
        path.mkdir(parents=True, exist_ok=True)
    prior = root / 'releases/prior'
    (root / 'current').symlink_to(prior)
    (root / 'releases/current').symlink_to(prior)
    locations = dict(ROOT=root, STATE=state, UNIT_DIR=units, BACKUP_DIR=units / 'backup',
        PACKAGE=package, ARCHIVE=package / ('robie-hermes-' + op.SHORT + '.tgz'),
        ATTEMPT=state / ('stopped-' + op.SHORT), RECEIPT=root / 'deployments/stopped-install-hold.json',
        RELEASE=root / 'releases' / op.SHORT / ('robie-hermes-' + op.SHORT),
        DB=root / 'jobs.db')
    locations['SNAPSHOT'] = locations['RELEASE'].parent / 'stopped-install-before.json'
    for key, value in locations.items():
        monkeypatch.setattr(op, key, value)
    monkeypatch.setattr(op, 'ROOT_UID', os.getuid())
    monkeypatch.setattr(op, 'protected', lambda *a, **k: None)
    monkeypatch.setattr(op, 'preserved_state', lambda prior: {'fixture': 'unchanged'})
    with sqlite3.connect(op.DB) as con:
        for table, fields in guard.REQUIRED.items():
            con.execute('CREATE TABLE ' + table + ' (' + ','.join(f'{field} TEXT' for field in sorted(fields)) + ')')
        con.execute("INSERT INTO jobs(id,status,action_type) VALUES ('queued-note','PENDING','note')")
        con.execute("INSERT INTO chat_event_queue(event_id,state) VALUES ('queued-event','QUEUED')")
    original = {}
    for unit in op.UNITS:
        (units / unit).write_text('[Unit]\nDescription=fixture ' + unit + '\n')
        (units / unit).chmod(0o640)
        original[unit] = dict(LoadState='loaded', ActiveState='inactive', SubState='dead',
                              MainPID='0', ControlPID='0', UnitFileState='disabled', ControlGroup='')
    original['robie-scheduler.timer'].update(ActiveState='active', SubState='waiting', UnitFileState='enabled')
    (units / 'hermes-gateway.service').unlink()
    (units / 'hermes-gateway.service').symlink_to('/dev/null')
    original['hermes-gateway.service'].update(LoadState='masked', UnitFileState='masked')
    calls = []
    def unit_state(unit):
        result = dict(original[unit])
        if (units / unit).is_symlink() and os.readlink(units / unit) == '/dev/null':
            result.update(LoadState='masked', UnitFileState='masked')
        return result
    def command(args, **kwargs):
        calls.append(args)
        if 'ExecStart' in args:
            return '{ path=' + sys.executable + ' ; argv[]=python gateway ; }'
        if args[1] == 'stop':
            assert args[2].endswith('.timer')
            original[args[2]].update(ActiveState='inactive', SubState='dead')
        return ''
    def runner(args, **kwargs):
        return SimpleNamespace(stdout='\n'.join(f'{k}={v}' for k,v in unit_state(args[2]).items()))
    fake = SimpleNamespace(interpreter_identity=guard.interpreter_identity,
        verify_interpreter=guard.verify_interpreter, database_snapshot=guard.database_snapshot,
        verify_preserved=guard.verify_preserved,
        unit_snapshot=lambda: guard.unit_snapshot(runner, units),
        hold_receipt=lambda path, digest: json.loads(path.read_text()),
        RUNTIME_DROPIN=units / 'runtime.conf')
    monkeypatch.setattr(op, 'unit_state', unit_state)
    monkeypatch.setattr(op, 'command', command)
    return SimpleNamespace(plan=approval(prior), guard=fake, prior=prior, calls=calls,
                           states=original, units=units, unit_state=unit_state)


def test_hold_captures_before_mask_and_preserves_backups_queue_and_preexisting_mask(host):
    db = op.DB.read_bytes()
    gateway = host.units / 'robie-gateway.service'
    metadata = op.unit_file(gateway)
    preexisting = (host.units / 'hermes-gateway.service').lstat().st_ino
    result = op.hold(host.plan, 'approval-hash', host.guard)
    assert result['status'] == 'HOLD ESTABLISHED'
    saved = op.read_private(op.ATTEMPT / 'before.json')
    assert saved['receipt']['gateway_interpreter']['invocation'] == sys.executable
    assert saved['receipt']['prior_units']['robie-scheduler.timer']['active'] == 'active'
    assert op.unit_file(op.BACKUP_DIR / 'robie-gateway.service') == metadata
    assert (host.units / 'hermes-gateway.service').lstat().st_ino == preexisting
    assert op.DB.read_bytes() == db
    assert op.RECEIPT.stat().st_mode & 0o777 == 0o600
    assert all(call[1] not in {'start', 'restart', 'unmask'} for call in host.calls)
    assert all(call[2].endswith('.timer') for call in host.calls if call[1] == 'stop')
    with pytest.raises(ValueError):
        op.hold(host.plan, 'approval-hash', host.guard)


@pytest.mark.parametrize('unit', ['robie-gateway.service', 'cron.service', 'robie-scheduler.service'])
def test_active_services_require_separate_safe_drain_before_any_mask(host, unit):
    host.states[unit].update(ActiveState='active', SubState='running', MainPID='22')
    with pytest.raises(ValueError, match='DRAIN_REQUIRED'):
        op.hold(host.plan, 'approval-hash', host.guard)
    assert not op.ATTEMPT.exists()
    assert host.calls == []


def test_masked_gateway_without_original_capture_is_never_unmasked(host):
    path = host.units / 'robie-gateway.service'
    path.unlink()
    path.symlink_to('/dev/null')
    with pytest.raises(ValueError, match='ORIGINAL_INTERPRETER_REQUIRED'):
        op.hold(host.plan, 'approval-hash', host.guard)
    assert host.calls == []


def test_unresolved_lease_refused_without_repair(host):
    with sqlite3.connect(op.DB) as con:
        con.execute("UPDATE jobs SET lease_expires_at='2000-01-01'")
    db = op.DB.read_bytes()
    with pytest.raises(ValueError, match='Nonidle'):
        op.hold(host.plan, 'approval-hash', host.guard)
    assert not op.ATTEMPT.exists()
    assert op.DB.read_bytes() == db


def test_interrupted_mask_attempt_leaves_evidence_and_never_retries(host, monkeypatch):
    base = op.command
    def fail_reload(args, **kwargs):
        if args[1] == 'daemon-reload':
            raise RuntimeError('injected')
        return base(args, **kwargs)
    monkeypatch.setattr(op, 'command', fail_reload)
    with pytest.raises(RuntimeError):
        op.hold(host.plan, 'approval-hash', host.guard)
    assert (op.ATTEMPT / 'before.json').is_file()
    assert (op.BACKUP_DIR / 'robie-gateway.service').is_file()
    assert not op.RECEIPT.exists()
    assert (host.units / 'robie-gateway.service').is_symlink()
    with pytest.raises(ValueError, match='ATTEMPT_REQUIRES_REVIEW'):
        op.hold(host.plan, 'approval-hash', host.guard)


def test_install_uses_only_exact_stopped_flags_and_failure_cannot_retry(host, monkeypatch):
    op.hold(host.plan, 'approval-hash', host.guard)
    files = {'robie-hermes-' + op.SHORT + '/scripts/deploy-test-release.sh': b'fixture'}
    calls = []
    def installer(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(op.subprocess, 'run', installer)
    monkeypatch.setattr(op, 'LOCK_FD', 100, raising=False)
    with pytest.raises(ValueError, match='INSTALL_FAILED'):
        op.install(host.guard, files, 'approval-hash')
    assert calls[0][0][-2:] == ['--skip-policy-setup', '--keep-stopped']
    assert calls[0][1]['pass_fds'] == (100,)
    assert calls[0][1]['env'] == op.ENV
    assert (op.ATTEMPT / 'install-started.json').is_file()
    with pytest.raises(FileExistsError):
        op.install(host.guard, files, 'approval-hash')
    assert len(calls) == 1


def test_backup_or_root_approval_change_blocks_followup(host):
    op.hold(host.plan, 'approval-hash', host.guard)
    with pytest.raises(ValueError, match='APPROVAL_CHANGED'):
        op.existing_hold(host.guard, 'different')
    (op.BACKUP_DIR / 'robie-gateway.service').write_text('changed')
    with pytest.raises(ValueError, match='UNIT_BACKUP_CHANGED'):
        op.existing_hold(host.guard, 'approval-hash')


def test_bad_archive_never_executes_or_extracts(host):
    op.ARCHIVE.write_bytes(b'wrong archive')
    with pytest.raises(ValueError, match='ARCHIVE_MISMATCH'):
        op.archive_bytes()
    assert not op.ATTEMPT.exists()


def installed_fixture(host, establish_hold=True):
    if establish_hold:
        op.hold(host.plan, 'approval-hash', host.guard)
    files = {'robie-hermes-' + op.SHORT + '/robie_job_engine/deploy_truth.py':
             (ROOT / 'robie_job_engine/deploy_truth.py').read_bytes()}
    op.RELEASE.mkdir(parents=True)
    for name, content in files.items():
        path = op.RELEASE / Path(name).relative_to('robie-hermes-' + op.SHORT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (op.RELEASE / '.release-sha256').write_text(op.DIGEST)
    (op.RELEASE / '.gateway-runtime').mkdir()
    (op.RELEASE / '.gateway-runtime/dependency.py').write_text('fixture')
    truth = op.candidate_module(files, 'robie_job_engine/deploy_truth.py', 'fixture_truth')
    for item in truth.CHAT_RUNTIME_FILES:
        path = op.ROOT / '.hermes' / item.dest_relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(truth.zip_load_shim_source(item.zip_relpath))
    for path in (op.ROOT / 'current', op.ROOT / 'releases/current'):
        path.unlink()
        path.symlink_to(op.RELEASE)
    host.guard.RUNTIME_DROPIN.write_text('[Service]\nEnvironment="PYTHONPATH=' +
        str(op.ROOT / 'releases/current/.gateway-runtime') + ':' + str(op.ROOT / 'releases/current') +
        ':' + str(op.ROOT / '.hermes/hermes-agent') + '"\n')
    before = op.read_private(op.ATTEMPT / 'before.json')
    op.save(op.SNAPSHOT, dict(hold_receipt=before['receipt'], units=host.guard.unit_snapshot(),
        database=before['database'], rollback=dict(old_current=str(host.prior),
        old_releases_current=str(host.prior), restart_gateway=False, restore_policy_skill=False,
        runtime_dropin={'state':'absent'})))
    return files


def test_successful_install_writes_result_only_after_independent_checks(host, monkeypatch):
    op.hold(host.plan, 'approval-hash', host.guard)
    files = {'robie-hermes-' + op.SHORT + '/robie_job_engine/deploy_truth.py':
             (ROOT / 'robie_job_engine/deploy_truth.py').read_bytes(),
             'robie-hermes-' + op.SHORT + '/scripts/deploy-test-release.sh': b'fixture'}
    def installer(args, **kwargs):
        installed_fixture(host, establish_hold=False)
        target = op.RELEASE / 'scripts/deploy-test-release.sh'
        target.parent.mkdir()
        target.write_bytes(b'fixture')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(op.subprocess, 'run', installer)
    monkeypatch.setattr(op, 'LOCK_FD', 100, raising=False)
    result = op.install(host.guard, files, 'approval-hash')
    assert result['status'] == 'TEST INSTALLED STOPPED'
    assert op.read_private(op.ATTEMPT / 'install-complete.json') == result
    assert result['live'] is False and result['durable_rows_preserved'] is True


def test_independent_stopped_verifier_checks_bytes_without_starting_gateway(host):
    files = installed_fixture(host)
    db = op.DB.read_bytes()
    result = op.verify(host.guard, files, 'approval-hash')
    assert result['status'] == 'TEST INSTALLED STOPPED' and result['live'] is False
    assert result['independent_qa'] is False
    assert op.DB.read_bytes() == db
    assert host.calls[-1][1:3] == ['-I', '-B']  # dependency import only
    assert not any('gateway run' in ' '.join(call) for call in host.calls)


@pytest.mark.parametrize('kind', ['source','overlay','queue','runtime','preserved','dropin'])
def test_verifier_detects_changed_state(host, monkeypatch, kind):
    files = installed_fixture(host)
    result = op.verify(host.guard, files, 'approval-hash')
    op.save(op.ATTEMPT / 'install-complete.json', result)
    if kind == 'source':
        (op.RELEASE / 'robie_job_engine/deploy_truth.py').write_text('changed')
    elif kind == 'overlay':
        (op.ROOT / '.hermes/scripts/robie_email_agent.py').write_text('changed')
    elif kind == 'queue':
        with sqlite3.connect(op.DB) as con:
            con.execute("UPDATE jobs SET status='CANCELLED'")
    elif kind == 'runtime':
        (op.RELEASE / '.gateway-runtime/dependency.py').write_text('changed')
    elif kind == 'preserved':
        monkeypatch.setattr(op, 'preserved_state', lambda prior: {'fixture':'changed'})
    else:
        host.guard.RUNTIME_DROPIN.write_text('changed')
    calls_before = len(host.calls)
    with pytest.raises(ValueError):
        op.verify(host.guard, files, 'approval-hash')
    if kind == 'runtime':
        assert len(host.calls) == calls_before  # changed dependencies never imported


def test_tree_fingerprint_detects_content_mode_and_link_changes(tmp_path):
    (tmp_path / 'file').write_text('before')
    (tmp_path / 'file').chmod(0o644)
    original = op.tree_digest(tmp_path)
    (tmp_path / 'file').write_text('after')
    assert op.tree_digest(tmp_path) != original
    before_mode = op.tree_digest(tmp_path)
    (tmp_path / 'file').chmod(0o600)
    assert op.tree_digest(tmp_path) != before_mode
    (tmp_path / 'link').symlink_to('file')
    before_link = op.tree_digest(tmp_path)
    (tmp_path / 'link').unlink()
    (tmp_path / 'link').symlink_to('other')
    assert op.tree_digest(tmp_path) != before_link
    before_root_mode = op.tree_digest(tmp_path)
    tmp_path.chmod(0o750)
    assert op.tree_digest(tmp_path) != before_root_mode


def test_stopped_workflow_has_no_freeform_shell_or_pre_auth_concurrency():
    text = (ROOT / '.github/workflows/test-operator-stopped.yml').read_text()
    data = yaml.safe_load(text)
    assert 'concurrency' not in data
    job = data['jobs']['operate']
    assert job['needs'] == 'authorize'
    assert "needs.authorize.result == 'success'" in job['if']
    assert job['environment'] == 'Test-Operator-Stopped'
    assert '${{ github.event.comment.body }}' not in text
    assert '/usr/local/libexec/robie-test-stopped-operator.py operate' in text
    assert 'bash scripts/build-release.sh' not in text
