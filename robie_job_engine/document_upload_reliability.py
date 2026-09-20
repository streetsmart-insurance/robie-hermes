"""Bounded document receipts and at-most-once uploads within a durable job.

An interrupted POST without an ID is deliberately not replayed. A saved ID
permits read-back retries only. Receipts never authorize COMPLETE themselves.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import VerificationEvidence, VerificationResult
from .store import JobStore, canonical_json, utc_now

_REQUEST = re.compile(
    r'(?:please\s+)?upload\s+(?:document\s+)?"([^"\r\n]+)"\s+to\s+'
    r'applicant\s+([1-9]\d*)\s*[.!]?\s*', re.I)


def document_request(payload):
    """Recognize only a complete single-upload request, never a partial match."""
    text = str(payload.get('request_text') or payload.get('text') or '')
    if text.startswith('Subject: '):
        subject, separator, text = text.partition('\n\n')
        if not separator or subject[9:].strip().casefold() not in {
            'task', 'robie task', 'document upload',
        }:
            return None
    match = _REQUEST.fullmatch(text.strip())
    if not match:
        return None
    name, applicant = match.groups()
    if payload.get('applicant_id') and str(payload['applicant_id']) != applicant:
        return None
    return {'document_name': name, 'applicant_id': applicant}


def _context(env):
    ids = {env[key] for key in ('ROBIE_JOB_ID', 'ROBIE_CURRENT_JOB_ID', 'JOB_ID') if env.get(key)}
    db = env.get('ROBIE_JOB_DB')
    if not ids and not db:
        return None
    if len(ids) != 1 or not db or not Path(db).is_file():
        raise ValueError('Document upload requires one existing job and job database')
    store = JobStore(db)
    job = store.get_job(next(iter(ids)))
    if job['status'] != 'RUNNING':
        raise ValueError('Document upload requires a RUNNING job')
    return store, job


def _confirm(client, applicant, name, doc_id, digest):
    from .ezlynx_api_read_port import EzlynxApiClientReadPort
    rows = EzlynxApiClientReadPort(client).documents_for_applicant(applicant)
    if not any(str(row.get('id')) == doc_id and row.get('name') == name for row in rows):
        raise ValueError('Saved document ID and name not confirmed on the requested account; do not upload again')
    body = client.download_document(doc_id)
    body = getattr(body, 'body', body)
    if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError('Saved document contents do not match the upload source; do not upload again')


def upload_with_receipt(client, applicant, name, data, *, env=None, **kwargs):
    """Journal a POST intent before writing, then persist its ID before reads."""
    applicant, name = str(applicant).strip(), str(name).strip()
    digest = hashlib.sha256(data).hexdigest()
    context = _context(os.environ if env is None else env)
    receipt = {'applicant_id': applicant, 'document_name': name, 'sha256': digest}
    kind = None
    previous = None
    if context:
        store, job = context
        bound = str(job['payload'].get('applicant_id') or '').strip()
        if bound and bound != applicant:
            raise ValueError('Upload account differs from the bound job')
        requested = document_request(job['payload'])
        if requested and (requested != {'applicant_id': applicant, 'document_name': name}
                          or str(kwargs.get('policy_master_id') or '0') != '0'):
            raise ValueError('Upload differs from the requested applicant-level document')
        identity = [applicant, name, str(kwargs.get('policy_master_id') or '0')]
        kind = 'document_upload:' + hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        # The transaction serializes simultaneous calls before either can POST.
        with store.transaction() as conn:
            status = conn.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if not status or status[0] != 'RUNNING':
                raise ValueError('Document job stopped before upload intent')
            row = conn.execute('SELECT data_json FROM checkpoints WHERE job_id=? AND kind=?',
                               (job['id'], kind)).fetchone()
            if row:
                previous = json.loads(row[0])
                if previous.get('sha256') != digest:
                    raise ValueError('Upload source changed after its durable intent')
                if not previous.get('document_id'):
                    raise ValueError('DOCUMENT_UPLOAD_OUTCOME_UNKNOWN: prior upload intent has no saved ID; reconcile before retry')
            else:
                conn.execute('INSERT INTO checkpoints(job_id,kind,data_json,created_at) VALUES(?,?,?,?)',
                             (job['id'], kind, canonical_json({**receipt, 'state': 'INTENT'}), utc_now()))
    doc_id = previous['document_id'] if previous else None
    try:
        if not previous:
            doc_id = str(client.upload_applicant_document(applicant, name, data, **kwargs)).strip()
            if not doc_id.isdigit():
                raise ValueError('DOCUMENT_UPLOAD_OUTCOME_UNKNOWN: upload returned no numeric document ID')
            if context:
                store.checkpoint(job['id'], kind, {**receipt, 'document_id': doc_id, 'state': 'SAVED'})
        _confirm(client, applicant, name, doc_id, digest)
    except Exception as exc:
        if context:
            # Bounded structured diagnostics only: no document bytes, paths,
            # credentials, or raw exception messages in the incident record.
            try:
                saved = store.get_checkpoint(job['id'], kind) or {}
                store.checkpoint(job['id'], 'document_upload_diagnostic', {
                    'operation': kind, 'failure_class': type(exc).__name__,
                    'last_saved_state': saved.get('state', 'INTENT'),
                    'document_id': saved.get('document_id'),
                    'safe_to_repeat_upload': False,
                    'next_action': 'READ_BACK_ONLY' if saved.get('document_id') else 'RECONCILE_BEFORE_WRITE',
                })
            except Exception:
                pass  # Preserve the original failure if diagnostic storage fails.
        raise
    receipt.update(document_id=doc_id, read_back=True, state='VERIFIED')
    if context:
        store.checkpoint(job['id'], kind, receipt)
        if document_request(job['payload']):
            # Persist the expected source fingerprint and returned ID, not a
            # completion verdict. The message verifier reads the destination again.
            payload = dict(store.get_job(job['id'])['payload'])
            payload['document_upload_receipt'] = receipt
            store.update_payload(job['id'], payload)
            store.checkpoint(job['id'], 'action', {
                'action': 'ezlynx.document_upload', 'destination': receipt,
                'detail': {'write_method': 'DocumentApi'},
            })
    return {'ok': True, **receipt}


def run_document_email(store, job_id, attachments, *, client=None):
    """Execute a recognized single-attachment upload without an agent/browser loop.

    None means another route owns the request. A recognized request never
    falls back to open-ended agent execution after a failure.
    """
    job = store.get_job(job_id)
    request = document_request(job['payload'])
    if not request:
        return None
    matches = [path for name, path in attachments if name == request['document_name']]
    if len(matches) != 1 or len(attachments) != 1:
        return 'ROBIE HITL: Supply exactly the one named attachment for this document upload.'
    store.checkpoint(job_id, 'email_route', {'route': 'document_upload', 'execution': 'deterministic-api'})
    try:
        data = Path(matches[0]).read_bytes()
        if not data:
            return 'ROBIE HITL: The named attachment is empty; supply the intended document.'
        if client is None:
            from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config
            client = EzlynxApiClient(load_ezlynx_api_config())
        receipt = upload_with_receipt(client, request['applicant_id'], request['document_name'], data,
                                     env={'ROBIE_JOB_ID': job_id, 'ROBIE_JOB_DB': store.path},
                                     filename=request['document_name'])
    except Exception as exc:
        return ('ROBIE_OUTCOME_UNKNOWN: Document upload stopped (' + type(exc).__name__ +
                '). Inspect the durable upload state; do not repeat the write. Independent verification is required.')
    return f"DocumentApi saved document {receipt['document_id']}; independent job verification follows."


def verify_document_request(reader, job, action):
    """Fresh account membership + returned ID + exact source hash; no policy required."""
    payload = job.get('payload') or {}
    requested = document_request(payload)
    receipt = payload.get('document_upload_receipt') or {}
    expected = {**(requested or {}), 'document_id': str(receipt.get('document_id') or ''),
                'sha256': str(receipt.get('sha256') or '')}
    observed = {}
    error = None
    retryable = False
    authoritative = False
    claimed = (action or {}).get('destination') or {}
    if (not requested or any(receipt.get(k) != v for k, v in requested.items())
            or not expected['document_id'].isdigit()
            or not re.fullmatch('[a-f0-9]{64}', expected['sha256'])
            or any(claimed.get(k) != v for k, v in expected.items())):
        error = 'Document upload lacks a matching durable account, ID, and source fingerprint'
    else:
        try:
            rows = reader.documents_for_applicant(expected['applicant_id'])
            matches = [row for row in rows if str(row.get('id')) == expected['document_id']
                       and row.get('name') == expected['document_name']]
            authoritative = True
            if not matches:
                error = 'Returned document ID and name were not confirmed on the requested account'
            else:
                body = reader.download_document(expected['document_id'])
                body = getattr(body, 'body', body)
                observed = {**requested, 'document_id': expected['document_id'],
                            'sha256': hashlib.sha256(body).hexdigest()}
                if observed != expected:
                    error = 'Saved document contents differ from the upload source'
        except Exception as exc:
            error = 'Document read-back failed: ' + type(exc).__name__
            retryable = True
    evidence = VerificationEvidence(
        method='EZLYNX_DOCUMENT_CONTENT_READBACK', source='ezlynx-documentapi',
        expected=expected, observed=observed, authoritative=authoritative,
        captured_at=datetime.now(timezone.utc).isoformat(), locator=expected['document_id'] or None)
    return VerificationResult(error is None, evidence, retryable=retryable, error=error)
