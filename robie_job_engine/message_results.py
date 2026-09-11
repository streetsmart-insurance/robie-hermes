"""Describe checked destination facts and gaps without promoting worker claims."""
from .complete_guard import evidence_is_stale


def verification_summary(store, job_id):
    job = store.get_job(job_id)
    rows = store.list_evidence(job_id)
    if not rows:
        return ''
    row = rows[-1]
    boundary = (store.get_checkpoint_record(job_id, 'action') or {}).get('created_at') or job.get('created_at')
    if (not row.get('authoritative') or
            row.get('method') != 'EZLYNX_API_DESTINATION_READBACK' or
            evidence_is_stale(captured_at=row.get('captured_at'), not_before=boundary)):
        return ''
    observed = row.get('observed') or {}
    expected = row.get('expected') or {}
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
        lines.append('Not checked: document library could not be read.')
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
