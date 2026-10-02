"""Fixture-only diagnostics: no cloud, systemd, credentials, or client systems."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch, Mock

from scripts import diagnose_test_release_readonly as audit
from robie_job_engine.deploy_truth import CHAT_RUNTIME_FILES, zip_load_shim_source


class TestReadonlyRelease(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'test-root'
        self.staging = Path(self.tmp.name) / 'staging'
        self.commit = 'a' * 40
        self.short = self.commit[:12]
        self.release = self.root / 'releases' / self.short / f'robie-hermes-{self.short}'
        self.release.mkdir(parents=True)
        (self.root / 'current').symlink_to(self.release)
        (self.root / 'releases/current').symlink_to(self.release)
        for source, dest, _ in audit.OVERLAYS:
            self.write(self.release / source, b'# synthetic source\n')
            self.write(self.root / '.hermes' / dest, b'# synthetic source\n')
        self.archive = self.staging / self.commit / f'robie-hermes-{self.short}.tgz'
        self.archive.parent.mkdir(parents=True)
        with tarfile.open(self.archive, 'w:gz') as bundle:
            for source, _, _ in audit.OVERLAYS:
                bundle.add(self.release / source, arcname=f'robie-hermes-{self.short}/{source}')
        sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.write(Path(str(self.archive) + '.sha256'), f'{sha}  {self.archive.name}\n'.encode())
        self.write(self.release / '.release-sha256', sha.encode())
        self.evidence = self.root / f'deployments/{self.short}/test-deploy-evidence.json'
        self.write_json(self.evidence, {'commit': self.commit, 'release_sha256': sha,
                        'environment': 'Test', 'host': audit.HOST, 'release_root': str(self.release),
                        'ignored_payload': 'SECRET_SENTINEL'})
        self.write_json(self.release / 'official-install-proof.json',
                        {'sha': self.short, 'done': True, 'live': True,
                         'authorizes_complete': False, 'proof': {'live': True}})
        self.write_json(self.release / 'official-install-flip.json',
                        {'sha': self.short, 'release_root': str(self.release), 'flip_at': '2026-01-01T00:00:00Z'})
        self.skill = self.release / 'deploy/hermes/skills/ezlynx-policy-setup'
        for name in audit.POLICY_FILES:
            self.write(self.skill / name, b'fixture skill')
        self.skill_link = self.root / '.hermes/skills/ezlynx-policy-setup'
        self.skill_link.parent.mkdir(parents=True)
        self.skill_link.symlink_to(self.skill)
        self.db = self.root / 'robie-job-engine/data/jobs.db'
        self.db.parent.mkdir(parents=True)
        with sqlite3.connect(self.db) as conn:
            conn.executescript('''CREATE TABLE jobs(status TEXT,lease_owner TEXT,payload_json TEXT);
                CREATE TABLE chat_event_queue(state TEXT,lease_owner TEXT,payload_json TEXT);
                CREATE TABLE conversation_job_links(active INTEGER,interaction_state_json TEXT);
                INSERT INTO jobs VALUES ('COMPLETE',NULL,'SECRET_SENTINEL');''')
        self.gateway = {'pid': 100, 'active_enter': '2026-01-02T00:00:00+00:00'}
        self.driver = {'state': 'OUT', 'holder': 'NONE', 'expires_at': '2026-01-01T00:00:00+00:00',
                       'updated_at': '2026-01-01T00:00:00+00:00', 'clear': True}
        for target, value in [('socket.gethostname', audit.HOST), ('gateway', self.gateway), ('driver', self.driver)]:
            mocked = patch.object(audit.socket, 'gethostname', return_value=value) if target.startswith('socket') else patch.object(audit, target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write_json(self, path, data):
        self.write(path, json.dumps(data).encode())

    def collect(self):
        return audit.collect(self.root, self.staging)

    def refused(self, code):
        report = self.collect()
        self.assertFalse(report['snapshot_verified'], report)
        self.assertIn(code, report['errors'])
        self.assertNotIn('SECRET_SENTINEL', json.dumps(report))

    def test_clean_snapshot_is_readonly_redacted_and_not_deploy_authorization(self):
        before = {str(p): p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file()}
        report = self.collect()
        self.assertTrue(report['snapshot_verified'], report)
        self.assertFalse(report['deployment_authorized'])
        self.assertEqual(report['loaded_process_modules'], 'UNVERIFIED')
        self.assertIsNone(report['checks']['durable_work']['reply_outbox_by_state'])
        self.assertNotIn('SECRET_SENTINEL', json.dumps(report))
        after = {str(p): p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_wrong_host_reads_nothing(self):
        audit.socket.gethostname.return_value = 'hermes-poc-01'
        self.refused('wrong_host')
        audit.gateway.assert_not_called()
        audit.driver.assert_not_called()

    def test_gateway_failure_does_not_leak_exception_message(self):
        audit.gateway.side_effect = RuntimeError('SECRET_SENTINEL')
        self.refused('RuntimeError')

    def test_missing_database_is_not_created(self):
        self.db.unlink()
        self.refused('path_missing')
        self.assertFalse(self.db.exists())

    def test_missing_schema_fails_closed(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('DROP TABLE chat_event_queue')
        self.refused('OperationalError')

    def test_pending_job_blocks_without_reading_payload(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO jobs VALUES ('PENDING',NULL,'SECRET_SENTINEL')")
        self.refused('durable_work_not_quiescent')

    def test_terminal_job_lease_blocks(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE jobs SET lease_owner='SECRET_SENTINEL'")
        self.refused('durable_work_not_quiescent')

    def test_queued_intake_and_active_link_block(self):
        for sql in ("INSERT INTO chat_event_queue VALUES ('QUEUED',NULL,'SECRET_SENTINEL')",
                    "INSERT INTO conversation_job_links VALUES (1,'SECRET_SENTINEL')"):
            with self.subTest(sql=sql):
                with sqlite3.connect(self.db) as conn:
                    conn.execute(sql)
                self.refused('durable_work_not_quiescent')

    def test_outbox_expected_but_missing_fails(self):
        self.write(self.release / 'robie_job_engine/chat_reply_outbox.py', b'# installed')
        self.refused('reply_schema_missing')

    def test_pending_reply_blocks_delivered_reply_does_not(self):
        with sqlite3.connect(self.db) as conn:
            conn.executescript("CREATE TABLE chat_reply_outbox(state TEXT,bodies_json TEXT); INSERT INTO chat_reply_outbox VALUES ('pending','SECRET_SENTINEL');")
        self.refused('durable_work_not_quiescent')
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE chat_reply_outbox SET state='delivered'")
        self.assertTrue(self.collect()['snapshot_verified'])

    def test_unknown_status_is_not_echoed(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE jobs SET status='SECRET_SENTINEL'")
        self.refused('job_status_unknown')

    def test_wal_reads_committed_jobs_without_logical_writes_or_new_files(self):
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO jobs VALUES ('RUNNING',NULL,'SECRET_SENTINEL')")
        writer.commit()
        before = {p.name: p.read_bytes() for p in self.db.parent.iterdir()}
        schema = writer.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
        statements = []
        real_connect = sqlite3.connect

        def inspected_connect(filename, **kwargs):
            self.assertTrue(filename.endswith('?mode=ro'))
            conn = real_connect(filename, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(audit, 'sqlite3', wraps=sqlite3) as collector_sqlite:
            collector_sqlite.connect.side_effect = inspected_connect
            report = self.collect()
        self.assertIn('durable_work_not_quiescent', report['errors'])
        self.assertEqual(report['checks']['durable_work']['jobs_by_status']['RUNNING'], 1)
        self.assertNotIn('SECRET_SENTINEL', json.dumps(report))
        after = {p.name: p.read_bytes() for p in self.db.parent.iterdir()}
        self.assertEqual(set(before), set(after))
        for name in before:
            if not name.endswith('-shm'):  # Authorized SQLite reader coordination only.
                self.assertEqual(before[name], after[name])
        self.assertEqual(schema, writer.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall())
        self.assertIn('PRAGMA query_only=ON', statements)
        self.assertIn('BEGIN', statements)
        self.assertTrue(all(s.startswith(('SELECT ', 'PRAGMA ', 'BEGIN')) for s in statements))

    def test_wal_missing_sidecars_refuses_before_connect_without_creating_files(self):
        writer = sqlite3.connect(self.db)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.close()
        for present in (None, '-wal', '-shm'):
            with self.subTest(present=present):
                if present:
                    Path(str(self.db) + present).touch()
                try:
                    before = {p.name: p.read_bytes() for p in self.db.parent.iterdir()}
                    with patch.object(audit, 'sqlite3', wraps=sqlite3) as collector_sqlite:
                        self.refused('wal_sidecars_missing')
                        collector_sqlite.connect.assert_not_called()
                    self.assertEqual(before, {p.name: p.read_bytes() for p in self.db.parent.iterdir()})
                finally:
                    if present:
                        Path(str(self.db) + present).unlink()

    def test_wal_read_transaction_keeps_counts_consistent_during_writer_commit(self):
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO jobs VALUES ('COMPLETE',NULL,'SECRET_SENTINEL')")
        writer.commit()

        class SnapshotConnection(sqlite3.Connection):
            def execute(conn, sql, *args):
                cursor = super().execute(sql, *args)
                if sql.startswith('SELECT CASE WHEN status IN'):
                    writer.execute("INSERT INTO jobs VALUES ('RUNNING',NULL,'SECRET_SENTINEL')")
                    writer.commit()
                return cursor

        real_connect = sqlite3.connect
        with patch.object(audit, 'sqlite3', wraps=sqlite3) as collector_sqlite:
            collector_sqlite.connect.side_effect = lambda filename, **kw: real_connect(filename, factory=SnapshotConnection, **kw)
            result = audit.database(self.root, self.release)
        self.assertEqual(result['jobs_by_status'], {'COMPLETE': 2})
        self.assertEqual(result['jobs_with_active_status_or_lease'], 0)
        self.assertTrue(result['clear'])
        self.assertEqual(writer.execute("SELECT COUNT(*) FROM jobs WHERE status='RUNNING'").fetchone()[0], 1)

    def test_wal_redirected_or_nonregular_sidecar_refuses_before_connect(self):
        writer = sqlite3.connect(self.db)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.close()
        Path(str(self.db) + '-wal').touch()
        shm = Path(str(self.db) + '-shm')
        for kind in ('symlink', 'fifo', 'directory'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    shm.symlink_to(self.db)
                elif kind == 'fifo':
                    os.mkfifo(shm)
                else:
                    shm.mkdir()
                try:
                    # Heartbeats use the same sqlite3 module in full discovery.
                    # Replace only the collector's binding, never that shared module.
                    with patch.object(audit, 'sqlite3', wraps=sqlite3) as collector_sqlite:
                        def background_read():
                            conn = sqlite3.connect(':memory:')
                            try:
                                return conn.execute('SELECT 1').fetchone()
                            finally:
                                conn.close()

                        with ThreadPoolExecutor(max_workers=1) as pool:
                            self.assertEqual(pool.submit(background_read).result(timeout=5), (1,))
                        self.refused('wal_sidecars_invalid')
                        collector_sqlite.connect.assert_not_called()
                finally:
                    shm.rmdir() if kind == 'directory' else shm.unlink()

    def test_missing_archive_fails(self):
        self.archive.unlink()
        self.refused('rollback_archive_not_found')
        report = self.collect()
        self.assertEqual(report['checks']['release']['commit'], self.commit)
        self.assertIn('policy_skill', report['checks'])
        self.assertIn('durable_work', report['checks'])

    def test_unknown_and_null_states_preserve_all_inventory_without_echo(self):
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                INSERT INTO jobs VALUES ('SECRET_SENTINEL',NULL,'unused');
                INSERT INTO jobs VALUES (NULL,NULL,'unused');
                INSERT INTO jobs VALUES ('RUNNING','SECRET_SENTINEL','unused');
                INSERT INTO chat_event_queue VALUES ('SECRET_SENTINEL',NULL,'unused');
                INSERT INTO conversation_job_links VALUES (1,'unused');
                CREATE TABLE chat_reply_outbox(state TEXT,bodies_json TEXT);
                INSERT INTO chat_reply_outbox VALUES (NULL,'SECRET_SENTINEL');
            """)
        self.archive.unlink()
        report = self.collect()
        work = report['checks']['durable_work']
        self.assertEqual(work['jobs_by_status'], {'COMPLETE': 1, 'RUNNING': 1, 'UNKNOWN': 2})
        self.assertEqual(work['jobs_with_active_status_or_lease'], 1)
        self.assertEqual(work['active_conversation_links'], 1)
        self.assertEqual(work['queue_by_state'], {'UNKNOWN': 1})
        self.assertEqual(work['reply_outbox_by_state'], {'UNKNOWN': 1})
        self.assertTrue({'job_status_unknown', 'queue_state_unknown', 'reply_state_unknown',
                         'rollback_archive_not_found'} <= set(report['errors']))
        self.assertIn('policy_skill', report['checks'])
        self.assertNotIn('SECRET_SENTINEL', json.dumps(report))
        self.assertFalse(report['snapshot_verified'])

    def test_cancelled_is_reported_but_not_certified_terminal(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE jobs SET status='CANCELLED'")
        report = self.collect()
        work = report['checks']['durable_work']
        self.assertEqual(work['jobs_by_status'], {'CANCELLED': 1})
        self.assertEqual(work['cancelled_semantics'], 'UNVERIFIED')
        self.assertEqual(work['nonterminal_jobs'], 1)
        self.assertIn('cancelled_semantics_unverified', report['errors'])
        self.assertFalse(work['clear'])

    def move_archive(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / self.archive.name
        self.archive.rename(destination)
        Path(str(self.archive) + '.sha256').rename(Path(str(destination) + '.sha256'))
        self.archive = destination

    def test_alternate_archive_in_each_approved_root_verifies_exact_bytes(self):
        for directory in (self.staging / 'retained', self.root / 'deployments' / self.short,
                          self.root / 'releases' / self.short / 'archives'):
            with self.subTest(directory=directory):
                self.move_archive(directory)
                report = self.collect()
                self.assertTrue(report['snapshot_verified'], report)
                self.assertEqual(report['checks']['release']['rollback_archive'], str(self.archive))

    def test_archive_and_checksum_symlinks_and_fifo_are_never_read(self):
        original = self.archive.read_bytes()
        checksum = Path(str(self.archive) + '.sha256')
        for target in (self.archive, checksum):
            data = target.read_bytes()
            external = Path(self.tmp.name) / 'outside'
            external.write_bytes(data)
            for kind in ('symlink', 'fifo'):
                with self.subTest(target=target.name, kind=kind):
                    target.unlink()
                    if kind == 'symlink':
                        target.symlink_to(external)
                    else:
                        os.mkfifo(target)
                    report = self.collect()
                    self.assertFalse(report['snapshot_verified'])
                    self.assertFalse(report['checks']['release'].get('rollback_archive_verified', False))
                    self.assertIn('durable_work', report['checks'])
                    target.unlink()
                    target.write_bytes(data)
        self.assertEqual(self.archive.read_bytes(), original)

    def test_search_never_enters_symlink_or_source_runtime_home_directories(self):
        self.move_archive(Path(self.tmp.name) / 'outside')
        (self.staging / 'retained').symlink_to(self.archive.parent, target_is_directory=True)
        for name in ('.hermes', 'home', 'robie-job-engine', 'data', 'secrets', 'src'):
            (self.staging / name).mkdir()
            (self.staging / name / self.archive.name).write_bytes(b'SECRET_SENTINEL')
        opened = []
        real_open = audit.os.open
        def guarded_open(path, *args, **kwargs):
            opened.append(str(path))
            self.assertNotIn(str(path), {'.hermes', 'home', 'robie-job-engine', 'data', 'secrets', 'src', 'retained'})
            return real_open(path, *args, **kwargs)
        # Observe only archive search; policy inspection intentionally reads its own fixed files.
        with patch.object(audit.os, 'open', side_effect=guarded_open):
            with self.assertRaisesRegex(audit.Refused, 'rollback_archive_not_found'):
                audit.find_rollback_archive(self.root, self.staging, self.commit, '0' * 64, {})

    def test_search_root_symlink_is_not_followed(self):
        self.move_archive(Path(self.tmp.name) / 'outside')
        import shutil
        shutil.rmtree(self.staging)
        self.staging.symlink_to(self.archive.parent, target_is_directory=True)
        self.refused('archive_search_incomplete')

    def test_checksum_mismatch_is_not_rollback_proof(self):
        Path(str(self.archive) + '.sha256').write_text('0' * 64 + '  ' + self.archive.name)
        self.refused('rollback_archive_digest')

    def test_checksum_must_name_the_exact_archive(self):
        sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        Path(str(self.archive) + '.sha256').write_text(sha + '  different.tgz')
        self.refused('rollback_archive_digest')

    def test_oversized_archive_is_rejected_before_read(self):
        with patch.object(audit, 'MAX_ARCHIVE', 1):
            self.refused('archive_file_type_or_size')

    def test_intermediate_root_symlink_refused(self):
        alias = Path(self.tmp.name) / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            audit.open_directory(alias / 'deployments')

    def test_directory_replaced_by_symlink_between_scan_and_open_is_refused(self):
        import shutil
        moved = Path(self.tmp.name) / 'outside'
        moved.mkdir()
        original_open = audit.os.open
        swapped = False
        def swap_before_open(path, *args, **kwargs):
            nonlocal swapped
            if str(path) == self.commit and not swapped:
                swapped = True
                directory = self.staging / self.commit
                shutil.move(str(directory), str(moved / self.commit))
                directory.symlink_to(moved / self.commit, target_is_directory=True)
            return original_open(path, *args, **kwargs)
        with patch.object(audit.os, 'open', side_effect=swap_before_open):
            self.refused('archive_search_incomplete')
        self.assertTrue(swapped)

    def test_search_limits_fail_closed_and_preserve_independent_checks(self):
        for setting, value in (('SEARCH_ENTRIES', 0), ('SEARCH_CANDIDATES', 0),
                               ('SEARCH_SECONDS', -1), ('SEARCH_DEPTH', 0)):
            with self.subTest(setting=setting), patch.object(audit, setting, value):
                report = self.collect()
                self.assertFalse(report['snapshot_verified'])
                self.assertTrue({'archive_search_limit', 'archive_search_incomplete'} & set(report['errors']))
                self.assertIn('durable_work', report['checks'])
                self.assertIn('policy_skill', report['checks'])

    def test_driver_conflict_still_collects_other_evidence(self):
        self.driver.update(state='IN', holder='PRODUCTION', clear=False)
        self.refused('driver_conflict_or_expired')
        self.assertIn('release', self.collect()['checks'])

    def test_duplicate_or_missing_archive_member_fails(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                with tarfile.open(self.archive, 'w:gz') as bundle:
                    source = audit.OVERLAYS[0][0]
                    for _ in range(2 if duplicate else 1):
                        bundle.add(self.release / source, arcname=f'robie-hermes-{self.short}/{source}')
                sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
                self.write(self.release / '.release-sha256', sha.encode())
                self.write(Path(str(self.archive) + '.sha256'), f'{sha}  {self.archive.name}\n'.encode())
                evidence = json.loads(self.evidence.read_text())
                evidence['release_sha256'] = sha
                self.write_json(self.evidence, evidence)
                self.refused('archive_member_invalid' if duplicate else 'archive_sources_missing')

    def test_tampered_archive_fails(self):
        with self.archive.open('ab') as stream:
            stream.write(b'tamper')
        self.refused('rollback_archive_digest')

    def test_changed_source_or_overlay_fails(self):
        source, dest, _ = audit.OVERLAYS[0]
        self.write(self.release / source, b'tamper')
        self.refused('release_source_changed')
        self.write(self.release / source, b'# synthetic source\n')
        self.write(self.root / '.hermes' / dest, b'tamper')
        self.refused('overlay_mismatch')

    def test_reviewed_shim_hashes_match_repository_contract(self):
        expected = [(x.zip_relpath,x.dest_relpath,hashlib.sha256(zip_load_shim_source(x.zip_relpath).encode()).hexdigest()) for x in CHAT_RUNTIME_FILES]
        self.assertEqual(list(audit.OVERLAYS), expected)
        for source, dest, _ in audit.OVERLAYS:
            self.write(self.root / '.hermes' / dest, zip_load_shim_source(source).encode())
        report = self.collect()
        self.assertTrue(report['snapshot_verified'], report)
        self.assertEqual(report['loaded_process_modules'], 'UNVERIFIED')

    def test_floating_policy_and_nested_file_links_fail(self):
        self.skill_link.unlink()
        self.skill_link.symlink_to(self.root / 'releases/current/deploy/hermes/skills/ezlynx-policy-setup')
        self.refused('floating_current_link')
        self.skill_link.unlink()
        self.skill_link.symlink_to(self.skill)
        profile = self.skill / 'references/profiles.json'
        profile.unlink()
        profile.symlink_to(self.root / 'current/integrations/google_chat/adapter.py')
        self.refused('floating_current_link')

    def test_escaped_policy_link_fails_before_read(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.write_text('SECRET_SENTINEL')
        skill = self.skill / 'SKILL.md'
        skill.unlink()
        skill.symlink_to(outside)
        self.refused('path_outside_boundary')

    def test_test_root_cannot_redirect_to_another_installation(self):
        moved = self.root.with_name('different-installation')
        self.root.rename(moved)
        self.root.symlink_to(moved)
        self.refused('boundary_redirected')

    def test_fifo_and_oversize_files_fail_without_hanging(self):
        target = self.skill / 'SKILL.md'
        target.unlink()
        os.mkfifo(target)
        self.refused('file_type_or_size')
        target.unlink()
        with target.open('wb') as stream:
            stream.truncate(audit.MAX_FILE + 1)
        self.refused('file_type_or_size')

    def test_changed_gateway_during_snapshot_fails(self):
        audit.gateway.side_effect = [self.gateway, dict(self.gateway, pid=101)]
        self.refused('snapshot_changed')

    def test_stale_or_wrong_install_proof_fails(self):
        self.gateway['active_enter'] = '2025-01-01T00:00:00+00:00'
        self.refused('gateway_predates_flip')


class TestTransportContracts(unittest.TestCase):
    def test_oslogin_instance_override_and_access_failure_stop_before_key(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/diagnose-test-gateway-and-browser.yml').read_text())
        steps = workflow['jobs']['diagnose']['steps']
        guard = next(s for s in steps if s.get('name') == 'Require effective OS Login before publishing any key')
        key_step = next(s for s in steps if s.get('name') == 'Prepare approved expiring OS Login key')
        self.assertLess(steps.index(guard), steps.index(key_step))
        for instance, project, access_code, expected in (
                ('enable-oslogin\tTRUE','enable-oslogin\tFALSE',0,0),
                ('enable-oslogin\tFALSE','enable-oslogin\tTRUE',0,2),
                ('','enable-oslogin\ttrue',0,0),
                ('\t','enable-oslogin\tTRUE',0,0),
                ('enable-oslogin\t','enable-oslogin\tTRUE',0,2),
                ('enable-oslogin','enable-oslogin\tTRUE',0,2),
                ('','',0,2), ('','SECRET_SENTINEL',0,2),
                ('enable-oslogin\tTRUE','enable-oslogin\tTRUE',41,41)):
            with self.subTest(instance=instance, project=project, access_code=access_code), tempfile.TemporaryDirectory() as directory:
                fake = Path(directory) / 'gcloud'
                fake.write_text('#!/bin/bash\n[ "$ACCESS_CODE" -eq 0 ] || exit "$ACCESS_CODE"\nif [[ "$*" == *"instances describe"* ]]; then printf "%s" "$INSTANCE_OSLOGIN"; else printf "%s" "$PROJECT_OSLOGIN"; fi\n')
                fake.chmod(0o755)
                result = subprocess.run(['bash','-c',guard['run']], capture_output=True, text=True,
                    env={'PATH':f'{directory}:/usr/bin:/bin','TEST_VM':audit.HOST,
                         'PROJECT_ID':'streetsmart-hermes-poc','ZONE':'us-east1-b',
                         'INSTANCE_OSLOGIN':instance,'PROJECT_OSLOGIN':project,'ACCESS_CODE':str(access_code)})
                self.assertEqual(result.returncode, expected)
                self.assertNotIn('SECRET_SENTINEL', result.stdout + result.stderr)

    def test_workflow_ssh_and_json_failures_cannot_turn_green(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/diagnose-test-gateway-and-browser.yml').read_text())
        command = next(s['run'] for s in workflow['jobs']['diagnose']['steps']
                       if s.get('name') == 'Collect bounded Test snapshot without remote installation')
        for ssh_exit, payload in ((47, ''), (0, 'invalid json'), (0, json.dumps({
                'host': audit.HOST, 'snapshot_verified': False, 'deployment_authorized': False,
                'errors': ['wrong_host']}))):
            with self.subTest(ssh_exit=ssh_exit, payload=payload), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                fake = base / 'gcloud'
                fake.write_text('#!/bin/bash\ncat >/dev/null\nprintf "%s" "$FIXTURE_PAYLOAD"\nexit "$FIXTURE_EXIT"\n')
                fake.chmod(0o755)
                (base / 'python3').symlink_to(__import__('sys').executable)
                result = subprocess.run(['bash','-c',command], cwd=root, capture_output=True, text=True,
                    env={'PATH':f'{base}:/usr/bin:/bin','HOME':directory,'RUNNER_TEMP':directory,
                         'GITHUB_SHA':'a'*40,'TEST_VM':audit.HOST,'PROJECT_ID':'streetsmart-hermes-poc',
                         'ZONE':'us-east1-b','OSLOGIN_SSH_KEY_TTL':'1h',
                         'FIXTURE_PAYLOAD':payload,'FIXTURE_EXIT':str(ssh_exit)})
                self.assertNotEqual(result.returncode, 0)
                if ssh_exit:
                    self.assertEqual(result.returncode, ssh_exit)

    def test_timeout_exits_nonzero_without_raw_exception(self):
        with patch.object(audit, 'collect', side_effect=audit.CollectorTimeout), \
             patch.object(audit.signal, 'signal'), patch.object(audit.signal, 'alarm'), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(audit.main(), 2)
            self.assertEqual(json.loads(output.getvalue())['errors'], ['collector_timeout'])

    def test_selected_systemd_fields_only(self):
        with patch.object(audit.subprocess, 'run', return_value=Mock(stdout='LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=42\nActiveEnterTimestamp=Fri 2026-01-02 00:00:00 UTC\n')) as run:
            self.assertEqual(audit.gateway()['pid'], 42)
            args = run.call_args.args[0]
            self.assertNotIn('Environment', args)
            self.assertNotIn('ExecStart', args)
            self.assertTrue(run.call_args.kwargs['check'])

    def test_metadata_is_fixed_bounded_no_redirect_and_no_values_leak(self):
        for state, holder, expiry, error in [('IN','PRODUCTION','2099-01-01T00:00:00Z','driver_conflict_or_expired'),
                                             ('IN','TEST','2000-01-01T00:00:00Z','driver_conflict_or_expired'),
                                             ('OUT','NONE','2000-01-01T00:00:00Z',None)]:
            with self.subTest(holder=holder, expiry=expiry), patch.object(audit, 'HTTPConnection') as http:
                response = http.return_value.getresponse.return_value
                response.status = 200
                response.getheader.return_value = 'Google'
                response.read.return_value = json.dumps({'state':state,'holder':holder,'expires_at':expiry,'updated_at':'2000-01-01T00:00:00Z','secret':'SECRET_SENTINEL'}).encode()
                if error:
                    self.assertFalse(audit.driver()['clear'])
                else:
                    self.assertNotIn('SECRET_SENTINEL', json.dumps(audit.driver()))
                http.assert_called_once_with('metadata.google.internal', timeout=5)
                self.assertEqual(http.return_value.request.call_args.args[1], '/computeMetadata/v1/project/attributes/robie-ezlynx-driver')
                response.read.assert_called_once_with(4097)

    def test_workflow_keeps_existing_identity_and_fails_ssh_closed(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        text = (root / '.github/workflows/diagnose-test-gateway-and-browser.yml').read_text()
        workflow = yaml.safe_load(text)
        self.assertEqual(workflow['permissions'], {'contents':'read','id-token':'write'})
        job = workflow['jobs']['diagnose']
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        self.assertIn('inputs.temporary_ssh_key_approved == true', job['if'])
        self.assertEqual(job['env']['OSLOGIN_SSH_KEY_TTL'], '1h')
        self.assertIn('--ssh-key-expire-after="${OSLOGIN_SSH_KEY_TTL}"', text)
        auth = next(s for s in job['steps'] if s.get('uses','').startswith('google-github-actions/auth@'))
        self.assertEqual(auth['with']['service_account'], 'robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com')
        for forbidden in ('systemctl cat','pgrep -fa','exit 0','compute scp','systemctl restart','secrets:'):
            self.assertNotIn(forbidden, text)
        for step in job['steps']:
            if 'run' in step:
                subprocess.run(['bash','-n'],input=step['run'],text=True,check=True)
        self.assertIn('set -euo pipefail', text)
        self.assertIn('python3 -I -B -', text)
        self.assertIn('if: always()', text)


if __name__ == '__main__':
    unittest.main()
