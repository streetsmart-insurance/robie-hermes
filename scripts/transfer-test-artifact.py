#!/usr/bin/env python3
"""Download one exact GitHub build on Test; never log bearer download URLs."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = 'streetsmart-insurance/robie-hermes'
STAGING_ROOT = Path('/var/tmp/robie-test-deploy')
MAX_BYTES = 32 * 1024 * 1024


def require_hex(value, length):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{%d}' % length, value):
        raise ValueError('Invalid immutable identifier')
    return value


def validate_url(value):
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or
            parsed.port not in (None, 443) or '\n' in value or '\r' in value or
            not (host.endswith('.blob.core.windows.net') or host.endswith('.actions.githubusercontent.com'))):
        raise ValueError('Invalid artifact download endpoint')
    return value


class SafeDownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_bundle(blob, payload):
    commit = require_hex(payload['commit'], 40)
    archive_hash = require_hex(payload['archive_sha256'], 64)
    installer_hash = require_hex(payload['installer_sha256'], 64)
    zip_hash = require_hex(payload['artifact_sha256'], 64)
    if len(blob) > MAX_BYTES or hashlib.sha256(blob).hexdigest() != zip_hash:
        raise ValueError('Artifact archive digest mismatch')
    archive = 'robie-hermes-' + commit[:12] + '.tgz'
    expected = {archive, archive + '.sha256', 'deploy-test-release.sh'}
    with zipfile.ZipFile(io.BytesIO(blob)) as bundle:
        members = bundle.infolist()
        if len(members) != 3 or {item.filename for item in members} != expected:
            raise ValueError('Unexpected artifact members')
        if sum(item.file_size for item in members) > MAX_BYTES:
            raise ValueError('Artifact contents too large')
        for item in members:
            mode = item.external_attr >> 16
            if item.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError('Artifact member is not a regular file')
        files = {item.filename: bundle.read(item) for item in members}
    if hashlib.sha256(files[archive]).hexdigest() != archive_hash:
        raise ValueError('Release digest mismatch')
    if hashlib.sha256(files['deploy-test-release.sh']).hexdigest() != installer_hash:
        raise ValueError('Installer digest mismatch')
    if files[archive + '.sha256'].decode().split() != [archive_hash, archive]:
        raise ValueError('Release checksum file mismatch')
    return files


def receive():
    if socket.gethostname().split('.')[0] != 'hermes-test-01':
        raise ValueError('Test host required')
    payload = json.load(sys.stdin)
    commit = require_hex(payload['commit'], 40)
    url = validate_url(payload['url'])
    opener = urllib.request.build_opener(SafeDownloadRedirect())
    with opener.open(url, timeout=30) as response:
        if int(response.headers.get('Content-Length', '0')) > MAX_BYTES:
            raise ValueError('Download too large')
        blob = response.read(MAX_BYTES + 1)
    files = validate_bundle(blob, payload)
    staging = STAGING_ROOT / commit
    if staging.is_symlink():
        raise ValueError('Staging cannot be a symlink')
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    if staging.stat().st_uid != os.getuid():
        raise ValueError('Staging owner mismatch')
    for name, content in files.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=staging, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
            os.replace(temporary, staging / name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    print('TEST_ARTIFACT_TRANSFER_OK')


def artifact_url(env):
    commit = require_hex(env['GITHUB_SHA'], 40)
    artifact_hash = require_hex(env['ARTIFACT_SHA256'], 64)
    artifact_id = env['ARTIFACT_ID']
    if not artifact_id.isdigit() or not env['GITHUB_RUN_ID'].isdigit():
        raise ValueError('Invalid artifact/run id')
    endpoint = 'https://api.github.com/repos/' + REPOSITORY + '/actions/artifacts/' + artifact_id
    headers = {'Authorization': 'Bearer ' + env['GITHUB_TOKEN'],
               'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(urllib.request.Request(endpoint, headers=headers), timeout=30) as response:
        metadata = json.load(response)
    run = metadata.get('workflow_run') or {}
    expected_name = 'test-release-' + commit + '-' + env['GITHUB_RUN_ATTEMPT']
    if (metadata.get('expired') or str(run.get('id')) != env['GITHUB_RUN_ID'] or
            run.get('head_sha') != commit or metadata.get('name') != expected_name or
            metadata.get('size_in_bytes', MAX_BYTES + 1) > MAX_BYTES):
        raise ValueError('Artifact does not match this exact workflow run')
    if metadata.get('digest') and metadata['digest'] != 'sha256:' + artifact_hash:
        raise ValueError('GitHub artifact digest mismatch')
    try:
        opener.open(urllib.request.Request(endpoint + '/zip', headers=headers), timeout=30)
    except urllib.error.HTTPError as response:
        if response.code == 302:
            return validate_url(response.headers['Location'])
        raise
    raise ValueError('Expected a temporary artifact download URL')


def transfer(env, runner=subprocess.run):
    if (env.get('GITHUB_REF') != 'refs/heads/main' or env.get('GITHUB_REPOSITORY') != REPOSITORY or
            env.get('TEST_VM') != 'hermes-test-01' or env.get('PROJECT_ID') != 'streetsmart-hermes-poc' or
            env.get('ZONE') != 'us-east1-b' or env.get('SSH_KEY') != '/tmp/hermes-test-deploy'):
        raise ValueError('Protected-main Test deployment required')
    commit = require_hex(env['GITHUB_SHA'], 40)
    payload = {'commit': commit, 'archive_sha256': require_hex(env['ARCHIVE_SHA256'], 64),
               'installer_sha256': require_hex(env['INSTALLER_SHA256'], 64),
               'artifact_sha256': require_hex(env['ARTIFACT_SHA256'], 64), 'url': artifact_url(env)}
    # URL is stdin data, never an argument, shell fragment, environment, or log.
    command = ['gcloud', 'compute', 'ssh', 'hermes-test-01', '--project=streetsmart-hermes-poc',
               '--zone=us-east1-b', '--tunnel-through-iap', '--quiet',
               '--ssh-key-file=/tmp/hermes-test-deploy', '--ssh-flag=-T',
               '--command=python3 -c ' + shlex.quote(Path(__file__).read_text()) + ' --receive']
    result = runner(command, input=json.dumps(payload), text=True, capture_output=True, timeout=150)
    if result.returncode or result.stdout.splitlines().count('TEST_ARTIFACT_TRANSFER_OK') != 1:
        raise RuntimeError('Remote artifact download or validation failed')
    print('TEST_ARTIFACT_TRANSFER_OK')


if __name__ == '__main__':
    try:
        receive() if sys.argv[1:] == ['--receive'] else transfer(os.environ)
    except Exception as exc:
        # Network exceptions can contain bearer URLs. Never print their values.
        print('TEST_ARTIFACT_TRANSFER_FAILED:' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
