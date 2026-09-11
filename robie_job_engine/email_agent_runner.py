"""Run email work through Hermes' final-response interface, with one scoped repair."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import uuid
from pathlib import Path
from urllib.parse import quote

from .store import JobStore

RECOVERY_PROMPT = (
    'The previous turn ended with malformed_function_call: that malformed tool call did not execute. '
    'Continue the SAME original email job and its existing account scope. Do not restart the task '
    'or repeat successful actions. Read the current destination and job checkpoints before any write. '
    'Use one valid tool call at a time, with the exact schema and short code. '
    'Finish with the actual saved result and unchecked gaps, or a concrete blocker.'
)


def session_finish_reason(home: Path, session_id: str) -> str:
    path = home / 'state.db'
    with sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True, timeout=5) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute(
            "SELECT finish_reason FROM messages WHERE session_id=? AND role='assistant' "
            "AND COALESCE(active,1)=1 ORDER BY id DESC LIMIT 1", (session_id,)
        ).fetchone()
    return str(row[0] or '').casefold() if row else ''


def run_scripted_email(prompt: str, *, env: dict, home: Path, cwd: Path,
                       job_id: str, db_path: str, runner=None) -> str:
    runner = runner or subprocess.run
    if not job_id or not db_path:
        return 'ROBIE_EXECUTION_BLOCKED: Email execution requires an active durable job.'
    store = JobStore(db_path)
    if store.get_job(job_id)['status'] != 'RUNNING':
        return 'ROBIE_EXECUTION_BLOCKED: The email job is not RUNNING.'
    key = hashlib.sha256(job_id.encode()).hexdigest()[:24]
    run_dir = Path(db_path).parent / 'email-runs' / key / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    records = []
    session_id = None
    for attempt in range(2):
        if store.get_job(job_id)['status'] != 'RUNNING':
            return 'ROBIE_EXECUTION_BLOCKED: The email job stopped before execution or recovery.'
        usage_path = run_dir / f'usage-{attempt}.json'
        command = [str(home / 'hermes-agent/venv/bin/python'), '-m', 'hermes_cli.main',
                   '-z', prompt if attempt == 0 else RECOVERY_PROMPT,
                   '--usage-file', str(usage_path)]
        if session_id:
            command.extend(['--resume', session_id])
        try:
            result = runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, timeout=600 if attempt == 0 else 300,
                            env=env, cwd=str(cwd))
            usage = json.loads(usage_path.read_text())
            reported_session = str(usage.get('session_id') or '')
            if not reported_session or (session_id and reported_session != session_id):
                raise ValueError('Missing or changed agent session')
            session_id = reported_session
            finish_reason = session_finish_reason(home, session_id)
            record = {'attempt': attempt + 1, 'session_id': session_id,
                      'returncode': result.returncode, 'finish_reason': finish_reason,
                      'completed': usage.get('completed'), 'failed': usage.get('failed')}
            records.append(record)
            store.checkpoint(job_id, 'email_agent_runtime', {'interface': 'scripted-final-response', 'attempts': records})
            if finish_reason == 'malformed_function_call':
                if attempt == 0:
                    continue
                return ('ROBIE_EXECUTION_BLOCKED: The agent produced a malformed tool call again '
                        'after one same-session recovery. The requested result is not verified.')
            if result.returncode or usage.get('failed') or not usage.get('completed'):
                return 'ROBIE_OUTCOME_UNKNOWN: The agent did not finish successfully. Check saved results before retrying.'
            if not finish_reason or finish_reason in {'tool_calls', 'length', 'max_tokens'}:
                return 'ROBIE_OUTCOME_UNKNOWN: The agent ended without a complete final turn. Check saved results before retrying.'
            response = result.stdout.strip()
            if not response:
                return 'ROBIE_OUTCOME_UNKNOWN: The agent returned no final response. Check saved results before retrying.'
            return response
        except subprocess.TimeoutExpired:
            # The parent owns process-group cleanup on every return and on its deadline.
            return 'ROBIE_OUTCOME_UNKNOWN: Email execution timed out. Check saved results before retrying.'
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
            return 'ROBIE_OUTCOME_UNKNOWN: No trustworthy final agent receipt was available. Check saved results before retrying.'
    raise AssertionError('unreachable')
