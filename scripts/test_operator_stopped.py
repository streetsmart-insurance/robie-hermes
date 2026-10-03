"""Proposed privileged Test-only stopped operator; no running-gateway drain/resume.

Trusted bootstrap installs this file and supplies root-owned approval and the
exact original TGZ. Issue commands request operations; they never grant approval.
No installer output, database rows, process arguments or secrets are published.
"""
import datetime as dt
import fcntl
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tarfile
import types
import time

COMMIT = '42e872f4c86fc4b4e37f859fc390f0b7c832f373'
DIGEST = '876dc38f2e53ab49771888fc710fe222b6384f7dce7b38be146190d4ad25a064'
SHORT = COMMIT[:12]
ROOT_UID = 0
ROOT = Path('/opt/streetsmart-hermes-test')
STATE = Path('/var/lib/robie-test-operator')
APPROVAL = Path('/etc/robie-test-stopped-approval.json')
UNIT_DIR = Path('/etc/systemd/system')
BACKUP_DIR = UNIT_DIR / ('.robie-stopped-' + SHORT)
PACKAGE = STATE / 'approved' / COMMIT
ARCHIVE = PACKAGE / ('robie-hermes-' + SHORT + '.tgz')
ATTEMPT = STATE / ('stopped-' + SHORT)
RECEIPT = ROOT / 'deployments/stopped-install-hold.json'
RELEASE = ROOT / 'releases' / SHORT / ('robie-hermes-' + SHORT)
SNAPSHOT = RELEASE.parent / 'stopped-install-before.json'
DB = ROOT / 'robie-job-engine/data/jobs.db'
KEY = 'official-install-proof:' + SHORT
UNITS = ('robie-gateway.service', 'hermes-gateway.service',
         'robie-scheduler.timer', 'robie-scheduler.service',
         'robie-ezlynx-keepalive-test.timer', 'robie-ezlynx-keepalive-test.service',
         'hermes-email-watcher.timer', 'hermes-email-watcher.service',
         'cron.service', 'crond.service')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC',
       'HOME': '/root', 'PYTHONDONTWRITEBYTECODE': '1'}


def need(condition, code='REFUSED'):
    if not condition:
        raise ValueError(code)


def stamp(value):
    need(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value))
    return dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)


def protected(path, directory=False, private=True):
    """Require root-owned, unredirected paths; no writable ancestors."""
    info = path.lstat()
    need(info.st_uid == ROOT_UID and not info.st_mode & (0o077 if private else 0o022))
    need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    for parent in path.parents:
        item = parent.lstat()
        need(stat.S_ISDIR(item.st_mode) and item.st_uid == 0 and not item.st_mode & 0o022)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_dir(path.parent)


def save(path, data):
    exclusive(path, json.dumps(data, sort_keys=True).encode() + b'\n')


def read_private(path):
    protected(path)
    return json.loads(path.read_text())


def validate_approval(approval, request, now):
    need(approval['version'] == 1 and approval['enabled'] == 'STOPPED_OPERATOR_V1')
    need(approval['host'] == 'hermes-test-01')
    need(approval['commit'] == request['commit'] == COMMIT)
    need(approval['sha256'] == request['sha256'] == DIGEST)
    need(request['operation'] in {'prepare-hold', 'hold', 'install', 'verify'})
    need(set(approval['operations']) <= {'prepare-hold', 'hold', 'install', 'verify'})
    need(request['operation'] in approval['operations'])
    need(type(request['actor_id']) is int and request['actor_id'] in approval['actor_ids'])
    need(type(request['issue']) is int and request['issue'] == approval['issue'])
    need(stamp(approval['not_before']) <= now < stamp(approval['expires']))
    need(stamp(approval['expires']) - stamp(approval['not_before']) <= dt.timedelta(hours=24))
    need(now < stamp(request['expires']) <= now + dt.timedelta(minutes=30))
    need(type(request['pr_id']) is int and request['pr_id'] == approval['pr_id'])
    need(type(request['comment_id']) is int and request['comment_id'] > 0)
    need(isinstance(request['nonce'], str) and re.fullmatch('[0-9a-f]{32}', request['nonce']))
    need(set(request) == {'version', 'operation', 'commit', 'sha256', 'nonce',
                         'expires', 'comment_id', 'actor_id', 'issue', 'pr_id'} and request['version'] == 1)
    for key in ('approved_outage_reference', 'external_fence_reference',
                'drain_evidence_reference', 'operator_handoff_reference'):
        need(isinstance(approval[key], str) and 1 <= len(approval[key]) <= 512)
        need(approval[key].strip() == approval[key] and '\n' not in approval[key])
    need(approval['external_producers_fenced'] is True)
    need(approval['manual_runners_fenced'] is True)
    if request['operation'] == 'prepare-hold':
        need(approval['prepare_hold_authorized'] is True)
        preparation_contract(approval)
    else:
        need(approval['workers_drained'] is True or (request['operation'] in {'install', 'verify'}
             and approval.get('prepare_hold_authorized') is True))
    need(approval['exclusive_operator_handoff'] is True)
    need(approval['gateway_already_inactive'] is True)
    need(approval['cron_outage_included'] is True)
    need(approval['no_resume'] is True)
    need(isinstance(approval['expected_prior_target'], str))


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          env=ENV, timeout=30, **kwargs).stdout


def archive_bytes():
    protected(STATE, directory=True)
    protected(PACKAGE, directory=True)
    protected(ARCHIVE)
    need(ARCHIVE.stat().st_size <= 32 * 1024 * 1024)
    data = ARCHIVE.read_bytes()
    need(hashlib.sha256(data).hexdigest() == DIGEST, 'ARCHIVE_MISMATCH')
    return data


def members(data):
    output = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        total = 0
        for member in archive:
            path = Path(member.name)
            need(not path.is_absolute() and '..' not in path.parts)
            need(path.parts[0] == 'robie-hermes-' + SHORT)
            need(member.isdir() or member.isfile())
            if member.isfile():
                total += member.size
                need(total <= 128 * 1024 * 1024 and member.name not in output)
                output[member.name] = archive.extractfile(member).read()
    return output


def candidate_module(files, relative, name):
    # Execute only a selected helper from the SHA256-verified approved artifact,
    # never code from a floating installed release or from the controller checkout.
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(files['robie-hermes-' + SHORT + '/' + relative], relative, 'exec'), module.__dict__)
    return module


def unit_state(unit):
    fields = 'LoadState,ActiveState,SubState,UnitFileState,MainPID,ControlPID,ControlGroup'
    text = command(['/usr/bin/systemctl', 'show', unit, '--all', '--no-pager', '--property=' + fields])
    values = {}
    for line in text.splitlines():
        key, value = line.split('=', 1)
        need(key in fields.split(',') and key not in values)
        values[key] = value
    need({'LoadState', 'ActiveState', 'SubState'} <= values.keys())
    if values['LoadState'] == 'not-found':
        values.setdefault('UnitFileState', '')
    need('UnitFileState' in values)
    return values


def no_workers(state):
    need(all(state.get(key, '') in ('', '0') for key in ('MainPID', 'ControlPID')))
    need(not cgroup_members(state), 'WORKERS_REMAIN')


def cgroup_members(state):
    group = state.get('ControlGroup', '')
    if not group:
        return set()
    need(group.startswith('/') and '..' not in Path(group).parts)
    root = Path('/sys/fs/cgroup') / group.lstrip('/')
    need(root.resolve().is_relative_to(Path('/sys/fs/cgroup')))
    need(root.is_dir(), 'CGROUP_UNAVAILABLE')
    count = 0
    members = set()
    for directory, children, names in os.walk(root, followlinks=False):
        count += 1
        need(count <= 512)
        need(all(not (Path(directory) / name).is_symlink() for name in children))
        need('cgroup.procs' in names, 'CGROUP_UNAVAILABLE')
        values = (Path(directory) / 'cgroup.procs').read_text().split()
        need(all(value.isdecimal() for value in values), 'CGROUP_UNAVAILABLE')
        members.update(values)
    return members


# Local source fragment; installed only by a separately approved bootstrap.
STOP_FIELDS = ('ExecStop', 'ExecStopPost', 'KillMode', 'KillSignal', 'SendSIGKILL',
               'Restart', 'OnFailure', 'OnSuccess', 'FailureAction', 'SuccessAction',
               'PropagatesStopTo', 'ConsistsOf', 'BoundBy', 'JobTimeoutAction', 'TriggeredBy', 'SendSIGHUP', 'UpheldBy', 'RequiredBy')


def preparation_contract(approval):
    units = approval['auxiliary_units']
    need(isinstance(units, list) and len(set(units)) == len(units) == 6, 'AUXILIARY_CONTRACT_REQUIRED')
    need(all(isinstance(u, str) and re.fullmatch(r'[A-Za-z0-9_-]+\.(service|timer)', u)
             and u not in UNITS and 'browser' not in u for u in units), 'AUXILIARY_CONTRACT_REQUIRED')
    lock = approval['worker_lock']
    need(lock['type'] == 'flock' and lock['initial_host_pid_namespace'] is True,
         'WORKER_LOCK_CONTRACT_REQUIRED')
    path = Path(lock['path'])
    need(path.is_absolute() and path.is_relative_to(ROOT) and '..' not in path.parts,
         'WORKER_LOCK_CONTRACT_REQUIRED')
    need(isinstance(lock['contract_reference'], str) and bool(lock['contract_reference'].strip()))
    need(type(lock['device']) is int and type(lock['inode']) is int and lock['inode'] > 0)
    need(type(approval['drain_timeout_seconds']) is int and 1 <= approval['drain_timeout_seconds'] <= 600)
    need(isinstance(approval['safe_stop'], dict))


def process_inventory():
    # No command lines, environments, browser state or client data. Root-private only.
    need(os.readlink('/proc/self/ns/pid') == os.readlink('/proc/1/ns/pid'), 'PID_NAMESPACE_MISMATCH')
    result = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            try:
                executable = os.readlink(path / 'exe')
            except FileNotFoundError:
                executable = None
            result[path.name] = dict(parent=fields[1], start=fields[19], executable=executable,
                                     cgroup=(path / 'cgroup').read_text())
        except FileNotFoundError:
            continue  # process ended naturally; other failures are not hidden
    return result


def worker_lock_observation(approval):
    contract = approval['worker_lock']
    path = Path(contract['path'])
    protected(path, private=False)
    before = path.stat()
    need((before.st_dev, before.st_ino) == (contract['device'], contract['inode']), 'WORKER_LOCK_CHANGED')
    need(os.readlink('/proc/self/ns/pid') == os.readlink('/proc/1/ns/pid'), 'PID_NAMESPACE_MISMATCH')
    key = (os.major(before.st_dev), os.minor(before.st_dev), before.st_ino)
    owners = []
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if '->' in fields:
            fields.remove('->')
        need(len(fields) >= 8, 'LOCK_INVENTORY_UNKNOWN')
        device = fields[5].split(':')
        need(len(device) == 3, 'LOCK_INVENTORY_UNKNOWN')
        current = (int(device[0], 16), int(device[1], 16), int(device[2]))
        if current == key:
            need(fields[1] == 'FLOCK', 'LOCK_TYPE_CHANGED')
            owners.append(fields[4])
    after = path.stat()
    need((after.st_dev, after.st_ino) == key_identity(before), 'WORKER_LOCK_CHANGED')
    return {'device': before.st_dev, 'inode': before.st_ino, 'owners': owners}


def key_identity(info):
    return info.st_dev, info.st_ino


def auxiliary_snapshot(approval):
    result = {unit: unit_state(unit) for unit in approval['auxiliary_units']}
    for state in result.values():
        need(state['ActiveState'] in {'inactive', 'failed'}, 'AUXILIARY_NOT_STOPPED')
        no_workers(state)
    return result


def stop_fields(unit):
    service_only = {'ExecStop', 'ExecStopPost', 'KillMode', 'KillSignal', 'SendSIGKILL', 'SendSIGHUP', 'Restart'}
    return tuple(field for field in STOP_FIELDS if unit.endswith('.service') or field not in service_only)


def stop_properties(unit):
    fields = stop_fields(unit)
    raw = command(['/usr/bin/systemctl', 'show', unit, '--all', '--no-pager',
                   '--property=' + ','.join(fields)])
    values = dict(line.split('=', 1) for line in raw.splitlines())
    need(set(values) == set(fields), 'UNKNOWN_STOP_SEMANTICS')
    return values


def safe_stop_contract(unit, approval, state, normalize=False):
    contract = approval['safe_stop'][unit]
    need(contract['reviewed_handler_never_signals_children'] is True and
         contract['children_cannot_escape_cgroup'] is True, 'UNKNOWN_STOP_SEMANTICS')
    need(isinstance(contract['source_review_reference'], str) and contract['source_review_reference'].strip(),
         'UNKNOWN_STOP_SEMANTICS')
    values = stop_properties(unit)
    if normalize:
        need(state['ActiveState'] == 'failed', 'NORMALIZATION_STATE_CHANGED')
        no_workers(state)
    else:
        need(hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest() ==
             contract['properties_sha256'], 'STOP_PROPERTIES_CHANGED')
    for field in ('ExecStop', 'ExecStopPost', 'OnFailure', 'OnSuccess',
                  'PropagatesStopTo', 'ConsistsOf', 'BoundBy', 'TriggeredBy', 'UpheldBy', 'RequiredBy'):
        if field in values:
            need(values[field] == '', 'UNSAFE_STOP_SEMANTICS')
    for field in ('FailureAction', 'SuccessAction', 'JobTimeoutAction'):
        need(values[field] == 'none', 'UNSAFE_STOP_SEMANTICS')
    if unit.endswith('.service') and state['ActiveState'] == 'active':
        need(values['SendSIGHUP'] == 'no', 'UNSAFE_STOP_SEMANTICS')
        need(values['KillMode'] == 'process' and values['KillSignal'] in {'15', 'SIGTERM'}
             and values['SendSIGKILL'] == 'no' and values['Restart'] == 'no', 'UNSAFE_STOP_SEMANTICS')
    pid = state.get('MainPID', '0')
    need(state.get('ControlPID', '0') == '0', 'CONTROL_PROCESS_REMAINS')
    if pid != '0':
        need(unit in ('cron.service', 'crond.service'), 'BUSINESS_PROCESS_STOP_REFUSED')
        executable = Path('/proc') / pid / 'exe'
        need(str(executable.resolve(strict=True)) == contract['executable'], 'CRON_EXECUTABLE_CHANGED')
        need(hashlib.sha256(executable.read_bytes()).hexdigest() == contract['executable_sha256'],
             'CRON_EXECUTABLE_CHANGED')
    return values


def idle_database(guard):
    try:
        return guard.database_snapshot(DB, KEY, reject_existing_proof=True)
    except ValueError as error:
        # Wait only for known activity; unknown schema/state must fail immediately.
        need(str(error) in {'Nonidle or unresolved lease: jobs',
             'Nonidle or unresolved lease: chat_event_queue',
             'Nonidle or unresolved lease: isolated_runs', 'Nonidle reply outbox'},
             'DATABASE_GUARD_REFUSED')
        return None


def prepare_launchers(approval):
    deadline = time.monotonic() + approval['drain_timeout_seconds']
    # Journal exists; retain effective original settings until launcher stops.
    for unit in UNITS:
        state = unit_state(unit)
        if unit.endswith('.timer') and state['ActiveState'] == 'active':
            safe_stop_contract(unit, approval, state)
            command(['/usr/bin/systemctl', 'stop', unit])
    for unit in ('cron.service', 'crond.service'):
        while unit_state(unit)['ActiveState'] == 'active':
            state = unit_state(unit)
            safe_stop_contract(unit, approval, state)
            inventory = process_inventory()
            # Cron may fork between this observation and stop. Its reviewed handler,
            # process-only kill policy and disabled escalation must preserve that child.
            group = state.get('ControlGroup', '')
            need(group.startswith('/') and '..' not in Path(group).parts, 'CGROUP_UNAVAILABLE')
            children = [pid for pid, item in inventory.items()
                        if item['parent'] == state['MainPID'] or
                        (pid != state['MainPID'] and any(
                            (cg := line.split(':', 2)[-1]) == group or cg.startswith(group + '/')
                            for line in item['cgroup'].splitlines()))]
            children.extend(cgroup_members(state) - {state['MainPID']})
            if not children:
                need(unit_state(unit) == state, 'CRON_PROCESS_CHANGED')
                refreshed = process_inventory()
                need(state['MainPID'] in inventory and refreshed.get(state['MainPID']) ==
                     inventory[state['MainPID']], 'CRON_PROCESS_CHANGED')
                need(cgroup_members(state) == {state['MainPID']}, 'CRON_PROCESS_CHANGED')
                command(['/usr/bin/systemctl', 'stop', unit])
                break
            need(time.monotonic() < deadline, 'DRAIN_TIMEOUT_REQUIRES_REVIEW')
            time.sleep(1)


def prepare_drain(approval, guard, states):
    deadline = time.monotonic() + approval['drain_timeout_seconds']
    while True:
        busy = False
        for unit in UNITS:
            state = unit_state(unit)
            need(state['ActiveState'] in {'active', 'inactive', 'failed'}, 'UNKNOWN_DRAIN_STATE')
            if state['ActiveState'] == 'active':
                need(unit not in ('robie-gateway.service', 'hermes-gateway.service', 'cron.service', 'crond.service')
                     and not unit.endswith('.timer'), 'LAUNCHER_REACTIVATED')
                busy = True
            else:
                # A nonempty cgroup is not safe to normalize, even if systemd says failed.
                try:
                    no_workers(state)
                except ValueError as error:
                    if str(error) != 'WORKERS_REMAIN':
                        raise
                    busy = True
        auxiliary_snapshot(approval)
        lock = worker_lock_observation(approval)
        database = idle_database(guard)
        if not busy and not lock['owners'] and database is not None:
            break
        need(time.monotonic() < deadline, 'DRAIN_TIMEOUT_REQUIRES_REVIEW')
        time.sleep(1)
    for unit in UNITS:
        state = unit_state(unit)
        if state['ActiveState'] == 'failed':
            no_workers(state)
            safe_stop_contract(unit, approval, state, normalize=True)
            command(['/usr/bin/systemctl', 'stop', unit])
    return database


def prior_units(states, preparing=False):
    prior = {}
    for unit, state in states.items():
        missing = state['LoadState'] == 'not-found'
        if missing:
            need(unit != 'robie-gateway.service' and state['ActiveState'] == 'inactive')
            no_workers(state)
        elif unit.endswith('.timer'):
            need(state['ActiveState'] in {'active', 'inactive'})
        elif preparing:
            need(state['ActiveState'] in {'inactive', 'failed', 'active'})
            if unit in ('robie-gateway.service', 'hermes-gateway.service'):
                need(state['ActiveState'] in {'inactive', 'failed'}, 'RUNNING_GATEWAY_REFUSED')
                no_workers(state)
        else:
            need(state['ActiveState'] == 'inactive' and state['SubState'] == 'dead', 'DRAIN_REQUIRED')
            no_workers(state)
        path = UNIT_DIR / unit
        masked = path.is_symlink() and os.readlink(path) == '/dev/null'
        if state['LoadState'] == 'masked':
            need(masked and state['UnitFileState'] == 'masked', 'RUNTIME_MASK_REFUSED')
        need(missing or state['UnitFileState'] in {'enabled', 'disabled', 'static', 'indirect', 'masked', 'enabled-runtime'})
        prior[unit] = dict(active='not-found' if missing else state['ActiveState'],
                           enabled='not-found' if missing else state['UnitFileState'],
                           mask_preexisting=masked,
                           unit_backup=str(BACKUP_DIR / unit) if not masked and (path.exists() or path.is_symlink()) else '')
    return prior


def unit_file(path):
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    need(info.st_uid == ROOT_UID and (stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode)))
    need(stat.S_ISLNK(info.st_mode) or not info.st_mode & 0o022)
    return dict(mode=info.st_mode, uid=info.st_uid, gid=info.st_gid,
                device=info.st_dev, inode=info.st_ino,
                target=os.readlink(path) if path.is_symlink() else None,
                sha256=None if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest())


def pointers(expected):
    need(ROOT.is_dir() and ROOT.resolve() == ROOT)
    for path in (ROOT / 'current', ROOT / 'releases/current'):
        need(path.is_symlink() and path.resolve(strict=True) == Path(expected))
    need(Path(expected).is_relative_to(ROOT / 'releases'))


def tree_digest(root):
    """Hash bounded release/skill files without exposing their contents."""
    need(root.is_dir() and not root.is_symlink())
    digest = hashlib.sha256()
    root_info = root.stat()
    digest.update(json.dumps(['.', root_info.st_mode, root_info.st_uid, root_info.st_gid, None]).encode() + b'\n')
    count = total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            count += 1
            need(count <= 50000, 'TREE_TOO_LARGE')
            info = path.lstat()
            content = None
            if path.is_symlink():
                content = os.readlink(path)
            elif path.is_file():
                file_hash = hashlib.sha256()
                with path.open('rb') as handle:
                    while block := handle.read(65536):
                        total += len(block)
                        need(total <= 2 * 1024 ** 3, 'TREE_TOO_LARGE')
                        file_hash.update(block)
                content = file_hash.hexdigest()
            else:
                need(path.is_dir())
            digest.update(json.dumps([str(path.relative_to(root)), info.st_mode,
                                      info.st_uid, info.st_gid, content]).encode() + b'\n')
        dirs.sort()
    return digest.hexdigest()


def driver_digest():
    # Fixed non-secret metadata attribute only; never request an access token.
    connection = http.client.HTTPConnection('169.254.169.254', timeout=5)
    try:
        connection.request('GET', '/computeMetadata/v1/project/attributes/robie-ezlynx-driver',
                           headers={'Metadata-Flavor': 'Google'})
        response = connection.getresponse()
        need(response.status in (200, 404), 'DRIVER_READ_FAILED')
        data = response.read(4097)
        need(len(data) <= 4096)
        return hashlib.sha256(str(response.status).encode() + b'\0' + data).hexdigest()
    finally:
        connection.close()


def preserved_state(prior):
    browser = command(['/usr/bin/systemctl', 'show', 'robie-ezlynx-browser.service',
                       '--all', '--no-pager', '--property=LoadState,ActiveState,SubState,MainPID,ControlPID,ActiveEnterTimestampMonotonic,ExecMainStartTimestampMonotonic'])
    skill = ROOT / '.hermes/skills/ezlynx-policy-setup'
    if skill.exists() or skill.is_symlink():
        resolved = skill.resolve(strict=True)
        skill_state = {'link': os.readlink(skill) if skill.is_symlink() else None,
                       'resolved': str(resolved), 'sha256': tree_digest(resolved)}
    else:
        skill_state = None
    return {'prior_release': tree_digest(Path(prior)), 'policy_skill': skill_state,
            'browser_state_sha256': hashlib.sha256(browser.encode()).hexdigest(),
            'driver_sha256': driver_digest()}


def existing_hold(guard, approval_hash):
    saved = read_private(ATTEMPT / 'before.json')
    need(saved['approval_sha256'] == approval_hash, 'APPROVAL_CHANGED')
    need(preserved_state(saved['approval']['expected_prior_target']) == saved['preserved'], 'PRESERVED_STATE_CHANGED')
    receipt = guard.hold_receipt(RECEIPT, DIGEST)
    need(receipt == saved['receipt'], 'RECEIPT_CHANGED')
    for unit, prior in receipt['prior_units'].items():
        if prior['unit_backup']:
            need(unit_file(Path(prior['unit_backup'])) == saved['unit_files'][unit], 'UNIT_BACKUP_CHANGED')
    if saved['approval'].get('prepare_hold_authorized'):
        preparation_contract(saved['approval'])
        auxiliary_snapshot(saved['approval'])
        need(not worker_lock_observation(saved['approval'])['owners'], 'WORKER_LOCK_HELD')
    units = guard.unit_snapshot()
    for unit in UNITS:
        no_workers(unit_state(unit))
    return saved, units


def hold(approval, approval_hash, guard, preparing=False):
    if preparing:
        preparation_contract(approval)
    # Never replace another operator's receipt, interrupted attempt, or release.
    need(not RECEIPT.exists() and not RECEIPT.is_symlink(), 'EXISTING_HOLD_REQUIRES_REVIEW')
    need(not SNAPSHOT.exists() and not RELEASE.parent.exists(), 'EXISTING_RELEASE_REQUIRES_REVIEW')
    need(not ATTEMPT.exists() and not BACKUP_DIR.exists(), 'ATTEMPT_REQUIRES_REVIEW')
    protected(UNIT_DIR, directory=True, private=False)
    protected(RECEIPT.parent, directory=True, private=False)
    pointers(approval['expected_prior_target'])
    states = {unit: unit_state(unit) for unit in UNITS}
    prior = prior_units(states, preparing=preparing)
    unit_files = {unit: unit_file(UNIT_DIR / unit) for unit in UNITS}
    need(not prior['robie-gateway.service']['mask_preexisting'], 'ORIGINAL_INTERPRETER_REQUIRED')
    invocation = command(['/usr/bin/systemctl', 'show', 'robie-gateway.service', '--no-pager', '-p', 'ExecStart', '--value'])
    matches = re.findall(r'path=([^ ;}]+)', invocation)
    need(len(matches) == 1, 'ORIGINAL_INTERPRETER_REQUIRED')
    identity = guard.interpreter_identity(matches[0])
    database = idle_database(guard) if preparing else guard.database_snapshot(DB, KEY, reject_existing_proof=True)
    preparation = {}
    if preparing:
        preparation = dict(processes=process_inventory(), auxiliary=auxiliary_snapshot(approval),
                           worker_lock=worker_lock_observation(approval))
        for unit, state in states.items():
            if state['ActiveState'] == 'failed' or (state['ActiveState'] == 'active' and
                    (unit.endswith('.timer') or unit in ('cron.service', 'crond.service'))):
                safe_stop_contract(unit, approval, state)
    preserved = preserved_state(approval['expected_prior_target'])
    receipt = dict(version=1, host='hermes-test-01', release_sha256=DIGEST,
                   gateway_interpreter=identity, external_producers_fenced=True,
                   approved_outage_reference=approval['approved_outage_reference'], prior_units=prior)
    ATTEMPT.mkdir(mode=0o700)
    fsync_dir(ATTEMPT.parent)
    journal = dict(approval_sha256=approval_hash, approval=approval,
                   receipt=receipt, unit_states=states, database=database,
                   unit_files=unit_files, preserved=preserved, preparation=preparation)
    save(ATTEMPT / ('prepare-before.json' if preparing else 'before.json'), journal)
    BACKUP_DIR.mkdir(mode=0o700)
    fsync_dir(BACKUP_DIR.parent)
    if preparing:
        originals = ATTEMPT / 'original-unit-files'
        originals.mkdir(mode=0o700)
        fsync_dir(ATTEMPT)
        for unit, metadata in unit_files.items():
            if metadata is not None:
                path = UNIT_DIR / unit
                content = metadata['target'].encode() if metadata['target'] is not None else path.read_bytes()
                exclusive(originals / unit, content)
                need(unit_file(path) == metadata, 'UNIT_FILE_CHANGED')
        prepare_launchers(approval)
    # Legacy hold stops only timers; prepare-hold has stopped verified launchers.
    for unit in UNITS:
        if not preparing and unit.endswith('.timer') and states[unit]['ActiveState'] == 'active':
            command(['/usr/bin/systemctl', 'stop', unit])
    for unit in UNITS:
        item = prior[unit]
        if item['active'] == 'not-found' or item['mask_preexisting']:
            continue
        live = unit_state(unit)
        if not preparing:
            need(live['ActiveState'] == 'inactive' and live['SubState'] == 'dead', 'DRAIN_REQUIRED')
            no_workers(live)
        path = UNIT_DIR / unit
        need(unit_file(path) == unit_files[unit], 'UNIT_FILE_CHANGED')
        if item['unit_backup']:
            need(path.exists() or path.is_symlink())
            os.rename(path, Path(item['unit_backup']))
            fsync_dir(BACKUP_DIR)
            fsync_dir(UNIT_DIR)
        else:
            need(not path.exists() and not path.is_symlink())
        os.symlink('/dev/null', path)
        fsync_dir(UNIT_DIR)
    command(['/usr/bin/systemctl', 'daemon-reload'])
    if preparing:
        database = prepare_drain(approval, guard, states)
        journal['database'] = database
        journal['drained_processes'] = process_inventory()
        need(preserved_state(approval['expected_prior_target']) == preserved, 'PRESERVED_STATE_CHANGED')
        save(ATTEMPT / 'before.json', journal)
    guard.unit_snapshot()
    need(guard.database_snapshot(DB, KEY, reject_existing_proof=True) == database, 'DURABLE_STATE_CHANGED')
    guard.verify_interpreter(identity)
    protected(RECEIPT.parent, directory=True, private=False)
    save(RECEIPT, receipt)
    existing_hold(guard, approval_hash)
    save(ATTEMPT / 'hold-complete.json', {'sha256': DIGEST, 'live': False})
    return {'status': 'HOLD ESTABLISHED', 'live': False}


def verify(guard, files, approval_hash):
    saved, units = existing_hold(guard, approval_hash)
    before = read_private(SNAPSHOT)
    need(before['hold_receipt'] == saved['receipt'] and before['units'] == units)
    after = guard.database_snapshot(DB, KEY)
    guard.verify_preserved(saved['database'], after)
    guard.verify_preserved(before['database'], after)
    rollback = before['rollback']
    need(rollback['old_current'] == rollback['old_releases_current'] == saved['approval']['expected_prior_target'])
    need(Path(rollback['old_current']).is_dir(), 'ROLLBACK_TARGET_MISSING')
    need(rollback['restart_gateway'] is False and rollback['restore_policy_skill'] is False)
    dropin = rollback['runtime_dropin']
    if dropin['state'] == 'present':
        need(hashlib.sha256(Path(dropin['snapshot']).read_bytes()).hexdigest() == dropin['sha256'])
    else:
        need(dropin['state'] == 'absent')
    pointers(str(RELEASE))
    for name, content in files.items():
        relative = Path(name).relative_to('robie-hermes-' + SHORT)
        path = RELEASE / relative
        need(path.is_file() and not path.is_symlink())
        need(hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(content).digest(), 'SOURCE_MISMATCH')
    need((RELEASE / '.release-sha256').read_text().strip() == DIGEST)
    truth = candidate_module(files, 'robie_job_engine/deploy_truth.py', 'approved_deploy_truth')
    for item in truth.CHAT_RUNTIME_FILES:
        path = ROOT / '.hermes' / item.dest_relpath
        need(path.is_file() and not path.is_symlink())
        need(path.read_bytes() == truth.zip_load_shim_source(item.zip_relpath).encode(), 'OVERLAY_MISMATCH')
    expected = '[Service]\nEnvironment="PYTHONPATH=' + str(ROOT / 'releases/current/.gateway-runtime') + ':' + str(ROOT / 'releases/current') + ':' + str(ROOT / '.hermes/hermes-agent') + '"\n'
    need(guard.RUNTIME_DROPIN.read_text() == expected, 'RUNTIME_DROPIN_MISMATCH')
    runtime = RELEASE / '.gateway-runtime'
    runtime_hash = tree_digest(runtime)
    completed = ATTEMPT / 'install-complete.json'
    if completed.exists():
        need(read_private(completed)['runtime_sha256'] == runtime_hash, 'RUNTIME_CHANGED')
    identity = guard.verify_interpreter(saved['receipt']['gateway_interpreter'])
    # Import only dependencies, never the gateway or business application. Confirm
    # each comes from this release's target directory rather than fallback packages.
    smoke = ('import pathlib,sys; root=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(root)); '
             'import google.auth.credentials,google.cloud.secretmanager,googleapiclient.discovery,playwright.sync_api; '
             'modules=(google.auth.credentials,google.cloud.secretmanager,googleapiclient.discovery,playwright.sync_api); '
             'assert all(pathlib.Path(m.__file__).resolve().is_relative_to(root) for m in modules)')
    command([identity, '-I', '-B', '-c', smoke, str(runtime)])
    return {'status': 'TEST INSTALLED STOPPED', 'commit': COMMIT, 'sha256': DIGEST,
            'live': False, 'independent_qa': False, 'durable_rows_preserved': True,
            'persistent_masks': True, 'rollback_preserved': True, 'source_and_overlays_match': True,
            'runtime_imports_verified': True, 'runtime_sha256': runtime_hash,
            'browser_driver_policy_unchanged': True}


def install(guard, files, approval_hash):
    saved, _ = existing_hold(guard, approval_hash)
    pointers(saved['approval']['expected_prior_target'])
    need(read_private(ATTEMPT / 'hold-complete.json')['sha256'] == DIGEST)
    need(not SNAPSHOT.exists() and not RELEASE.parent.exists(), 'ATTEMPT_REQUIRES_REVIEW')
    need(guard.database_snapshot(DB, KEY, reject_existing_proof=True) == saved['database'])
    # An exclusive durable claim prevents rerunning a partially executed install.
    save(ATTEMPT / 'install-started.json', {'sha256': DIGEST})
    checksum = PACKAGE / (ARCHIVE.name + '.sha256')
    exclusive(checksum, (DIGEST + '  ' + ARCHIVE.name + '\n').encode())
    installer = PACKAGE / 'deploy-test-release.sh'
    exclusive(installer, files['robie-hermes-' + SHORT + '/scripts/deploy-test-release.sh'])
    log = ATTEMPT / 'installer.log'
    # Keep operational inventory/logs root-private on the host, never Actions output.
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        result = subprocess.run(['/bin/bash', str(installer), '--archive', str(ARCHIVE),
                                 '--checksum', str(checksum), '--commit', COMMIT,
                                 '--skip-policy-setup', '--keep-stopped'],
                                env=ENV, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                check=False, pass_fds=(LOCK_FD,))
        output.flush()
        os.fsync(output.fileno())
    need(result.returncode == 0, 'INSTALL_FAILED_REQUIRES_REVIEW')
    result = verify(guard, files, approval_hash)
    save(ATTEMPT / 'install-complete.json', result)
    return result


def main():
    global LOCK_FD
    need(sys.argv[1:] == ['operate'])
    need(os.geteuid() == 0 and socket.gethostname().split('.')[0] == 'hermes-test-01')
    protected(STATE, directory=True)
    protected(APPROVAL)
    raw = sys.stdin.buffer.read(4097)
    need(len(raw) <= 4096)
    request = json.loads(raw)
    approval_raw = APPROVAL.read_bytes()
    approval = json.loads(approval_raw)
    need(approval['helper_sha256'] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'HELPER_APPROVAL_MISMATCH')
    validate_approval(approval, request, dt.datetime.now(dt.timezone.utc))
    approval_hash = hashlib.sha256(approval_raw).hexdigest()
    LOCK_FD = os.open(STATE / 'operator.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(LOCK_FD, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for field in ('comment_id', 'nonce'):
            save(STATE / (field + '-' + str(request[field])), request)
        files = members(archive_bytes())
        guard = candidate_module(files, 'scripts/test_stopped_install.py', 'approved_stopped_guard')
        need(tuple(guard.UNITS) == UNITS)
        if request['operation'] in {'prepare-hold', 'hold'}:
            result = hold(approval, approval_hash, guard, preparing=request['operation'] == 'prepare-hold')
        elif request['operation'] == 'install':
            result = install(guard, files, approval_hash)
        else:
            result = verify(guard, files, approval_hash)
        result.update(host='hermes-test-01', commit=COMMIT, sha256=DIGEST)
        result['helper_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('TEST_STOPPED_OPERATOR_REFUSED: inspect root-private attempt evidence; no automatic retry or resume') from None
