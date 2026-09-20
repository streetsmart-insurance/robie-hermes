"""Describe checked destination facts and gaps without promoting worker claims."""
from .complete_guard import evidence_is_stale


def verification_summary(store, job_id):
    job = store.get_job(job_id)
    rows = store.list_evidence(job_id)
    if not rows:
        diagnostic = store.get_checkpoint(job_id, 'document_upload_diagnostic') or {}
        if diagnostic.get('next_action') == 'READ_BACK_ONLY':
            return (f"Saved upload ID {diagnostic.get('document_id')}; contents are not yet verified. "
                    'Retry the read-back only; do not upload again.')
        if diagnostic.get('next_action') == 'RECONCILE_BEFORE_WRITE':
            return 'Upload outcome is uncertain. Check the destination before any repeat upload.'
        return ''
    row = rows[-1]
    boundary = (store.get_checkpoint_record(job_id, 'action') or {}).get('created_at') or job.get('created_at')
    if (not row.get('authoritative') or
            row.get('method') not in {'EZLYNX_API_DESTINATION_READBACK', 'EZLYNX_DOCUMENT_CONTENT_READBACK'} or
            evidence_is_stale(captured_at=row.get('captured_at'), not_before=boundary)):
        return ''
    observed = row.get('observed') or {}
    expected = row.get('expected') or {}
    if row.get('method') == 'EZLYNX_DOCUMENT_CONTENT_READBACK':
        if row.get('verified') and expected == observed:
            return (f"Checked: document {observed.get('document_id')} on applicant "
                    f"{observed.get('applicant_id')}; name and contents match the upload source.")
        return 'Not confirmed: document account, ID, name, or contents did not pass independent read-back.'
    lines = []
    if observed.get('policy_found'):
        lines.append(f"Checked: policy {expected.get('policy_number', '')} exists on applicant {expected.get('applicant_id', '')}.")
    documents = expected.get('document_names') or []
    if documents and 'documents_missing' in observed:
        missing = observed['documents_missing']
        found = [name for name in documents if name not in missing]
        if found:
            lines.append('Checked: documents found: ' + ', '.join(found) + '.')
        if missing:
            lines.append('Not confirmed: documents missing: ' + ', '.join(missing) + '.')
    elif documents and observed.get('documents_error'):
        lines.append('Not checked: DocumentApi could not be read.')
    if expected.get('discussion_title'):
        receipt = observed.get('discussion_receipt_present')
        if receipt is True:
            lines.append('Checked: the named discussion receipt exists; this does not prove its described work.')
        elif receipt is False:
            lines.append('Gap: the named discussion receipt was not found.')
        else:
            lines.append('Not checked: the discussion receipt could not be read.')
    if lines:
        lines.append('These checks establish record/document presence; other requested field changes are not covered by this check.')
    return '\n'.join(lines)
