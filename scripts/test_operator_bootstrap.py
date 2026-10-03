"""Render a fixed inspection-only root bootstrap from the reviewed controller tree.

Executed locally on Actions to emit Python stdin, never from issue code. Refuses
any existing helper/config/claim; partial setup requires evidence-preserving review.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

# Kept self-contained so the remote host needs no repository or auxiliary tools.
REMOTE = r'''
import base64, datetime as dt, fcntl, hashlib, json, os, socket, stat, sys
from pathlib import Path

def need(ok):
    if not ok:
        raise ValueError('BOOTSTRAP_REFUSED')

def safe(path, directory=False, private=False):
    info = path.lstat()
    need(info.st_uid == 0 and not info.st_mode & (0o077 if private else 0o022))
    need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    for parent in path.parents:
        item = parent.lstat()
        need(stat.S_ISDIR(item.st_mode) and item.st_uid == 0 and not item.st_mode & 0o022)

def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def write(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    sync(path.parent)

def run(payload):
    need(os.geteuid() == 0 and socket.gethostname().split('.')[0] == 'hermes-test-01')
    need(dt.datetime.now(dt.timezone.utc) < dt.datetime.strptime(payload['expires'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc))
    helper = Path('/usr/local/libexec/robie-test-operator.py')
    config = Path('/etc/robie-test-operator.json')
    state = Path('/var/lib/robie-test-operator')
    # Prior installation is never overwritten or adopted automatically.
    need(not helper.exists() and not helper.is_symlink() and not config.exists() and not config.is_symlink())
    for parent in (Path('/usr/local'), Path('/etc'), Path('/var/lib')):
        safe(parent, directory=True)
    for directory in (helper.parent, state):
        if not directory.exists() and not directory.is_symlink():
            directory.mkdir(mode=0o700 if directory == state else 0o755)
            sync(directory.parent)
        safe(directory, directory=True, private=directory == state)
    claim = state / 'inspection-bootstrap.json'
    fd = os.open(state / 'operator.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        safe(state / 'operator.lock', private=True)
        source = base64.b64decode(payload['helper_base64'], validate=True)
        need(hashlib.sha256(source).hexdigest() == payload['helper_sha256'])
        cfg = payload['config']
        need(cfg['enabled'] == 'INSPECT_ONLY_V1' and cfg['actor_ids'] == [320188404])
        write(claim, json.dumps({k:v for k,v in payload.items() if k != 'helper_base64'}, sort_keys=True).encode(), 0o600)
        write(helper, source, 0o644)
        write(config, json.dumps(cfg, sort_keys=True).encode(), 0o600)
        safe(helper)
        safe(config, private=True)
        need(hashlib.sha256(helper.read_bytes()).hexdigest() == payload['helper_sha256'])
        need(json.loads(config.read_text()) == cfg)
        result = dict(status='INSPECTION BOOTSTRAP INSTALLED', host='hermes-test-01',
                      controller_commit=payload['controller_commit'], helper_sha256=payload['helper_sha256'],
                      issue=cfg['issue'], pr_id=cfg['pr_id'], actor_ids=cfg['actor_ids'], stopped_operations_enabled_by_bootstrap=False,
                      runtime_changed=False)
        write(state / 'inspection-bootstrap-complete.json', json.dumps(result, sort_keys=True).encode(), 0o600)
        print(json.dumps(result, sort_keys=True))

try:
    run(PAYLOAD)
except Exception:
    raise SystemExit('INSPECTION_BOOTSTRAP_REFUSED: preserve setup evidence; no automatic retry') from None
'''


def render(request, controller, approved, helper):
    if not (re.fullmatch('[0-9a-f]{40}', controller or '') and controller == approved
            and request['operation'] == 'bootstrap-inspect' and request['actor_id'] == 320188404
            and type(request['issue']) is int and request['issue'] > 0
            and type(request['pr_id']) is int and request['pr_id'] > 0):
        raise ValueError('BOOTSTRAP_CONTROLLER_REFUSED')
    payload = dict(controller_commit=controller, expires=request['expires'],
        comment_id=request['comment_id'], nonce=request['nonce'],
        helper_base64=base64.b64encode(helper).decode(), helper_sha256=hashlib.sha256(helper).hexdigest(),
        config=dict(enabled='INSPECT_ONLY_V1', commit=request['commit'], sha256=request['sha256'],
                    issue=request['issue'], pr_id=request['pr_id'], actor_ids=[320188404]))
    # JSON data is decoded, not interpolated as Python/shell syntax.
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    return "import base64,json\nPAYLOAD=json.loads(base64.b64decode('" + encoded + "'))\n" + REMOTE


def main():
    request = json.loads(Path(os.environ['RUNNER_TEMP'], 'operator-request.json').read_text())
    source = Path(__file__).with_name('test_operator_inspect.py').read_bytes()
    checkout = subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    print(render(request, checkout, os.environ['OPERATOR_SETUP_COMMIT'], source))


if __name__ == '__main__':
    main()
