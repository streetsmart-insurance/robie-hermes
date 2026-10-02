#!/usr/bin/env python3
"""Test-only precondition/evidence for an install that NEVER starts the gateway.

Does not establish or release maintenance. Persistent masks must be established
by an approved operator first. No JobStore/IsolatedRunStore initialization.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess

ROOT = Path('/opt/streetsmart-hermes-test')
RUNTIME_DROPIN = Path('/etc/systemd/system/robie-gateway.service.d/zz-robie-test-release-runtime.conf')
UNITS = (
    'robie-gateway.service', 'hermes-gateway.service',
    'robie-scheduler.timer', 'robie-scheduler.service',
    'robie-ezlynx-keepalive-test.timer', 'robie-ezlynx-keepalive-test.service',
    'hermes-email-watcher.timer', 'hermes-email-watcher.service',
    'cron.service', 'crond.service',
)
REQUIRED = {
    'jobs': {'id', 'status', 'action_type', 'idempotency_key', 'lease_owner', 'lease_expires_at'},
    'chat_event_queue': {'event_id', 'state', 'lease_owner', 'lease_expires_at'},
    'conversation_job_links': {'id', 'job_id', 'active'},
    'isolated_runs': {'id', 'status', 'owner', 'lease_expires_at'},
    'checkpoints': {'id', 'job_id'}, 'job_intake': {'job_id'},
    'attempts': {'id', 'job_id'},
}


def interpreter_identity(invocation):
    """Capture before masking; preserve the invocation path for venv semantics."""
    path = Path(invocation)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('Gateway interpreter unavailable')
    resolved = path.resolve(strict=True)
    # CPython checks alongside the invocation and one directory above it.
    configs = [path.parent / 'pyvenv.cfg', path.parent.parent / 'pyvenv.cfg']
    return {'invocation': str(path), 'resolved': str(resolved),
            'sha256': hashlib.sha256(resolved.read_bytes()).hexdigest(),
            'venv_metadata': {str(config): hashlib.sha256(config.read_bytes()).hexdigest()
                              if config.exists() or config.is_symlink() else None for config in configs}}


def verify_interpreter(data):
    if not isinstance(data, dict) or not data.get('invocation'):
        raise ValueError('Missing captured gateway interpreter')
    if interpreter_identity(data['invocation']) != data:
        raise ValueError('Gateway interpreter or venv metadata changed')
    return data['invocation']


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rollback_inputs(target):
    """Save config bytes and exact pointer targets before installer mutations."""
    current = (ROOT / 'current').resolve(strict=True)
    releases_current = (ROOT / 'releases/current').resolve(strict=True)
    if current != releases_current:
        raise ValueError('Test rollback pointers disagree')
    backup = target.parent / ('.pre-' + target.parent.name + '-gateway-runtime.conf')
    config = {'destination': str(RUNTIME_DROPIN), 'state': 'absent',
              'snapshot': str(backup), 'sha256': None}
    if RUNTIME_DROPIN.is_symlink():
        raise ValueError('Runtime drop-in must not be a symlink')
    if RUNTIME_DROPIN.exists():
        content = RUNTIME_DROPIN.read_bytes()
        with backup.open('xb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        fsync_directory(backup.parent)
        config.update(state='present', sha256=hashlib.sha256(content).hexdigest())
    return {'old_current': str(current), 'old_releases_current': str(releases_current),
            'current_link': str(ROOT / 'current'),
            'releases_current_link': str(ROOT / 'releases/current'),
            'old_policy_skill_target': '',
            'policy_skill_link': str(ROOT / '.hermes/skills/ezlynx-policy-setup'),
            'gateway_unit': 'robie-gateway', 'restore_policy_skill': False,
            'restart_gateway': False, 'runtime_dropin': config}


def hold_receipt(path, digest):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Private root-owned hold receipt required')
    data = json.loads(path.read_text())
    if (data.get('version') != 1 or data.get('host') != 'hermes-test-01'
            or data.get('release_sha256') != digest
            or data.get('external_producers_fenced') is not True
            or not data.get('approved_outage_reference')
            or set(data.get('prior_units', {})) != set(UNITS)):
        raise ValueError('Incomplete or mismatched approved hold receipt')
    for prior in data['prior_units'].values():
        if (prior.get('active') not in {'active', 'inactive', 'failed', 'not-found'}
                or prior.get('enabled') not in {'enabled', 'disabled', 'static', 'indirect',
                                               'masked', 'not-found', 'enabled-runtime'}
                or not isinstance(prior.get('mask_preexisting'), bool)
                or not isinstance(prior.get('unit_backup'), str)):
            raise ValueError('Unknown prior unit state; refuse unverifiable restoration')
    verify_interpreter(data.get('gateway_interpreter'))
    return data


def unit_snapshot(runner=subprocess.run, unit_dir=Path('/etc/systemd/system')):
    result = {}
    for unit in UNITS:
        proc = runner(['systemctl', 'show', unit, '--no-pager',
                       '--property=LoadState,ActiveState,SubState,MainPID,ControlPID,UnitFileState'],
                      capture_output=True, text=True, check=True, timeout=10)
        state = dict(line.split('=', 1) for line in proc.stdout.splitlines() if '=' in line)
        if state.get('LoadState') == 'not-found' and unit != 'robie-gateway.service':
            if state.get('ActiveState') != 'inactive':
                raise ValueError('Unexpected missing unit state: ' + unit)
        else:
            mask = unit_dir / unit
            if (not mask.is_symlink() or os.readlink(mask) != '/dev/null'
                    or state.get('LoadState') != 'masked'
                    or state.get('UnitFileState') != 'masked'
                    or state.get('ActiveState') != 'inactive'
                    or state.get('SubState') != 'dead'
                    or any(state.get(k, '0') != '0' for k in ('MainPID', 'ControlPID'))):
                raise ValueError('Persistent stopped mask required: ' + unit)
        result[unit] = state
    return result


def database_snapshot(path, proof_key='', *, reject_existing_proof=False):
    """Hash existing rows only; never expose payloads or reconcile expired runs."""
    if not path.is_file() or path.is_symlink():
        raise ValueError('Regular existing Test database required')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        for table, expected in REQUIRED.items():
            columns = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
            if not expected <= columns:
                raise ValueError('Unknown or missing schema: ' + table)
        if reject_existing_proof and con.execute(
                'SELECT 1 FROM jobs WHERE idempotency_key=? LIMIT 1', (proof_key,)).fetchone():
            raise ValueError('Existing official install proof key; stopped install refused')
        states = {
            'jobs': ('status', {'PENDING', 'RUNNING', 'VERIFYING', 'PAUSED', 'WAITING',
                'RETRY_WAIT', 'AWAITING_HUMAN_INPUT', 'NEEDS_AUTH', 'NEEDS_SKILL',
                'NEEDS_CLARIFICATION', 'COMPLETE', 'FAILED', 'UNVERIFIED', 'CANCELLED'}),
            'chat_event_queue': ('state', {'QUEUED', 'INFLIGHT', 'AWAITING_HUMAN_INPUT', 'COMPLETE', 'FAILED'}),
            'isolated_runs': ('status', {'ACTIVE', 'INTAKE', 'COMPLETE', 'FAILED',
                'UNVERIFIED', 'CANCELLED', 'BLOCKED', 'ABANDONED'}),
        }
        for table, (column, known) in states.items():
            if not {row[0] for row in con.execute(f'SELECT DISTINCT {column} FROM {table}')} <= known:
                raise ValueError('Unknown durable state: ' + table)
        for table, predicate in (
            ('jobs', "status IN ('RUNNING','VERIFYING') OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL"),
            ('chat_event_queue', "state='INFLIGHT' OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL"),
            ('isolated_runs', "status='ACTIVE'"),
        ):
            if con.execute(f'SELECT 1 FROM {table} WHERE {predicate} LIMIT 1').fetchone():
                raise ValueError('Nonidle or unresolved lease: ' + table)
        tables = dict(REQUIRED)
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_reply_outbox'").fetchone():
            columns = {row[1] for row in con.execute('PRAGMA table_info(chat_reply_outbox)')}
            if not {'id', 'state', 'lease_token', 'lease_until'} <= columns:
                raise ValueError('Unknown reply outbox schema')
            if con.execute("SELECT 1 FROM chat_reply_outbox WHERE state='sending' OR lease_token IS NOT NULL OR lease_until>0 LIMIT 1").fetchone():
                raise ValueError('Nonidle reply outbox')
            tables['chat_reply_outbox'] = {'id'}
        output = {}
        proof_ids = {row[0] for row in con.execute(
            "SELECT id FROM jobs WHERE action_type='robie.official_install' AND idempotency_key=?",
            (proof_key,))} if proof_key else set()
        for table in tables:
            cursor = con.execute(f'SELECT * FROM {table}')
            columns = [item[0] for item in cursor.description]
            key = 'event_id' if table == 'chat_event_queue' else 'job_id' if table == 'job_intake' else 'id'
            rows = {}
            allowed_new = []
            for values in cursor:
                row = dict(zip(columns, values))
                rows[str(row[key])] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
                if ((table == 'jobs' and row['id'] in proof_ids)
                        or (table in {'checkpoints', 'job_intake', 'attempts'} and row['job_id'] in proof_ids)):
                    allowed_new.append(str(row[key]))
            output[table] = {'columns': columns, 'rows': rows, 'allowed_new_proof_rows': sorted(allowed_new)}
        return output


def verify_preserved(before, after):
    # Official install may append its own proof job/checkpoint. Every existing
    # row, including historical proof rows, must remain byte-for-byte logical.
    for table, old in before.items():
        new = after.get(table)
        if not new or old['columns'] != new['columns']:
            raise ValueError('Schema changed during stopped install: ' + table)
        if any(new['rows'].get(key) != value for key, value in old['rows'].items()):
            raise ValueError('Existing durable row changed: ' + table)
        if table in {'chat_event_queue', 'conversation_job_links', 'isolated_runs', 'chat_reply_outbox'} and old != new:
            raise ValueError('Queue/run changed during stopped install: ' + table)
        if not (set(new['rows']) - set(old['rows'])) <= set(new['allowed_new_proof_rows']):
            raise ValueError('Unexpected appended durable row: ' + table)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != 'hermes-test-01':
        raise SystemExit('Root on Test required')
    target = Path(args.snapshot)
    if not target.is_absolute() or ROOT / 'releases' not in target.parents:
        raise SystemExit('Snapshot must be under Test releases')
    units = unit_snapshot()
    receipt = hold_receipt(ROOT / 'deployments/stopped-install-hold.json', args.sha256)
    database = database_snapshot(ROOT / 'robie-job-engine/data/jobs.db',
                                 'official-install-proof:' + target.parent.name,
                                 reject_existing_proof=not args.verify)
    if args.preflight:
        if target.exists():
            raise ValueError('Stopped snapshot already exists')
        print(verify_interpreter(receipt['gateway_interpreter']))
        return
    if args.verify:
        before = json.loads(target.read_text())
        if before['units'] != units:
            raise ValueError('Persistent hold changed')
        if before['hold_receipt'] != receipt:
            raise ValueError('Approved hold receipt changed')
        verify_preserved(before['database'], database)
        print('TEST INSTALLED STOPPED: persistent masks intact; existing durable rows preserved; NOT LIVE')
    else:
        # Refuse before even replacing a previous config backup.
        if target.exists():
            raise ValueError('Stopped snapshot already exists')
        rollback = rollback_inputs(target)
        # Exclusive creation prevents a retry from silently replacing evidence.
        with target.open('x') as output:
            os.chmod(target, 0o600)
            json.dump({'units': units, 'hold_receipt': receipt, 'database': database, 'live': False,
                       'automatic_resume': False, 'rollback': rollback}, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        fsync_directory(target.parent)
        fsync_directory(target.parent.parent)


if __name__ == '__main__':
    main()
