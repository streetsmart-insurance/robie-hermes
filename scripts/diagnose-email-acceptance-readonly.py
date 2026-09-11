#!/usr/bin/env python3
"""Read-only metadata for the explicitly authorized policy acceptance test.

Never emit prompts, message bodies, credentials, browser code, or token values.
"""
import hashlib
import json
import re
import sqlite3
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

JOB_ID = 'bd5b832e-e942-418c-a479-57d7b40262eb'
POLICY = 'TEST-HO-20260911-E01'
MARKERS = ('NEEDS_SKILL', 'NEEDS_CLARIFICATION', 'PLAYWRIGHT_BLOCKED',
           'EZLYNX_WRITE_SCOPE_REFUSED', 'ROBIE_OUTCOME_UNKNOWN',
           'TimeoutError', 'ModuleNotFoundError', 'PermissionError',
           'ImportError', 'AUTH_REQUIRED', 'RESOURCE_EXHAUSTED',
           'max_iterations', 'tool_calls', 'finish_reason', 'STOP',
           'MALFORMED_FUNCTION_CALL', 'MAX_TOKENS')

TOOL_ERROR_PATTERNS = {
    'unknown_tool': r'unknown tool|tool .{0,80}not found|no such tool|unrecognized tool',
    'invalid_arguments': r'invalid argument|validation error|missing required|unexpected keyword',
    'module_missing': r'ModuleNotFoundError|No module named',
    'name_error': r'NameError|is not defined',
    'syntax_error': r'SyntaxError|invalid syntax',
    'attribute_error': r'AttributeError|has no attribute',
    'permission_denied': r'PermissionError|permission denied',
    'file_missing': r'FileNotFoundError|No such file or directory',
    'timeout': r'TimeoutError|timed out',
    'connection_error': r'ConnectionError|connection refused|ECONNREFUSED',
    'tool_unavailable': r'not available|not enabled|not registered|unavailable',
}


def tool_error_categories(content):
    """Fixed labels only: never return excerpts of tool output or error values."""
    return [name for name, pattern in TOOL_ERROR_PATTERNS.items()
            if re.search(pattern, str(content or ''), re.I)]


def markers(value):
    value = str(value or '')
    return [key for key in MARKERS if key.casefold() in value.casefold()]


def connect(path):
    connection = sqlite3.connect(f'file:{quote(str(path))}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


def inspect_job(path):
    with connect(path) as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (JOB_ID,)).fetchone()
        if not row:
            return {'found': False}
        payload = json.loads(row['payload_json'])
        report = {key: row[key] for key in ('id', 'action_type', 'status', 'attempt_count',
                   'verification_count', 'created_at', 'updated_at', 'completed_at')}
        report['matches_requested_test'] = POLICY in str(payload.get('request_text', ''))
        report['gmail_message_id'] = payload.get('gmail_message_id')
        report['error_markers'] = markers(row['last_error'])
        report['error_sha256'] = hashlib.sha256(str(row['last_error']).encode()).hexdigest()
        report['checkpoints'] = []
        for checkpoint in db.execute('SELECT kind,data_json,created_at FROM checkpoints WHERE job_id=? ORDER BY id', (JOB_ID,)):
            data = json.loads(checkpoint['data_json'])
            report['checkpoints'].append({'kind': checkpoint['kind'], 'created_at': checkpoint['created_at'],
                'keys': sorted(data) if isinstance(data, dict) else [], 'markers': markers(checkpoint['data_json'])})
        report['browser_calls'] = []
        for call in db.execute('SELECT id,tool,status,result_json,created_at,updated_at FROM playwright_exec WHERE job_id=? ORDER BY id', (JOB_ID,)):
            report['browser_calls'].append({key: call[key] for key in ('id','tool','status','created_at','updated_at')} | {'markers': markers(call['result_json'])})
        report['verification'] = [dict(item) for item in db.execute('SELECT verified,method,source,authoritative,captured_at FROM verification_evidence WHERE job_id=? ORDER BY id', (JOB_ID,))]
        return report


def inspect_runtime(home):
    report = {'source_files': [], 'session_metadata': {}}
    root = home / 'hermes-agent'
    for name in ('hermes_cli/main.py', 'cli.py', 'run_agent.py'):
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text()
        # Only API/format signatures, not raw source text or configuration values.
        report['source_files'].append({'name': name, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
            'json_output_flag': bool(re.search(r'--(?:json|json-output|output-format)', text)),
            'query_flag': bool(re.search(r'[\"\']-q[\"\']', text)),
            'reasoning_output': 'reasoning' in text.casefold(),
            'final_response': 'final_response' in text,
            'functions': re.findall(r'^\s*(?:async )?def ([a-zA-Z_][a-zA-Z_0-9]*)\(', text, re.M)[:180]})
    for name in ('state.db', 'sessions.db'):
        path = home / name
        if path.is_file():
            with connect(path) as db:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                report['session_metadata'][name] = {}
                for table in tables:
                    if re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]*', table):
                        report['session_metadata'][name][table] = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
    return report


TOOL_EXPOSURE_PROBE = r'''
import sys, json, importlib.util, inspect
def deny(event, args):
    if event in {'socket.connect','socket.connect_ex','socket.bind','subprocess.Popen','os.system','os.exec'}:
        raise RuntimeError('Network/process access forbidden')
sys.addaudithook(deny)
out = {}
try:
    import toolsets
    from tools import registry, tool_search
    core = getattr(toolsets, '_HERMES_CORE_TOOLS', None)
    out['core_collection_type'] = type(core).__name__
    out['playwright_in_core'] = 'playwright_exec' in (core or [])
    out['playwright_dependency_available'] = importlib.util.find_spec('playwright') is not None
    out['register_parameters'] = list(inspect.signature(registry.registry.register).parameters)
    import tools.playwright_tool
    out['playwright_deferrable'] = tool_search.is_deferrable_tool_name('playwright_exec')
    schema = tools.playwright_tool.PLAYWRIGHT_EXEC_SCHEMA
    if 'function' not in schema:
        schema = {'type':'function','function':schema}
    out['schema_name_matches'] = schema['function']['name'] == 'playwright_exec'
    if hasattr(tool_search, 'classify_tools'):
        visible, deferred = tool_search.classify_tools([schema])
        out['visible_schema_count'] = len(visible)
        out['deferred_schema_count'] = len(deferred)
except Exception as exc:
    out['error_type'] = type(exc).__name__
print('TOOL_EXPOSURE_JSON:' + json.dumps(out))
'''


def inspect_tool_exposure(home):
    """Import-only sandbox: no model, browser, network, or tool dispatch."""
    package = home / 'hermes-agent'
    with tempfile.TemporaryDirectory(prefix='robie-tool-exposure-') as directory:
        env = dict(os.environ, HOME=directory, HERMES_HOME=directory,
                   PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=str(package) + os.pathsep + str(home.parent / 'releases/current'))
        result = subprocess.run([str(package / 'venv/bin/python'), '-c', TOOL_EXPOSURE_PROBE],
            cwd=directory, env=env, capture_output=True, text=True, timeout=30)
        lines = [line for line in result.stdout.splitlines() if line.startswith('TOOL_EXPOSURE_JSON:')]
        if result.returncode or len(lines) != 1:
            return {'probe_ok': False, 'returncode': result.returncode}
        return json.loads(lines[0].split(':', 1)[1])


def inspect_acceptance_sessions(path):
    """Only sessions containing this test's unique identifier; no message content output."""
    if not path.is_file():
        return []
    with connect(path) as db:
        rows = db.execute(
            "SELECT DISTINCT s.id,s.source,s.model,s.started_at,s.ended_at,s.end_reason,"
            "s.message_count,s.tool_call_count,s.api_call_count FROM sessions s "
            "JOIN messages m ON m.session_id=s.id WHERE m.content LIKE ? LIMIT 5",
            ('%' + POLICY + '%',),
        ).fetchall()
        result = []
        for row in rows:
            session = dict(row)
            session['messages'] = []
            recent_messages = db.execute(
                'SELECT role,content,tool_name,tool_calls,finish_reason,reasoning,reasoning_content '
                'FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 100', (row['id'],)
            ).fetchall()
            for message in reversed(recent_messages):
                names = []
                try:
                    calls = json.loads(message['tool_calls'] or '[]')
                    for call in calls if isinstance(calls, list) else []:
                        name = (call.get('function') or {}).get('name') or call.get('name') or ''
                        if re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9.-]{0,80}', name):
                            names.append(name)
                except (ValueError, TypeError, AttributeError):
                    pass
                content = str(message['content'] or '')
                session['messages'].append({
                    'role': message['role'], 'tool_name': message['tool_name'],
                    'tool_call_names': names, 'finish_reason': message['finish_reason'],
                    'content_length': len(content), 'markers': markers(content),
                    'tool_error_categories': tool_error_categories(content) if message['role'] == 'tool' else [],
                    'reasoning_length': len(str(message['reasoning'] or message['reasoning_content'] or '')),
                })
            result.append(session)
        return result


if __name__ == '__main__':
    import socket
    if socket.gethostname().split('.')[0] != 'hermes-poc-01':
        raise SystemExit('Production host required')
    root = Path('/opt/streetsmart-hermes')
    print(json.dumps({'job': inspect_job(root / 'robie-job-engine/data/jobs.db'),
                      'runtime': inspect_runtime(root / '.hermes'),
                      'tool_exposure': inspect_tool_exposure(root / '.hermes'),
                      'acceptance_sessions': inspect_acceptance_sessions(root / '.hermes/state.db')}, indent=2))
