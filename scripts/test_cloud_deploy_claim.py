"""Render only a root-private Test replay claim/check; never installs a release.

The receiver runs claim before staging; failure consumes the request. Check runs
again immediately before the existing installer. No automatic retry or cleanup.
"""
import base64
import json
import os
from pathlib import Path
import sys

REMOTE = r'''
import datetime as dt, fcntl, json, os, re, socket, stat
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

def consume(request, state):
    # A partial two-file claim is permanently consumed; preserve it for review.
    for key in ('comment_id', 'nonce'):
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

def run(request, mode):
    need(mode in {'claim', 'check'})
    need(os.geteuid() == 0 and socket.gethostname().split('.')[0] == 'hermes-test-01')
    need(request['operation'] == 'deploy' and request['actor_id'] == 320188404)
    for field, length in (('commit',40), ('sha256',64), ('prior',40), ('prior_sha256',64),
                          ('nonce',32), ('controller_commit',40)):
        need(re.fullmatch('[0-9a-f]{%d}' % length, request[field]) is not None)
    for key in ('comment_id', 'receiver_run_id', 'pr', 'pr_id'):
        need(type(request[key]) is int and request[key] > 0)
    expiry = dt.datetime.strptime(request['expires'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    need(now < expiry <= now + dt.timedelta(minutes=30))
    verify_prior(request, Path('/opt/streetsmart-hermes-test'))
    state = Path('/var/lib/robie-test-cloud-deploy')
    for parent in state.parents:
        safe(parent, directory=True)
    if mode == 'claim' and not state.exists() and not state.is_symlink():
        state.mkdir(mode=0o700); sync(state.parent)
    safe(state, directory=True, private=True)
    fd = os.open(state / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        safe(state / 'lock', private=True)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if mode == 'claim':
            consume(request, state)
        else:
            check(request, state)
        print(json.dumps(dict(status='CLAIMED' if mode == 'claim' else 'CLAIM VERIFIED',
                              comment_id=request['comment_id'], receiver_run_id=request['receiver_run_id'])))
'''


def render(request, mode):
    if mode not in {'claim', 'check'}:
        raise ValueError('invalid claim mode')
    encoded = base64.b64encode(json.dumps(request, sort_keys=True).encode()).decode()
    return (REMOTE + "\nimport base64\nrun(json.loads(base64.b64decode('" + encoded + "')), '" + mode + "')\n")


if __name__ == '__main__':
    request = json.loads(Path(os.environ['RUNNER_TEMP'], 'cloud-deploy-request.json').read_text())
    print(render(request, sys.argv[1]))
