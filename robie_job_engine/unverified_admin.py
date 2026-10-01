"""Close an unverified job that never wrote. This does not touch EZLynx."""

from __future__ import annotations

import argparse

from .models import JobStatus
from .chat_job_controls import stop_recordings_for_jobs


def job_has_external_write(store, job: dict) -> bool:
    """True when this job already filed a note, uploaded a file, or proved a write."""
    from .write_verification_loop import write_landed

    if write_landed(store, job):
        return True
    job_id = str(job.get("id") or "")
    if not job_id:
        return False
    note = store.get_checkpoint(job_id, "discussion_note") or {}
    if str(note.get("note_id") or "").strip():
        return True
    if str(note.get("status") or "") in {"filed", "posted, verifying", "sent"}:
        return True
    for kind in ("document_upload", "uploaded_document", "ezlynx_document"):
        document = store.get_checkpoint(job_id, kind) or {}
        if document.get("document_id") or document.get("read_back"):
            return True
    for item in store.list_evidence(job_id):
        if not item.get("verified"):
            continue
        method = str(item.get("method") or "")
        if method in {"DiscussionApi", "EZLYNX_API", "EZLYNX_API_DESTINATION_READBACK"}:
            return True
    return False


def close_unverified_without_write(store, job_id: str, *, reason: str = "closed without a write") -> dict:
    """Cancel a finished-but-unverified job that never wrote.

    Refuses anything that is not UNVERIFIED, and anything that already
    wrote. The status change is the whole action.
    """
    job = store.get_job(job_id)
    if JobStatus(job["status"]) != JobStatus.UNVERIFIED:
        raise RuntimeError("only an unverified job can be closed this way")
    if job_has_external_write(store, job):
        raise RuntimeError("this job wrote something; close it only after a review")
    store.checkpoint(
        job_id,
        "admin_close",
        {"reason": reason, "wrote": False},
    )
    closed = store.transition(
        job_id,
        JobStatus.CANCELLED,
        expected={JobStatus.UNVERIFIED},
        error=reason,
        release_lease=True,
    )
    stop_recordings_for_jobs(
        str(getattr(store, "path", "") or ""),
        [job_id],
        JobStatus.CANCELLED.value,
    )
    return closed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cancel an UNVERIFIED job that never wrote. Does not call EZLynx."
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--reason", default="closed without a write")
    args = parser.parse_args()
    from .store import JobStore

    closed = close_unverified_without_write(
        JobStore(args.db), args.job_id, reason=args.reason
    )
    print(f"{closed['id']} {closed['status']}")


if __name__ == "__main__":
    main()
