"""Proposed root-owned host helper. Inspect only; never installs or releases a hold.

Owner installation/configuration is deliberately separate from this proposal.
Only the replay ledger/lock are written. No application imports or database reads.
"""
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys

RELEASE = '42e872f4c86fc4b4e37f859fc390f0b7c832f373'
DIGEST = '876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064'
CONFIG = Path('/etc/robie-test-operator.json')
STATE = Path('/var/lib/robie-test-operator')
ROOT = Path('/opt/streetsmart-hermes-test')
UNITS = ('robie-gateway.service', 'hermes-gateway.service',
         'robie-scheduler.timer', 'robie-scheduler.service',
         'robie-ezlynx-keepalive-test.timer', 'robie-ezlynx-keepalive-test.service',
         'hermes-email-watcher.timer', 'hermes-email-watcher.service',
         'cron.service', 'crond.service')
AUXILIARY_UNITS = ('robie-verification-audit-4246.timer', 'robie-verification-audit-4246.service', 'robie-verification-manual-renewal-4247.timer', 'robie-verification-manual-renewal-4247.service', 'robie-verification-mortgagee-4372.timer', 'robie-verification-mortgagee-4372.service')
PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlPID', 'UnitFileState')


def require(ok):
    if not ok:
        raise ValueError('OPERATOR_INSPECTION_REFUSED')


def private(path, directory=False):
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o077)
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))


def validate(request, config, now):
    require(set(request) == {'version', 'operation', 'commit', 'sha256', 'nonce',
                            'expires', 'comment_id', 'actor_id', 'issue', 'pr_id'})
    require(request['version'] == 1 and request['operation'] == 'inspect')
    require(config['enabled'] == 'INSPECT_ONLY_V1')
    require(request['commit'] == config['commit'] == RELEASE)
    require(request['sha256'] == config['sha256'] == DIGEST)
    require(type(request['actor_id']) is int and request['actor_id'] in config['actor_ids'])
    require(type(request['issue']) is int and request['issue'] == config['issue'])
    require(type(request['pr_id']) is int and request['pr_id'] == config['pr_id'])
    require(type(request['comment_id']) is int and request['comment_id'] > 0)
    require(isinstance(request['nonce'], str) and re.fullmatch('[0-9a-f]{32}', request['nonce']))
    expiry = dt.datetime.strptime(request['expires'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    require(now < expiry <= now + dt.timedelta(minutes=30))


def consume(request, state):
    # Caller holds the host lock. Partial claims stay consumed after interruption.
    for key in ('comment_id', 'nonce'):
        path = state / (key + '-' + str(request[key]))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(request, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def unit_properties(unit, output):
    values = {}
    for line in output.splitlines():
        require('=' in line)
        key, value = line.split('=', 1)
        require(key in PROPERTIES and key not in values)
        values[key] = value
    if values.get('LoadState') == 'not-found':
        require(unit != 'robie-gateway.service')
        require(values.get('ActiveState') == 'inactive' and values.get('SubState') == 'dead')
        require(values.get('UnitFileState') in (None, ''))
        require(all(values.get(key) in (None, '', '0') for key in ('MainPID', 'ControlPID')))
        # Preserve absent properties as null; do not invent successful observations.
        return {key: values.get(key) for key in PROPERTIES}
    if unit.endswith('.timer'):
        required = set(PROPERTIES) - {'MainPID', 'ControlPID'}
        require(required <= set(values))
        require(all(re.fullmatch('[A-Za-z0-9_-]{1,40}', values[key]) for key in required))
        require(all(values.get(key) in (None, '', '0') for key in ('MainPID', 'ControlPID')))
        return {key: values.get(key) for key in PROPERTIES}
    require(set(values) == set(PROPERTIES))
    require(all(re.fullmatch('[A-Za-z0-9_-]{1,40}', value) for value in values.values()))
    return values


def snapshot(runner=subprocess.run, root=ROOT):
    units = {}
    for unit in UNITS + AUXILIARY_UNITS:
        result = runner(['/usr/bin/systemctl', 'show', unit, '--no-pager', '--all',
                         '--property=' + ','.join(PROPERTIES)],
                        capture_output=True, text=True, check=True, timeout=10)
        units[unit] = unit_properties(unit, result.stdout)
    pointers = {}
    for name in ('current', 'releases/current'):
        path = root / name
        require(path.is_symlink())
        resolved = path.resolve(strict=True)
        require(resolved.is_relative_to(root / 'releases'))
        pointers[name] = str(resolved)
    return {'host': 'hermes-test-01', 'units': units, 'pointers': pointers,
            'hold_verified': False, 'installation_verified': False,
            'database_checked': False, 'external_producers_checked': False}


# Fixed, bounded diagnostic reads. No environment, command line or lock content.
STOP_FIELDS = ('ExecStop', 'ExecStopPost', 'KillMode', 'KillSignal', 'SendSIGKILL',
               'Restart', 'OnFailure', 'OnSuccess', 'FailureAction', 'SuccessAction',
               'PropagatesStopTo', 'ConsistsOf', 'BoundBy', 'JobTimeoutAction',
               'TriggeredBy', 'SendSIGHUP', 'UpheldBy', 'RequiredBy')


def stop_diagnostic(unit, runner):
    service_only = {'ExecStop', 'ExecStopPost', 'KillMode', 'KillSignal', 'SendSIGKILL', 'SendSIGHUP', 'Restart'}
    fields = tuple(f for f in STOP_FIELDS if unit.endswith('.service') or f not in service_only)
    output = runner(['/usr/bin/systemctl', 'show', unit, '--no-pager', '--all',
                     '--property=' + ','.join(fields)],
                    capture_output=True, text=True, check=True, timeout=10).stdout
    require(len(output) <= 32768)
    values = {}
    for line in output.splitlines():
        key, value = line.split('=', 1)
        require(key in fields and key not in values)
        values[key] = value
    public = {}
    for key, value in values.items():
        if key in {'ExecStop', 'ExecStopPost'}:
            public[key] = {'present': bool(value)}
        else:
            require(re.fullmatch(r'[A-Za-z0-9_.@:/ -]{0,4096}', value) is not None)
            public[key] = value
    return dict(properties=public, missing_properties=sorted(set(fields) - set(values)),
                properties_sha256=hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest())


def lock_candidates(root, proc=Path('/proc')):
    # Discover observed filenames only within the fixed Test data directory.
    # Presence/ownership is not a claim that a candidate serializes all workers.
    directory = root / 'robie-job-engine/data'
    if not directory.is_dir() or directory.is_symlink():
        return {'status': 'DATA_DIRECTORY_UNAVAILABLE', 'candidates': []}
    require(directory.resolve().is_relative_to(root.resolve()))
    paths = []
    for path in directory.iterdir():
        if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}\.lock', path.name):
            paths.append(path)
            require(len(paths) <= 64)
    observed = []
    locks = (proc / 'locks').read_text()
    require(len(locks) <= 1024 * 1024)
    namespace_matches = os.readlink(proc / 'self/ns/pid') == os.readlink(proc / '1/ns/pid')
    for path in sorted(paths):
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            observed.append(dict(path=str(path), status='NOT_REGULAR'))
            continue
        owners = []
        key = (os.major(before.st_dev), os.minor(before.st_dev), before.st_ino)
        for line in locks.splitlines():
            fields = line.split()
            if '->' in fields:
                fields.remove('->')
            require(len(fields) >= 8)
            device = fields[5].split(':')
            require(len(device) == 3)
            if (int(device[0], 16), int(device[1], 16), int(device[2])) == key:
                owners.append(dict(type=fields[1], pid=fields[4], access=fields[3]))
        after = path.lstat()
        require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino))
        observed.append(dict(path=str(path), device=before.st_dev, inode=before.st_ino, owners=owners))
    return dict(status='OBSERVED', pid_namespace_matches_init=namespace_matches, candidates=observed)


def prerequisites(snapshot, runner=subprocess.run, root=ROOT, proc=Path('/proc')):
    stops = {unit: stop_diagnostic(unit, runner) for unit in UNITS
             if snapshot['units'][unit]['LoadState'] != 'not-found'}
    cron = {}
    for unit in ('cron.service', 'crond.service'):
        pid = snapshot['units'][unit].get('MainPID')
        if pid is None or pid == '0':
            cron[unit] = {'status': 'NO_RUNNING_EXECUTABLE_OBSERVED'}
            continue
        require(re.fullmatch('[1-9][0-9]{0,9}', pid) is not None)
        directory = proc / pid
        before = (directory / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        executable = (directory / 'exe').resolve(strict=True)
        require(str(executable).startswith(('/usr/bin/', '/usr/sbin/', '/bin/', '/sbin/')))
        info = executable.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 32 * 1024 * 1024)
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        after = (directory / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        require(before == after and (directory / 'exe').resolve(strict=True) == executable)
        cron[unit] = dict(status='OBSERVED', pid=pid, start_time=before, executable=str(executable), sha256=digest)
    return dict(stop_diagnostics=stops, cron_executables=cron, worker_lock_observations=lock_candidates(root, proc),
                worker_lock_contract_verified=False, cron_handler_semantics_verified=False)


def main():
    require(sys.argv[1:] == ['inspect'])
    require(os.geteuid() == 0 and socket.gethostname().split('.')[0] == 'hermes-test-01')
    private(CONFIG)
    private(STATE, directory=True)
    request = json.loads(sys.stdin.buffer.read(4097))
    require(len(json.dumps(request)) <= 4096)
    config = json.loads(CONFIG.read_text())
    validate(request, config, dt.datetime.now(dt.timezone.utc))
    fd = os.open(STATE / 'operator.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        consume(request, STATE)
        result = snapshot()
        result.update(prerequisites(result))
        result['helper_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('OPERATOR_INSPECTION_REFUSED') from None
