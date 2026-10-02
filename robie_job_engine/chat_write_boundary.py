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


def assert_chat_applicant(applicant_id: str) -> None:
    """A named Chat request cannot borrow an allowlisted fixture account."""
    from .turn_finalization import bound_model_context
    owner, generation, path = bound_model_context()
    if not owner:
        return
    if not path or not generation:
        raise RuntimeError('EZLYNX_APPLICANT_UNTRUSTED: missing Chat context')
    from .store import JobStore
    from .client_name_lookup import trusted_applicant_ids, write_client_name
    store = JobStore(path)
    job = store.get_job(owner)
    if write_client_name(job) and str(applicant_id) not in trusted_applicant_ids(store, job):
        raise RuntimeError('EZLYNX_APPLICANT_UNTRUSTED: applicant was not resolved for this request')
