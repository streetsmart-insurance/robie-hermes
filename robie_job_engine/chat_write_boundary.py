"""Recheck driver ownership and the exact Chat generation at API transport."""
from __future__ import annotations


def assert_chat_write_allowed() -> None:
    from .ezlynx_driver_gate import require_driver_in
    from .turn_finalization import bound_model_context

    require_driver_in()
    job_id, generation, db_path = bound_model_context()
    # Non-Chat clients retain their existing authorization path. Only bound
    # context identifies a Chat turn; mutable job env is never authority.
    if not job_id and not generation and not db_path:
        return
    if not job_id or not generation or not db_path:
        raise RuntimeError('EZLYNX_WRITE_REFUSED: missing bound Chat generation')
    from .store import JobStore

    try:
        store = JobStore(db_path)
        job = store.get_job(job_id)
        state = store.get_checkpoint(job_id, 'model_generation') or {}
    except Exception as exc:
        raise RuntimeError('EZLYNX_WRITE_REFUSED: Chat state unavailable') from exc
    if (job.get('status') != 'RUNNING' or not state.get('running')
            or state.get('generation') != generation):
        raise RuntimeError('EZLYNX_WRITE_REFUSED: Chat job is not the running owner')
    assert_chat_applicant(str((job.get('payload') or {}).get('applicant_id') or ''))


def assert_chat_applicant(applicant_id: str) -> None:
    """Every bound Chat write needs unambiguous job-specific provenance."""
    from .turn_finalization import bound_model_context
    owner, generation, path = bound_model_context()
    if not owner and not generation and not path:
        return
    if not owner or not path or not generation:
        raise RuntimeError('EZLYNX_APPLICANT_UNTRUSTED: missing Chat context')
    from .store import JobStore
    from .client_name_lookup import trusted_applicant_ids
    store = JobStore(path)
    job = store.get_job(owner)
    trusted = trusted_applicant_ids(store, job)
    if len(trusted) != 1 or str(applicant_id) != trusted[0]:
        raise RuntimeError('EZLYNX_APPLICANT_UNTRUSTED: applicant was not resolved for this request')
