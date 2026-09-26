"""Read-only daily server evidence report. Never sends mail or changes job state.

Uses an explicit local calendar date and a consistent SQLite snapshot. This is
an activity report, not proof that a mailbox listener or the server is healthy.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo


OPEN = {'PENDING', 'NEEDS_SKILL', 'NEEDS_CLARIFICATION', 'NEEDS_AUTH',
        'AWAITING_HUMAN_INPUT', 'WAITING', 'RUNNING', 'VERIFYING', 'RETRY_WAIT', 'PAUSED'}


def _stamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Evidence timestamps must include a timezone')
    return stamp.astimezone(timezone.utc)


def build_report(db_path: str | Path, day: date, *, timezone_name: str = 'America/New_York') -> dict:
    """Report activity in [local midnight, next midnight); no payload/body export.

    Current open work is deliberately a snapshot, including older items. Historical
    state is not reconstructed from mutable jobs.updated_at. Re-running a past day
    may show a newer current backlog, labeled as such.
    """
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    path = Path(db_path).expanduser().resolve(strict=True)
    uri = 'file:' + quote(str(path), safe='/') + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        jobs = conn.execute('SELECT id, action_type, status, created_at, updated_at FROM jobs').fetchall()
        # Read only status/identity, never expected_json, observed_json, or source text.
        evidence = conn.execute('SELECT id, job_id, verified, authoritative, captured_at, created_at '
                                'FROM verification_evidence ORDER BY id').fetchall()
        attempts = conn.execute('SELECT job_id, phase, outcome, created_at FROM attempts').fetchall()
        actions = conn.execute("SELECT job_id, data_json FROM checkpoints WHERE kind='action'").fetchall()

    def in_window(value):
        return start <= _stamp(value) < end

    touched = {j['id'] for j in jobs if in_window(j['created_at']) or in_window(j['updated_at'])}
    touched.update(e['job_id'] for e in evidence if in_window(e['created_at']))
    touched.update(a['job_id'] for a in attempts if in_window(a['created_at']))
    last_evidence = {e['job_id']: e for e in evidence}
    action_details = {}
    for row in actions:
        value = json.loads(row['data_json'])
        if not isinstance(value, dict):
            raise ValueError('Invalid action checkpoint')
        action_details[row['job_id']] = value

    rows = []
    verified = Counter()
    for job in jobs:
        if job['id'] not in touched:
            continue
        proof = last_evidence.get(job['id'])
        proven = bool(job['status'] == 'COMPLETE' and proof and proof['verified'] == 1
                      and proof['authoritative'] == 1)
        # A later failed verification invalidates an older success for this report.
        if proven and in_window(proof['created_at']):
            verified[job['action_type']] += 1
        checkpoint = action_details.get(job['id'], {})
        detail = checkpoint.get('detail') or {}
        destination = checkpoint.get('destination') or {}
        if not isinstance(detail, dict) or not isinstance(destination, dict):
            raise ValueError('Invalid action detail or destination')
        rows.append({
            'job_id': job['id'], 'action_type': job['action_type'],
            'current_status': job['status'], 'destination_verified': proven,
            'intake_disposition': detail.get('intake_disposition')
                if detail.get('intake_disposition') in {'existing_task_reused', 'task_create_requested'} else None,
            'manual_upload_instruction_recorded': destination.get('manual_upload_required') is True,
        })
    backlog = [{'job_id': j['id'], 'action_type': j['action_type'], 'current_status': j['status']}
               for j in jobs if j['status'] in OPEN]
    failures = sum(a['outcome'].casefold() in {'failed', 'failure', 'unverified', 'not_verified', 'error', 'action_outcome_unknown'}
                   for a in attempts if in_window(a['created_at']))
    return {
        'report_date': day.isoformat(), 'timezone': timezone_name,
        'window_start': start.isoformat(), 'window_end_exclusive': end.isoformat(),
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Job Engine evidence only; external processes and mailbox polling are not verified',
        'mailbox_coverage': {'certificates@streetsmart.insurance': 'UNVERIFIED',
                             'hello@streetsmart.insurance': 'UNVERIFIED'},
        'jobs_with_activity': len(rows),
        'current_status_counts_for_active_jobs': dict(Counter(r['current_status'] for r in rows)),
        'verified_jobs_with_evidence_recorded_today': dict(verified),
        'complete_without_current_authoritative_proof': [r['job_id'] for r in rows
            if r['current_status'] == 'COMPLETE' and not r['destination_verified']],
        'failed_attempts_in_window': failures,
        'existing_intake_tasks_reused': sum(r['intake_disposition'] == 'existing_task_reused' for r in rows),
        'manual_upload_instruction_recorded_jobs': [r['job_id'] for r in rows if r['manual_upload_instruction_recorded']],
        'current_open_work': backlog, 'jobs': rows,
        'limitations': ['Job counts are not counts of individual documents or notes.',
                       'Reused tasks count only recorded intake dispositions, not all prevented duplicates.',
                       'Current statuses and open work reflect report generation time.',
                       'Zero recorded activity does not prove the server or inbox listener ran.'],
    }


def render_report(report: dict) -> str:
    lines = [f"ROBIE daily server report — {report['report_date']} ({report['timezone']})",
             report['scope'], '', f"Jobs with recorded activity: {report['jobs_with_activity']}",
             f"Verified jobs with evidence recorded today: {sum(report['verified_jobs_with_evidence_recorded_today'].values())}",
             f"Recorded existing intake tasks reused: {report['existing_intake_tasks_reused']}",
             f"Failed attempts: {report['failed_attempts_in_window']}",
             f"Activity-day jobs with manual-upload instructions (completion unverified): {len(report['manual_upload_instruction_recorded_jobs'])}",
             f"Current open work: {len(report['current_open_work'])}",
             f"Completion claims missing proof: {len(report['complete_without_current_authoritative_proof'])}",
             '', 'Inbox polling coverage: Certificates UNVERIFIED; Hello UNVERIFIED.', '', 'Activity:']
    for row in report['jobs']:
        proof = 'verified' if row['destination_verified'] else 'not verified'
        lines.append(f"- {row['job_id']} | {row['action_type']} | {row['current_status']} | {proof}")
    if not report['jobs']:
        lines.append('- No activity recorded. This is not a healthy-server confirmation.')
    lines.extend(['', 'Current open work (includes older jobs):'])
    lines.extend(f"- {r['job_id']} | {r['action_type']} | {r['current_status']}" for r in report['current_open_work'])
    lines.extend(['', *report['limitations']])
    return '\n'.join(lines) + '\n'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--date', required=True, type=date.fromisoformat)
    parser.add_argument('--timezone', default='America/New_York')
    parser.add_argument('--format', choices=('json', 'text'), default='text')
    args = parser.parse_args(argv)
    try:
        report = build_report(args.db, args.date, timezone_name=args.timezone)
    except (OSError, ValueError, KeyError, sqlite3.Error):
        # No exception bodies: paths/driver responses can contain sensitive data.
        parser.exit(2, 'Daily report UNVERIFIED: evidence database or report inputs unavailable/invalid.\n')
    print(json.dumps(report, indent=2) if args.format == 'json' else render_report(report), end='\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
