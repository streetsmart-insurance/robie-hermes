import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from robie_job_engine.server_daily_report import build_report, render_report


class ServerDailyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'jobs.db'
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE jobs(id TEXT, action_type TEXT, status TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE verification_evidence(id INTEGER PRIMARY KEY, job_id TEXT, verified INT,
                    authoritative INT, captured_at TEXT, created_at TEXT);
                CREATE TABLE attempts(job_id TEXT, phase TEXT, outcome TEXT, created_at TEXT);
                CREATE TABLE checkpoints(job_id TEXT, kind TEXT, data_json TEXT);
            ''')
        self.day = date(2026, 9, 26)
        self.now = '2026-09-26T12:00:00+00:00'

    def add_job(self, jid, status='COMPLETE', when=None):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
                       (jid, 'intake.certificates', status, when or self.now, when or self.now))

    def evidence(self, jid, verified=1, authoritative=1, when=None):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO verification_evidence(job_id,verified,authoritative,captured_at,created_at) '
                       'VALUES (?,?,?,?,?)', (jid, verified, authoritative, when or self.now, when or self.now))

    def test_completion_requires_latest_authoritative_evidence(self):
        for name in ['good', 'receipt', 'later_failure', 'unauthoritative']:
            self.add_job(name)
        self.evidence('good')
        self.evidence('later_failure')
        self.evidence('later_failure', verified=0)
        self.evidence('unauthoritative', authoritative=0)
        report = build_report(self.path, self.day)
        self.assertEqual(report['verified_jobs_with_evidence_recorded_today'], {'intake.certificates': 1})
        self.assertEqual(set(report['complete_without_current_authoritative_proof']),
                         {'receipt', 'later_failure', 'unauthoritative'})

    def test_repeated_evidence_counts_job_once_and_no_private_fields(self):
        self.add_job('one')
        self.evidence('one')
        self.evidence('one')
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO checkpoints VALUES (?,?,?)', ('one', 'action', json.dumps({
                'detail': {'intake_disposition': 'existing_task_reused', 'source_artifact': '/private/client'},
                'destination': {'manual_upload_required': True, 'description': 'PRIVATE BODY'}})))
        report = build_report(self.path, self.day)
        self.assertEqual(report['existing_intake_tasks_reused'], 1)
        self.assertEqual(report['manual_upload_instruction_recorded_jobs'], ['one'])
        self.assertEqual(sum(report['verified_jobs_with_evidence_recorded_today'].values()), 1)
        self.assertNotIn('PRIVATE BODY', json.dumps(report))
        self.assertNotIn('/private/client', json.dumps(report))

    def test_older_backlog_is_included_but_not_daily_activity(self):
        self.add_job('old', 'NEEDS_AUTH', '2026-09-20T12:00:00Z')
        report = build_report(self.path, self.day)
        self.assertEqual(report['jobs_with_activity'], 0)
        self.assertEqual(report['current_open_work'][0]['job_id'], 'old')
        self.assertIn('not a healthy-server confirmation', render_report(report))
        self.assertEqual(set(report['mailbox_coverage'].values()), {'UNVERIFIED'})

    def test_midnight_boundaries_offsets_and_dst(self):
        self.add_job('before', when='2026-09-26T03:59:59Z')
        self.add_job('start', when='2026-09-26T00:00:00-04:00')
        self.add_job('last', when='2026-09-27T03:59:59Z')
        self.add_job('next', when='2026-09-27T04:00:00Z')
        self.assertEqual({r['job_id'] for r in build_report(self.path, self.day)['jobs']}, {'start', 'last'})
        spring = build_report(self.path, date(2026, 3, 8))
        self.assertEqual(spring['window_start'], '2026-03-08T05:00:00+00:00')
        self.assertEqual(spring['window_end_exclusive'], '2026-03-09T04:00:00+00:00')
        fall = build_report(self.path, date(2026, 11, 1))
        self.assertEqual(fall['window_end_exclusive'], '2026-11-02T05:00:00+00:00')

    def test_attempt_in_window_is_visible_after_later_job_update(self):
        self.add_job('later', 'FAILED', '2026-09-28T12:00:00Z')
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO attempts VALUES (?,?,?,?)', ('later', 'perform', 'failure', self.now))
        report = build_report(self.path, self.day)
        self.assertEqual(report['jobs_with_activity'], 1)
        self.assertEqual(report['failed_attempts_in_window'], 1)

    def test_later_success_is_not_counted_in_earlier_day(self):
        self.add_job('later-proof')
        self.evidence('later-proof', verified=0)
        self.evidence('later-proof', when='2026-09-27T12:00:00Z')
        report = build_report(self.path, self.day)
        self.assertEqual(report['verified_jobs_with_evidence_recorded_today'], {})

    def test_read_only_and_missing_database_never_becomes_empty_success(self):
        before = self.path.read_bytes()
        build_report(self.path, self.day)
        self.assertEqual(before, self.path.read_bytes())
        missing = self.path.parent / 'missing.db'
        with self.assertRaises(FileNotFoundError):
            build_report(missing, self.day)
        self.assertFalse(missing.exists())

    def test_invalid_timestamp_is_not_silently_ignored(self):
        self.add_job('bad', when='2026-09-26T12:00:00')
        with self.assertRaises(ValueError):
            build_report(self.path, self.day)


if __name__ == '__main__':
    unittest.main()
