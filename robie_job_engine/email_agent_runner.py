"""Run email work on the installed chat interface; read final replies from its ledger."""
from __future__ import annotations

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


def session_connection(home: Path):
    db = sqlite3.connect(f'file:{quote(str(home / "state.db"))}?mode=ro', uri=True, timeout=5)
    db.execute('PRAGMA query_only=ON')
    return db


def session_for_receipt(home: Path, nonce: str) -> tuple[str, int]:
    with session_connection(home) as db:
        rows = db.execute(
            "SELECT session_id,MAX(id) FROM messages WHERE role='user' AND instr(content,?)>0 GROUP BY session_id LIMIT 2",
            (nonce,),
        ).fetchall()
    if len(rows) != 1:
        raise ValueError('Missing or ambiguous email session')
    return str(rows[0][0]), int(rows[0][1])


def session_receipt(home: Path, session_id: str) -> dict:
    with session_connection(home) as db:
        row = db.execute(
            "SELECT id,finish_reason,content FROM messages WHERE session_id=? AND role='assistant' "
            "AND COALESCE(active,1)=1 ORDER BY id DESC LIMIT 1", (session_id,),
        ).fetchone()
    return {'message_id': row[0], 'finish_reason': str(row[1] or '').casefold(),
            'content': str(row[2] or '')} if row else {}


def run_scripted_email(prompt: str, *, env: dict, home: Path, cwd: Path,
                       job_id: str, db_path: str, runner=None) -> str:
    runner = runner or subprocess.run
    if not job_id or not db_path:
        return 'ROBIE_EXECUTION_BLOCKED: Email execution requires an active durable job.'
    store = JobStore(db_path)
    nonce = 'ROBIE_EMAIL_RECEIPT_' + uuid.uuid4().hex
    records = []
    session_id = None
    for attempt in range(2):
        if store.get_job(job_id)['status'] != 'RUNNING':
            return 'ROBIE_EXECUTION_BLOCKED: The email job stopped before execution or recovery.'
        command = [str(home / 'hermes-agent/venv/bin/python'), '-m', 'hermes_cli.main', 'chat']
        try:
            baseline = session_receipt(home, session_id).get('message_id', 0) if session_id else 0
            if session_id:
                command.extend(['--resume', session_id, '-q', RECOVERY_PROMPT])
            else:
                command.extend(['-q', prompt + '\n\nExecution receipt identifier: ' + nonce +
                                '. Do not include this identifier in your final reply.'])
                store.checkpoint(job_id, 'email_agent_invocation', {'receipt_nonce': nonce})
            result = runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, timeout=600 if attempt == 0 else 300,
                            env=env, cwd=str(cwd))
            if not session_id:
                session_id, baseline = session_for_receipt(home, nonce)
            receipt = session_receipt(home, session_id)
            if receipt.get('message_id', 0) <= baseline:
                raise ValueError('No fresh response in the same session')
            finish_reason = receipt.get('finish_reason', '')
            records.append({'attempt': attempt + 1, 'session_id': session_id,
                            'returncode': result.returncode, 'finish_reason': finish_reason,
                            'message_id': receipt['message_id'], 'mode': 'chat-resume' if attempt else 'chat'})
            store.checkpoint(job_id, 'email_agent_runtime', {'interface': 'session-final-response', 'attempts': records})
            if finish_reason == 'malformed_function_call':
                if attempt == 0:
                    continue
                return ('ROBIE_EXECUTION_BLOCKED: The agent produced a malformed tool call again '
                        'after one same-session recovery. The requested result is not verified.')
            if result.returncode or finish_reason not in {'stop', 'end_turn'}:
                return 'ROBIE_OUTCOME_UNKNOWN: The agent ended without a complete final turn. Check saved results before retrying.'
            # Display output and separate reasoning fields are never used as the result.
            response = receipt['content'].strip()
            if not response:
                return 'ROBIE_OUTCOME_UNKNOWN: The agent returned no final response. Check saved results before retrying.'
            return response
        except subprocess.TimeoutExpired:
            # The parent owns process-group cleanup on every return and on its deadline.
            return 'ROBIE_OUTCOME_UNKNOWN: Email execution timed out. Check saved results before retrying.'
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
            return 'ROBIE_OUTCOME_UNKNOWN: No trustworthy final agent receipt was available. Check saved results before retrying.'
    raise AssertionError('unreachable')
