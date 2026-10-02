"""Fixed Test-host snapshot. No application imports, mutations, or client requests.

Exit 0 means the bounded snapshot checks passed, never deploy authorization.
It does not attest in-memory Python modules or hold intake/driver admission.
"""
from __future__ import annotations

import argparse
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
import time
from datetime import datetime, timezone
from urllib.parse import quote

ROOT = Path('/opt/streetsmart-hermes-test')
STAGING = Path('/var/tmp/robie-test-deploy')
HOST = 'hermes-test-01'
HANDOVER_CURRENT = ('592e148ff1df640556c30aee4089fa7551db7e20',
                    'fa468342bfe0d6e2083eb8af9282274e62d49fa353069a92c9d14e122db42964')
HANDOVER_OLDER = ('73e720702350f908b2f5ebd5fe22c773b0b26c30',
                  'edad5eff3f327ce0430c7751f36d33b8c667541ce15789eff2ec6738fe83d1b1')
HANDOVER_ARCHIVE = Path('/tmp/robie-hermes-73e720702350.tgz')
MAX_FILE = 2 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_MEMBERS = 20000
SEARCH_ENTRIES = 2000
SEARCH_CANDIDATES = 8
SEARCH_SECONDS = 10
SEARCH_DEPTH = 3
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
          'PAUSED', 'COMPLETE', 'UNVERIFIED', 'FAILED', 'CANCELLED'}


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
    # Normal SQLite reader locks/SHM read marks are permitted, logical writes
    # are not. Require existing sidecars so a WAL read need not create them.
    # Never use immutable=1: it would silently omit committed WAL work.
    with path.open('rb') as stream:
        header = stream.read(100)
    require(len(header) == 100 and header[:16] == b'SQLite format 3\x00', 'database_header')
    require(header[18:20] in (b'\x01\x01', b'\x02\x02'), 'database_journal_mode')
    sidecars = [Path(str(path) + suffix) for suffix in ('-wal', '-shm')]
    if header[18:20] == b'\x02\x02' or any(os.path.lexists(p) for p in sidecars):
        require(all(p.exists() for p in sidecars), 'wal_sidecars_missing')
        require(all(p.is_file() and not p.is_symlink() and resolved(p, root) == p
                    for p in sidecars), 'wal_sidecars_invalid')
    conn = sqlite3.connect('file:' + quote(str(path)) + '?mode=ro', uri=True, timeout=2)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.set_progress_handler(lambda: 1 if datetime.now(timezone.utc).timestamp() > deadline else 0, 10000)
        deadline = datetime.now(timezone.utc).timestamp() + 5
        conn.execute('BEGIN')
        def buckets(table, column, allowed):
            # Identifiers are fixed call-site constants; unknown values never leave SQL.
            allowed = sorted(allowed)
            placeholders = ','.join('?' for _ in allowed)
            return dict(conn.execute(
                f'SELECT CASE WHEN {column} IN ({placeholders}) THEN {column} '
                f'ELSE ? END AS bucket,COUNT(*) FROM {table} GROUP BY bucket',
                (*allowed, 'UNKNOWN')))

        counts = buckets('jobs', 'status', STATES)
        leases = conn.execute('''SELECT COUNT(*) FROM jobs WHERE status IN ('RUNNING','VERIFYING')
                               OR lease_owner IS NOT NULL''').fetchone()[0]
        pending = sum(count for state, count in counts.items() if state not in {'COMPLETE','FAILED','UNVERIFIED'})
        queue = buckets('chat_event_queue', 'state',
                        {'QUEUED','INFLIGHT','AWAITING_HUMAN_INPUT','COMPLETE','FAILED'})
        queue_leases = conn.execute('SELECT COUNT(*) FROM chat_event_queue WHERE lease_owner IS NOT NULL').fetchone()[0]
        links = conn.execute('SELECT COUNT(*) FROM conversation_job_links WHERE active=1').fetchone()[0]
        outbox_present = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='chat_reply_outbox'").fetchone()[0]
        if outbox_present:
            replies = buckets('chat_reply_outbox', 'state',
                              {'pending','sending','delivered','cancelled','failed'})
        else:
            require(not (release / 'robie_job_engine/chat_reply_outbox.py').exists(), 'reply_schema_missing')
            replies = None  # Explicitly absent in a pre-outbox release, never zero.
        result = {'jobs_by_status': counts, 'jobs_with_active_status_or_lease': leases,
                  'nonterminal_jobs': pending, 'queue_by_state': queue, 'queue_leases': queue_leases,
                  'active_conversation_links': links, 'reply_outbox_by_state': replies,
                  'cancelled_semantics': 'UNVERIFIED' if counts.get('CANCELLED') else 'NOT_OBSERVED',
                  'blocking_codes': [code for value, code in (
                      (counts.get('UNKNOWN'), 'job_status_unknown'),
                      (counts.get('CANCELLED'), 'cancelled_semantics_unverified'),
                      (queue.get('UNKNOWN'), 'queue_state_unknown'),
                      ((replies or {}).get('UNKNOWN'), 'reply_state_unknown')) if value]}
        result['clear'] = not (leases or pending or queue_leases or links
                              or result['blocking_codes']
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


def open_directory(path):
    """Pin every directory component; never follow even an intermediate symlink."""
    require(path.is_absolute() and '..' not in path.parts, 'archive_search_path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def archive_bytes(directory_fd, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'archive_file_type_or_size')
        data = stream.read(limit + 1)
        require(len(data) <= limit, 'archive_file_type_or_size')
        after = os.fstat(stream.fileno())
        linked = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        require(identity(info) == identity(after) == identity(linked)
                and len(data) == info.st_size, 'file_changed_during_read')
        return data


def exact_bytes(path, limit=MAX_FILE):
    fd = open_directory(path.parent)
    try:
        return archive_bytes(fd, path.name, limit)
    finally:
        os.close(fd)


def exact_current_archive(root, staging, commit, sha, output):
    # No directory enumeration, no /tmp fallback, no older-release substitution.
    name = f'robie-hermes-{commit[:12]}.tgz'
    paths = (staging / commit / name, staging / commit[:12] / name,
             root / 'deployments' / commit[:12] / name,
             root / 'releases' / commit[:12] / name)
    output['exact_archive_paths'] = [str(path) for path in paths]
    for path in paths:
        try:
            data = exact_bytes(path, MAX_ARCHIVE)
        except FileNotFoundError:
            continue
        require(digest(data) == sha, 'rollback_archive_digest')
        return path, data
    raise Refused('current_archive_exact_paths_missing')


def archive_digest_before(data, deadline):
    hasher = hashlib.sha256()
    view = memoryview(data)
    for offset in range(0, len(view), 1024 * 1024):
        require(time.monotonic() <= deadline, 'archive_search_limit')
        hasher.update(view[offset:offset + 1024 * 1024])
        require(time.monotonic() <= deadline, 'archive_search_limit')
    value = hasher.hexdigest()
    require(time.monotonic() <= deadline, 'archive_search_limit')
    return value


def find_rollback_archive(root, staging, commit, sha, output):
    """Search release-storage layouts only, never source/runtime/home trees.

    Directory names are an explicit layout allowlist. A negative result is
    bounded-scope evidence, not a claim that the archive exists nowhere.
    """
    roots = (staging, root / 'deployments', root / 'releases')
    name = f'robie-hermes-{commit[:12]}.tgz'
    layout = re.compile(r'(?:[0-9a-f]{12,40}|robie-hermes-[0-9a-f]{12}|archives|packages|retained|releases|deployments)')
    search = {'roots': [str(p) for p in roots], 'entries': 0, 'candidates': 0,
              'bytes_reserved': 0, 'complete': False, 'scope': 'release_storage_layouts_only'}
    output['archive_search'] = search
    deadline = time.monotonic() + SEARCH_SECONDS
    incomplete = False
    candidate_error = None

    def walk(fd, path, depth):
        nonlocal incomplete, candidate_error
        with os.scandir(fd) as entries:
            for entry in entries:
                search['entries'] += 1
                require(search['entries'] <= SEARCH_ENTRIES and time.monotonic() <= deadline,
                        'archive_search_limit')
                if entry.name == name:
                    search['candidates'] += 1
                    require(search['candidates'] <= SEARCH_CANDIDATES, 'archive_search_limit')
                    try:
                        remaining = MAX_EXPANDED - search['bytes_reserved']
                        require(remaining > 1, 'archive_search_limit')
                        limit = min(MAX_ARCHIVE, remaining - 1)
                        # Reserve the complete possible read, including the extra
                        # growth-detection byte. Only a successful bounded read
                        # proves unused capacity can be refunded.
                        search['bytes_reserved'] += limit + 1
                        data = archive_bytes(fd, name, limit)
                        search['bytes_reserved'] -= limit + 1 - len(data)
                        require(time.monotonic() <= deadline, 'archive_search_limit')
                        require(search['bytes_reserved'] + 257 <= MAX_EXPANDED,
                                'archive_search_limit')
                        search['bytes_reserved'] += 257
                        checksum_data = archive_bytes(fd, name + '.sha256', 256)
                        search['bytes_reserved'] -= 257 - len(checksum_data)
                        checksum = checksum_data.decode().split()
                        require(time.monotonic() <= deadline, 'archive_search_limit')
                        require(checksum == [sha, name] and archive_digest_before(data, deadline) == sha,
                                'rollback_archive_digest')
                        require(time.monotonic() <= deadline, 'archive_search_limit')
                        return path / name, data
                    except Refused as exc:
                        if str(exc) == 'archive_search_limit':
                            raise
                        candidate_error = str(exc)
                    except (OSError, UnicodeError):
                        candidate_error = 'archive_candidate_unreadable'
                elif layout.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                    if depth >= SEARCH_DEPTH:
                        incomplete = True
                        continue
                    try:
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            found = walk(child, path / entry.name, depth + 1)
                        finally:
                            os.close(child)
                        if found:
                            return found
                    except OSError:
                        incomplete = True
        return None

    for directory in roots:
        try:
            fd = open_directory(directory)
        except FileNotFoundError:
            continue
        except OSError:
            incomplete = True
            continue
        try:
            found = walk(fd, directory, 0)
        finally:
            os.close(fd)
        if found:
            search['matched'] = True
            return found
    search['complete'] = not incomplete
    require(not incomplete, 'archive_search_incomplete')
    raise Refused(candidate_error or 'rollback_archive_not_found')


def deployment_record(root, release, output, *, selected=False):
    short = release.parent.name
    evidence = json.loads(exact_bytes(root / f'deployments/{short}/test-deploy-evidence.json'))
    commit = evidence['commit']
    require(re.fullmatch('[0-9a-f]{40}', commit) is not None and commit.startswith(short), 'release_commit')
    sha = exact_bytes(release / '.release-sha256', 128).decode().strip()
    require(re.fullmatch('[0-9a-f]{64}', sha) is not None and evidence['release_sha256'] == sha, 'release_digest')
    require(evidence['environment'] == 'Test' and evidence['host'] == HOST
            and evidence['release_root'] == str(release), 'deployment_identity')
    output.update(commit=commit, release_root=str(release), sha256=sha)
    if selected:
        previous = evidence.get('previous_release')
        require(isinstance(previous, str) and re.fullmatch(
            re.escape(str(root)) + r'/releases/([0-9a-f]{12})/robie-hermes-\1', previous),
            'previous_release_unverified')
        output['previous_release'] = previous
        runtime = evidence.get('gateway_playwright_runtime') or {}
        require(runtime.get('root') == str(release / '.gateway-runtime')
                and re.fullmatch('[0-9a-f]{64}', str(runtime.get('content_digest', ''))),
                'runtime_metadata_unverified')
        output['recorded_runtime'] = {'root': runtime['root'], 'content_digest': runtime['content_digest']}
        require(evidence.get('proof_path') == str(release / 'official-install-proof.json'), 'proof_path_invalid')
    proof = json.loads(exact_bytes(release / 'official-install-proof.json'))
    flip = json.loads(exact_bytes(release / 'official-install-flip.json'))
    require(proof['sha'] == short and proof['done'] is True and proof['live'] is True
            and proof['authorizes_complete'] is False and proof['proof']['live'] is True,
            'stored_install_proof')
    require(flip['sha'] == short and flip['release_root'] == str(release), 'flip_identity')
    output['flip_at'] = timestamp(flip['flip_at']).isoformat()
    output['stored_proof_live'] = True
    return output


def release_evidence(root, staging, live_gateway, output, *, handover=False):
    release = resolved(root / 'current', root)
    require(release == resolved(root / 'releases/current', root), 'pointers_disagree')
    match = re.fullmatch(r'robie-hermes-([0-9a-f]{12})', release.name)
    require(match is not None and release.parent.parent == root / 'releases', 'release_layout')
    short = match[1]
    require(release.parent.name == short, 'release_directory_sha')
    if handover:
        require(short == HANDOVER_CURRENT[0][:12], 'handover_current_mismatch')
    deployment_record(root, release, output, selected=handover)
    commit, sha = output['commit'], output['sha256']
    if handover:
        require((commit, sha) == HANDOVER_CURRENT, 'handover_current_mismatch')
    require(timestamp(live_gateway['active_enter']) > timestamp(output['flip_at']), 'gateway_predates_flip')
    locate = exact_current_archive if handover else find_rollback_archive
    archive, data = locate(root, staging, commit, sha, output)
    output['rollback_archive'] = str(archive)
    archived = archive_sources(data, short)
    output['rollback_archive_verified'] = True
    overlays = []
    for source, dest, known_shim in OVERLAYS:
        source_sha = digest(exact_bytes(release / source))
        require(source_sha == archived[f'robie-hermes-{short}/{source}'], 'release_source_changed')
        dest_sha = digest(read_file(root / '.hermes' / dest, root))
        require(dest_sha in (source_sha, known_shim), 'overlay_mismatch')
        overlays.append({'source': source, 'destination': dest, 'source_sha256': source_sha,
                         'installed_sha256': dest_sha, 'mode': 'copy' if dest_sha == source_sha else 'reviewed_shim'})
    output['overlays'] = overlays
    return output


def archive_sources(data, short):
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
    return archived


def handover_record(root, expected, output):
    commit, sha = expected
    release = root / 'releases' / commit[:12] / f'robie-hermes-{commit[:12]}'
    deployment_record(root, release, output, selected=True)
    require((output['commit'], output['sha256']) == (commit, sha), 'handover_record_mismatch')
    return output


def older_archive(root, output):
    # Reported candidate only: never fills the current release's rollback result.
    commit, sha = HANDOVER_OLDER
    output.update(path=str(HANDOVER_ARCHIVE), rollback_selected=False,
                  previous_release_suitability='UNVERIFIED')
    data = exact_bytes(HANDOVER_ARCHIVE, MAX_ARCHIVE)
    require(digest(data) == sha, 'older_archive_digest')
    output['sha256'] = sha
    archived = archive_sources(data, commit[:12])
    release = root / 'releases' / commit[:12] / f'robie-hermes-{commit[:12]}'
    for source, _, _ in OVERLAYS:
        require(digest(exact_bytes(release / source)) == archived[f'robie-hermes-{commit[:12]}/{source}'],
                'older_archive_source_changed')
    output['selected_sources_match'] = True
    return output


def collect(root=ROOT, staging=STAGING, *, handover=False):
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
                                                 report['checks'].setdefault('release', {}), handover=handover))
    if handover:
        for name, expected in (('current_deployment_record', HANDOVER_CURRENT),
                               ('older_deployment_record', HANDOVER_OLDER)):
            check(name, lambda: handover_record(root, expected, report['checks'].setdefault(name, {})))
        check('older_reported_archive', lambda: older_archive(
            root, report['checks'].setdefault('older_reported_archive', {})))
    skill = check('policy_skill', lambda: policy(root))
    jobs = check('durable_work', lambda: database(root, Path(release['path']))) if release else None
    if jobs and not jobs['clear']:
        report['errors'].extend(jobs['blocking_codes'])
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-handover', action='store_true')
    args = parser.parse_args(argv)
    def timeout(*_):
        raise CollectorTimeout()
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(120)
    try:
        report = collect(handover=args.release_handover)
    except CollectorTimeout:
        report = {'schema': 1, 'snapshot_verified': False, 'deployment_authorized': False,
                  'errors': ['collector_timeout']}
    finally:
        signal.alarm(0)
    print(json.dumps(report, sort_keys=True))
    return 0 if report['snapshot_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
