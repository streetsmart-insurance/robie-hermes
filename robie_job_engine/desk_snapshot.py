"""Operator-only read-only task desk snapshot. No approval or execution endpoint.

This is a backend foundation, not a deployed dashboard. Raw requests, note
text, checkpoint bodies, tokens, emails and client names never enter output.
"""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote


def snapshot(db_path: str | Path, *, limit: int = 50) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('limit must be between 1 and 200')
    path = Path(db_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('An existing job database is required')
    # Do not instantiate JobStore: that constructor creates/migrates tables.
    conn = sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        # One read transaction keeps counts and detail rows consistent.
        conn.execute('BEGIN')
        counts = {r['status']: r['n'] for r in conn.execute('SELECT status,count(*) n FROM jobs GROUP BY status')}
        completion = conn.execute("""SELECT count(*) AS complete_jobs,
            sum(CASE WHEN e.verified=1 AND e.authoritative=1 THEN 1 ELSE 0 END) AS with_evidence
            FROM jobs j LEFT JOIN verification_evidence e ON e.id=(
                SELECT max(v.id) FROM verification_evidence v WHERE v.job_id=j.id)
            WHERE j.status='COMPLETE'""").fetchone()
        complete_jobs = int(completion['complete_jobs'])
        complete_with_evidence = int(completion['with_evidence'] or 0)
        attention = {status: counts.get(status, 0) for status in (
            'AWAITING_HUMAN_INPUT', 'NEEDS_CLARIFICATION', 'NEEDS_AUTH',
            'NEEDS_SKILL', 'PAUSED', 'FAILED', 'UNVERIFIED')}
        rows = conn.execute('''SELECT id,action_type,status,attempt_count,
            verification_count,created_at,updated_at,next_wakeup_at,
            CASE WHEN lease_owner IS NULL THEN 0 ELSE 1 END leased
            FROM jobs ORDER BY updated_at DESC,id LIMIT ?''', (limit,)).fetchall()
        items = []
        for row in rows:
            job_id = row['id']
            kinds = {r['kind'] for r in conn.execute('SELECT kind FROM checkpoints WHERE job_id=?', (job_id,))}
            evidence = conn.execute('''SELECT verified,authoritative FROM verification_evidence
                WHERE job_id=? ORDER BY id DESC LIMIT 1''', (job_id,)).fetchone()
            # Evidence is described, never used here to transition a job.
            state = 'verified' if evidence and evidence['verified'] == 1 and evidence['authoritative'] == 1 else 'unverified' if evidence else 'no_evidence'
            item = dict(row)
            item['leased'] = bool(item['leased'])
            item['approval_pending'] = row['status'] == 'AWAITING_HUMAN_INPUT'
            item['confirmation_present'] = 'playground_confirmation' in kinds
            item['source_checkpoint_present'] = bool(kinds & {'action', 'destination', 'source'})
            item['verification'] = state
            item['approval_action_available'] = False
            items.append(item)
        conn.rollback()
        return {'read_only': True, 'execution_available': False,
                'approval_available': False, 'counts_by_status': counts,
                 'completion_evidence': {
                    'complete_jobs': complete_jobs,
                    'with_latest_authoritative_verified_evidence': complete_with_evidence,
                    'without_latest_authoritative_verified_evidence': complete_jobs - complete_with_evidence,
                    'useful_work_count_verified': False},
                'attention_counts_by_status': attention,
                'total_jobs': sum(counts.values()), 'shown_jobs': len(items),
                'truncated': sum(counts.values()) > len(items), 'jobs': items}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='Read-only operator task desk snapshot. No writes or approvals.')
    parser.add_argument('--db', required=True)
    parser.add_argument('--limit', type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.db, limit=args.limit), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
