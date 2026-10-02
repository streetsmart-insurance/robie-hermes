"""Fixed Test-host snapshot. No application imports, mutations, or client requests.

Exit 0 means the bounded snapshot checks passed, never deploy authorization.
It does not attest in-memory Python modules or hold intake/driver admission.
"""
from __future__ import annotations

import hashlib
import gzip
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import tarfile
from datetime import datetime, timezone
from urllib.parse import quote

ROOT = Path('/opt/streetsmart-hermes-test')
STAGING = Path('/var/tmp/robie-test-deploy')
HOST = 'hermes-test-01'
MAX_FILE = 2 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_MEMBERS = 20000
POLICY_FILES = ('SKILL.md', 'references/profiles.json', 'references/selector-inventory.md')
# Exact reviewed shim hashes from deploy_truth.zip_load_shim_source; no runtime import.
OVERLAYS = (
    ('scripts/robie_email_agent.py', 'scripts/robie_email_agent.py', '9cb31b9ded0d835ab2e4d688709b4603bc5d03b3d0e8da4312c5f6a765c9ee6c'),
    ('integrations/google_chat/adapter.py', 'hermes-agent/plugins/platforms/google_chat/adapter.py', '941db52b7ea20db6319b5ef35b714b768b3a7e75b97cedec3238bf4624913595'),
    ('integrations/google_chat/oauth.py', 'hermes-agent/plugins/platforms/google_chat/oauth.py', '4e073a17dfea42fa38ea6aa04ea9716e1283799a3ffe5eee04ed2d5fd1574468'),
    ('deploy/hermes/tools/playwright_tool.py', 'hermes-agent/tools/playwright_tool.py', '4768564aa44aed96cf8b360d7e3d736bde0833e7aa3301c85476d0f0add938c4'),
    ('deploy/hermes/tools/playwright_write_guard.py', 'hermes-agent/tools/playwright_write_guard.py', 'ef1dbcf8f615ad518e09953ab4d452dbe1ddb1c74855b6a18d277bf3b0953ea8'),
    ('deploy/hermes/tools/gemini_field_tool.py', 'hermes-agent/tools/gemini_field_tool.py', '7b66bce1c2a62f3452e94de02e0d66362cae3d00c01095355304c0f8e00d1a9c'),
    ('deploy/hermes/tools/policy_setup_tool.py', 'hermes-agent/tools/policy_setup_tool.py', 'cad42de398c8cbc621baf86f6b6df8152ac491e601b4261a17bb33e4ca196b5e'),
    ('deploy/hermes/tools/ezlynx_document_tool.py', 'hermes-agent/tools/ezlynx_document_tool.py', 'fc2edb9ade0b363d65d8e47717912eba14ef27d9205086f2bc861bbb23b444fa'),
    ('deploy/hermes/tools/ezlynx_note_tool.py', 'hermes-agent/tools/ezlynx_note_tool.py', '155d00119917a1544e2f108fdf67f6a1fc7b5152c88079c677fbc7d9f6b0b6a6'),
)
STATES = {'PENDING', 'NEEDS_SKILL', 'NEEDS_CLARIFICATION', 'NEEDS_AUTH',
          'AWAITING_HUMAN_INPUT', 'WAITING', 'RUNNING', 'VERIFYING', 'RETRY_WAIT',
          'PAUSED', 'COMPLETE', 'UNVERIFIED', 'FAILED'}


class Refused(Exception):
    """Only fixed diagnostic codes may reach output."""


class CollectorTimeout(BaseException):
    """Escape individual check handlers when the whole audit exceeds its limit."""


def require(ok, code):
    if not ok:
        raise Refused(code)


def timestamp(value):
    require(isinstance(value, str), 'timestamp_type')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(stamp.tzinfo is not None, 'timestamp_timezone')
    return stamp


def resolved(path, boundary, *, stable=False):
    """Walk every component, including symlinks hidden by '..' or parent links."""
    require(boundary.is_absolute() and boundary.resolve() == boundary, 'boundary_redirected')
    pending = list(path.absolute().parts[1:])
    current = Path('/')
    hops = 0
    while pending:
        part = pending.pop(0)
        if part in ('', '.'):
            continue
        if part == '..':
            current = current.parent
            continue
        current /= part
        if stable:
            require(current not in (boundary / 'current', boundary / 'releases/current'),
                    'floating_current_link')
        if current.is_symlink():
            hops += 1
            require(hops <= 40, 'symlink_loop')
            target = Path(os.readlink(current))
            current = Path('/') if target.is_absolute() else current.parent
            pending = list(target.parts[1:] if target.is_absolute() else target.parts) + pending
    require(current.is_relative_to(boundary.resolve()), 'path_outside_boundary')
    require(current.exists(), 'path_missing')
    return current


def read_file(path, boundary, limit=MAX_FILE, *, stable=False):
    target = resolved(path, boundary, stable=stable)
    # O_NOFOLLOW plus descriptor stat avoids FIFO/device reads and final-link races.
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'file_type_or_size')
        data = stream.read(limit + 1)
    require(len(data) <= limit, 'file_size')
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path, root):
    value = json.loads(read_file(path, root))
    require(isinstance(value, dict), 'json_object_required')
    return value


def gateway():
    names = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'ActiveEnterTimestamp')
    result = subprocess.run(
        ['systemctl', 'show', 'robie-gateway', '--no-pager',
         *[arg for name in names for arg in ('-p', name)]],
        capture_output=True, text=True, timeout=10, check=True,
        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'})
    require(len(result.stdout) <= 4096, 'systemctl_output_size')
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines())
    require(set(fields) == set(names), 'gateway_properties_missing')
    require(fields['LoadState'] == 'loaded' and fields['ActiveState'] == 'active'
            and fields['SubState'] == 'running', 'gateway_not_running')
    pid = int(fields['MainPID'])
    require(pid > 0, 'gateway_pid_missing')
    started = datetime.strptime(fields['ActiveEnterTimestamp'], '%a %Y-%m-%d %H:%M:%S %Z')
    return {'pid': pid, 'active_enter': started.replace(tzinfo=timezone.utc).isoformat()}


def driver():
    connection = HTTPConnection('metadata.google.internal', timeout=5)
    try:
        connection.request('GET', '/computeMetadata/v1/project/attributes/robie-ezlynx-driver',
                           headers={'Metadata-Flavor': 'Google'})
        response = connection.getresponse()
        require(response.status == 200 and response.getheader('Metadata-Flavor') == 'Google',
                'driver_metadata_response')
        data = response.read(4097)
    finally:
        connection.close()
    require(len(data) <= 4096, 'driver_size')
    value = json.loads(data)
    state, holder = value['state'], value['holder']
    require((state, holder) in {('OUT', 'NONE'), ('IN', 'TEST'), ('IN', 'PRODUCTION')},
            'driver_invalid')
    expires = timestamp(value['expires_at'])
    updated = timestamp(value['updated_at'])
    require(updated <= datetime.now(timezone.utc), 'driver_future_update')
    return {'state': state, 'holder': holder, 'expires_at': expires.isoformat(),
            'updated_at': updated.isoformat(),
            'clear': state == 'OUT' or (holder == 'TEST' and expires > datetime.now(timezone.utc))}


def database(root, release):
    path = resolved(root / 'robie-job-engine/data/jobs.db', root)
    require(path.is_file(), 'database_missing')
    # Do not let a read-only SQLite connection create a missing WAL sidecar.
    require(not Path(str(path) + '-wal').exists() or Path(str(path) + '-shm').is_file(),
            'wal_shared_memory_missing')
    conn = sqlite3.connect('file:' + quote(str(path)) + '?mode=ro', uri=True, timeout=2)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.set_progress_handler(lambda: 1 if datetime.now(timezone.utc).timestamp() > deadline else 0, 10000)
        deadline = datetime.now(timezone.utc).timestamp() + 5
        conn.execute('BEGIN')
        counts = dict(conn.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status'))
        require(set(counts) <= STATES, 'job_status_unknown')
        leases = conn.execute('''SELECT COUNT(*) FROM jobs WHERE status IN ('RUNNING','VERIFYING')
                               OR lease_owner IS NOT NULL''').fetchone()[0]
        pending = sum(count for state, count in counts.items() if state not in {'COMPLETE','FAILED','UNVERIFIED'})
        queue = dict(conn.execute('SELECT state,COUNT(*) FROM chat_event_queue GROUP BY state'))
        require(set(queue) <= {'QUEUED','INFLIGHT','AWAITING_HUMAN_INPUT','COMPLETE','FAILED'}, 'queue_state_unknown')
        queue_leases = conn.execute('SELECT COUNT(*) FROM chat_event_queue WHERE lease_owner IS NOT NULL').fetchone()[0]
        links = conn.execute('SELECT COUNT(*) FROM conversation_job_links WHERE active=1').fetchone()[0]
        outbox_present = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='chat_reply_outbox'").fetchone()[0]
        if outbox_present:
            replies = dict(conn.execute('SELECT state,COUNT(*) FROM chat_reply_outbox GROUP BY state'))
            require(set(replies) <= {'pending','sending','delivered','cancelled','failed'}, 'reply_state_unknown')
        else:
            require(not (release / 'robie_job_engine/chat_reply_outbox.py').exists(), 'reply_schema_missing')
            replies = None  # Explicitly absent in a pre-outbox release, never zero.
        result = {'jobs_by_status': counts, 'jobs_with_active_status_or_lease': leases,
                  'nonterminal_jobs': pending, 'queue_by_state': queue, 'queue_leases': queue_leases,
                  'active_conversation_links': links, 'reply_outbox_by_state': replies}
        result['clear'] = not (leases or pending or queue_leases or links
                              or any(queue.get(s, 0) for s in ('QUEUED','INFLIGHT','AWAITING_HUMAN_INPUT'))
                              or any((replies or {}).get(s, 0) for s in ('pending','sending')))
        return result
    finally:
        conn.close()


def policy(root):
    base = root / '.hermes/skills/ezlynx-policy-setup'
    return {name: {'resolved_path': str(resolved(base / name, root, stable=True)),
                   'sha256': digest(read_file(base / name, root, stable=True))}
            for name in POLICY_FILES}


def release_evidence(root, staging, live_gateway, output):
    release = resolved(root / 'current', root)
    require(release == resolved(root / 'releases/current', root), 'pointers_disagree')
    match = re.fullmatch(r'robie-hermes-([0-9a-f]{12})', release.name)
    require(match is not None and release.parent.parent == root / 'releases', 'release_layout')
    short = match[1]
    require(release.parent.name == short, 'release_directory_sha')
    evidence = read_json(root / f'deployments/{short}/test-deploy-evidence.json', root)
    commit = evidence['commit']
    require(re.fullmatch('[0-9a-f]{40}', commit) is not None and commit.startswith(short), 'release_commit')
    sha = read_file(release / '.release-sha256', root, 128).decode().strip()
    require(re.fullmatch('[0-9a-f]{64}', sha) is not None and evidence['release_sha256'] == sha, 'release_digest')
    require(evidence['environment'] == 'Test' and evidence['host'] == HOST
            and evidence['release_root'] == str(release), 'deployment_identity')
    output.update(commit=commit, release_root=str(release), sha256=sha)
    proof = read_json(release / 'official-install-proof.json', root)
    flip = read_json(release / 'official-install-flip.json', root)
    require(proof['sha'] == short and proof['done'] is True and proof['live'] is True
            and proof['authorizes_complete'] is False and proof['proof']['live'] is True,
            'stored_install_proof')
    require(flip['sha'] == short and flip['release_root'] == str(release), 'flip_identity')
    require(timestamp(live_gateway['active_enter']) > timestamp(flip['flip_at']), 'gateway_predates_flip')
    output['stored_proof_live'] = True
    # The known installer retains its transfer at this exact commit-scoped path.
    archive = staging / commit / f'robie-hermes-{short}.tgz'
    data = read_file(archive, staging, MAX_ARCHIVE, stable=True)
    actual = digest(data)
    checksum = read_file(Path(str(archive) + '.sha256'), staging, 256).decode().split()
    require(checksum == [sha, archive.name] and actual == sha, 'rollback_archive_digest')
    output['rollback_archive'] = str(archive)
    import io
    wanted = {f'robie-hermes-{short}/{source}': source for source, _, _ in OVERLAYS}
    archived = {}
    class BoundedTar:
        def __init__(self, stream):
            self.stream, self.consumed = stream, 0

        def read(self, size):
            require(self.consumed + size <= MAX_EXPANDED, 'archive_expansion_limit')
            chunk = self.stream.read(size)
            self.consumed += len(chunk)
            return chunk

    with gzip.GzipFile(fileobj=io.BytesIO(data)) as decompressed, \
            tarfile.open(fileobj=BoundedTar(decompressed), mode='r|') as bundle:
        total = 0
        seen = set()
        for index, member in enumerate(bundle):
            total += member.size
            require(index < MAX_MEMBERS and total <= MAX_EXPANDED, 'archive_limits')
            parts = Path(member.name).parts
            require(parts and parts[0] == f'robie-hermes-{short}' and '..' not in parts
                    and (member.isfile() or member.isdir()) and member.name not in seen,
                    'archive_member_invalid')
            seen.add(member.name)
            if member.name in wanted:
                require(member.isfile() and member.size <= MAX_FILE and member.name not in archived,
                        'archive_member_invalid')
                archived[member.name] = digest(bundle.extractfile(member).read(MAX_FILE + 1))
    require(set(archived) == set(wanted), 'archive_sources_missing')
    output['rollback_archive_verified'] = True
    overlays = []
    for source, dest, known_shim in OVERLAYS:
        source_sha = digest(read_file(release / source, root, stable=True))
        require(source_sha == archived[f'robie-hermes-{short}/{source}'], 'release_source_changed')
        dest_sha = digest(read_file(root / '.hermes' / dest, root))
        require(dest_sha in (source_sha, known_shim), 'overlay_mismatch')
        overlays.append({'source': source, 'destination': dest, 'source_sha256': source_sha,
                         'installed_sha256': dest_sha, 'mode': 'copy' if dest_sha == source_sha else 'reviewed_shim'})
    output['overlays'] = overlays
    return output


def collect(root=ROOT, staging=STAGING):
    report = {'schema': 1, 'collected_at': datetime.now(timezone.utc).isoformat(),
              'snapshot_verified': False, 'deployment_authorized': False,
              'loaded_process_modules': 'UNVERIFIED', 'intake_hold': 'NOT_ACQUIRED',
              'rollback_rehearsal': 'NOT_PERFORMED', 'checks': {}, 'errors': []}
    if socket.gethostname().split('.')[0] != HOST:
        report['errors'].append('wrong_host')
        return report
    report['host'] = HOST

    def check(name, read):
        try:
            value = read()
            report['checks'][name] = value
            return value
        except Refused as exc:
            report['errors'].append(str(exc))
        except Exception as exc:
            # Exception messages can contain SQL, credentials or payloads.
            report['errors'].append(type(exc).__name__)
        report['checks'].setdefault(name, {})['verified'] = False
        return None

    initial = check('gateway', gateway)
    lease = check('driver', driver)
    release = check('pointer', lambda: {'path': str(resolved(root / 'current', root))})
    if initial and release:
        check('release', lambda: release_evidence(root, staging, initial,
                                                 report['checks'].setdefault('release', {})))
    skill = check('policy_skill', lambda: policy(root))
    jobs = check('durable_work', lambda: database(root, Path(release['path']))) if release else None
    if jobs and not jobs['clear']:
        report['errors'].append('durable_work_not_quiescent')
    if lease and not lease['clear']:
        report['errors'].append('driver_conflict_or_expired')
    if not report['errors']:
        def unchanged():
            require(gateway() == initial and driver() == lease
                    and str(resolved(root / 'current', root)) == release['path']
                    and str(resolved(root / 'releases/current', root)) == release['path']
                    and policy(root) == skill, 'snapshot_changed')
            return {'verified': True}
        check('stable_snapshot', unchanged)
    report['snapshot_verified'] = not report['errors']
    return report


def main():
    def timeout(*_):
        raise CollectorTimeout()
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(120)
    try:
        report = collect()
    except CollectorTimeout:
        report = {'schema': 1, 'snapshot_verified': False, 'deployment_authorized': False,
                  'errors': ['collector_timeout']}
    finally:
        signal.alarm(0)
    print(json.dumps(report, sort_keys=True))
    return 0 if report['snapshot_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
