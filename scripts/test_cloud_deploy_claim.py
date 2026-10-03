"""One fixed Test execution: claim, bounded staging, existing installer under lock.

Expiry authorizes starts, never kills an in-flight installation or rollback.
No terminal result means UNKNOWN; preserve receipts and reconcile without retry.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import runpy

REMOTE = r'''
import datetime as dt, fcntl, hashlib, json, os, re, signal, socket, stat, subprocess, time
from pathlib import Path

def need(ok):
    if not ok:
        raise ValueError('TEST_DEPLOY_CLAIM_REFUSED')

def safe(path, directory=False, private=False):
    info = path.lstat()
    need(info.st_uid == 0 and not info.st_mode & (0o077 if private else 0o022))
    need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))

def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, sort_keys=True); out.flush(); os.fsync(out.fileno())
    sync(path.parent)

def consume(request, state, authorize=lambda: None):
    # A partial two-file claim is permanently consumed; preserve it for review.
    for key in ('comment_id', 'nonce'):
        authorize()
        write(state / (key + '-' + str(request[key])), request)

def check(request, state):
    for key in ('comment_id', 'nonce'):
        path = state / (key + '-' + str(request[key]))
        safe(path, private=True)
        need(json.loads(path.read_text()) == request)

def verify_prior(request, root):
    target = root / 'releases' / request['prior'][:12] / ('robie-hermes-' + request['prior'][:12])
    need(target.is_dir() and target.resolve(strict=True) == target)
    for name in ('current', 'releases/current'):
        pointer = root / name
        need(pointer.is_symlink() and pointer.resolve(strict=True) == target)
    need((target / '.release-sha256').read_text().strip() == request['prior_sha256'])

def authorized(request, now=None):
    expiry = dt.datetime.strptime(request['expires'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc) if now is None else now
    need(now < expiry <= now + dt.timedelta(minutes=30))


def stage_files(files, directory, authorize):
    authorize()
    directory.mkdir(mode=0o700)  # Exclusive: interrupted staging is never adopted.
    sync(directory.parent)
    for name, content in files.items():
        need(re.fullmatch(r'[A-Za-z0-9_.-]+', name) is not None)
        authorize()
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            authorize()
            out.write(content); out.flush(); os.fsync(out.fileno())
        sync(directory)


def invoke_installer(request, directory, lock_fd, authorize, runner=subprocess.run):
    archive = directory / ('robie-hermes-' + request['commit'][:12] + '.tgz')
    # Logs and start/result evidence belong to the already-consumed attempt.
    authorize()
    fd = os.open(directory / 'installer.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as log:
        authorize()
        write(directory / 'install-launch-intent.json', dict(comment_id=request['comment_id'],
              receiver_run_id=request['receiver_run_id'], commit=request['commit'], sha256=request['sha256']))
        authorize()  # Last authorization boundary, on the same host as exec, under the same lock.
        result = runner(['/bin/bash', str(directory / 'deploy-test-release.sh'),
                         '--archive', str(archive), '--checksum', str(archive) + '.sha256',
                         '--commit', request['commit'], '--skip-policy-setup'],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        start_new_session=True, pass_fds=(lock_fd,), check=False)
        # Deliberately no expiry check or forced timeout after launch: rollback may be active.
        log.flush(); os.fsync(log.fileno())
    outcome = dict(status='TEST INSTALLER COMPLETED' if result.returncode == 0 else 'TEST INSTALLER FAILED',
                   returncode=result.returncode, comment_id=request['comment_id'],
                   receiver_run_id=request['receiver_run_id'], commit=request['commit'], sha256=request['sha256'])
    write(directory / 'install-result.json', outcome)
    need(result.returncode == 0)
    return outcome


def execute_locked(request, state, lock_fd, download, root, installer=invoke_installer):
    authorize = lambda: authorized(request)
    authorize()
    verify_prior(request, root)
    consume(request, state, authorize)
    check(request, state)
    files = download()  # Bounded read; expiry is rechecked after the download.
    authorize()
    check(request, state)
    verify_prior(request, root)
    directory = state / ('run-' + str(request['receiver_run_id']))
    stage_files(files, directory, authorize)
    check(request, state)
    verify_prior(request, root)
    # Ownership/rollback/expiry and invocation share this lock and remote execution.
    return installer(request, directory, lock_fd, authorize)


def run(request, transfer, transfer_source):
    need(os.geteuid() == 0 and socket.gethostname().split('.')[0] == 'hermes-test-01')
    need(request['operation'] == 'deploy' and request['actor_id'] == 320188404)
    for field, length in (('commit',40), ('sha256',64), ('prior',40), ('prior_sha256',64),
                          ('nonce',32), ('controller_commit',40)):
        need(re.fullmatch('[0-9a-f]{%d}' % length, request[field]) is not None)
    for key in ('comment_id', 'receiver_run_id', 'pr', 'pr_id'):
        need(type(request[key]) is int and request[key] > 0)
    authorized(request)
    need(transfer['commit'] == request['commit'] and transfer['archive_sha256'] == request['sha256'])
    # Source comes only from the pinned checkout, never the artifact or request.
    library = {'__name__': 'pinned_transfer_library'}
    exec(compile(transfer_source, 'pinned_transfer_library', 'exec'), library)
    def download():
        url = library['validate_url'](transfer['url'])
        opener = library['urllib'].request.build_opener(library['SafeDownloadRedirect']())
        with opener.open(url, timeout=30) as response:
            if int(response.headers.get('Content-Length', '0')) > library['MAX_BYTES']:
                raise ValueError('Download too large')
            parts = []; size = 0; deadline = time.monotonic() + 120
            while True:
                need(time.monotonic() < deadline)
                authorized(request)
                block = response.read1(min(65536, library['MAX_BYTES'] + 1 - size))
                if not block:
                    break
                parts.append(block); size += len(block)
                need(size <= library['MAX_BYTES'])
            blob = b''.join(parts)
        return library['validate_bundle'](blob, transfer)
    state = Path('/var/lib/robie-test-cloud-deploy')
    for parent in state.parents:
        safe(parent, directory=True)
    if not state.exists() and not state.is_symlink():
        authorized(request); state.mkdir(mode=0o700); sync(state.parent)
    safe(state, directory=True, private=True)
    authorized(request)
    fd = os.open(state / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        safe(state / 'lock', private=True)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        outcome = execute_locked(request, state, lock.fileno(), download, Path('/opt/streetsmart-hermes-test'))
        print(json.dumps(outcome, sort_keys=True))
'''


def render(request, transfer, source):
    payload = dict(request=request, transfer=transfer,
                   source=base64.b64encode(source).decode())
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    return (REMOTE + "\nimport base64\nPAYLOAD=json.loads(base64.b64decode('" + encoded + "'))\n"
            "try:\n    run(PAYLOAD['request'], PAYLOAD['transfer'], base64.b64decode(PAYLOAD['source']))\n"
            "except Exception:\n    raise SystemExit('TEST CLOUD DEPLOY UNVERIFIED: preserve receipts; no retry') from None\n")


if __name__ == '__main__':
    request = json.loads(Path(os.environ['RUNNER_TEMP'], 'cloud-deploy-request.json').read_text())
    path = Path(__file__).with_name('transfer-test-artifact.py')
    library = runpy.run_path(str(path))
    expected_installer = hashlib.sha256(Path(__file__).with_name('deploy-test-release.sh').read_bytes()).hexdigest()
    if os.environ['INSTALLER_SHA256'] != expected_installer:
        raise SystemExit('PINNED_TEST_INSTALLER_MISMATCH')
    transfer = dict(commit=request['commit'], archive_sha256=request['sha256'],
        installer_sha256=library['require_hex'](os.environ['INSTALLER_SHA256'], 64),
        artifact_sha256=library['require_hex'](os.environ['ARTIFACT_SHA256'], 64),
        url=library['artifact_url'](os.environ))
    print(render(request, transfer, path.read_bytes()))
