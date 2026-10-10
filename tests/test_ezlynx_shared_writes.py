"""Shared EZLynx write jobs: ezlynx.document_upload and ezlynx.note_append.

Runs the real JobEngine and JobStore against fakes. No network, fake data
only. Covers: done, unverified, failed, rerun without a duplicate, partial
reads, wrong client refused, resume after a crash between post and read-back,
and the store refusing COMPLETE without the exact evidence.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from robie_job_engine import ezlynx_shared_writes as sw
from robie_job_engine import ezlynx_write_scope as scope
from robie_job_engine.engine import JobEngine
from robie_job_engine.models import VERIFIER_AUTHORITY, JobStatus, VerificationEvidence
from robie_job_engine.store import JobStore

ALLOWED = "220250093"  # the documented Test client
OTHER = "330000001"
DISCUSSION = "700000001"
OTHER_DISCUSSION = "700000002"
FILE_BYTES = b"%PDF-1.4 fake test document 0001"
NAME = "Fake test document.pdf"
BODY = "Fake test note: renewal reviewed, no change needed."

_ROOT = Path(__file__).resolve().parent.parent / ".robie-durable-test" / "unit"


@pytest.fixture
def durable(tmp_path):
    _ROOT.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=_ROOT))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setenv("ROBIE_EZLYNX_DRIVER_GATE_REQUIRED", "0")
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    for name in ("ROBIE_EZLYNX_WRITE_SCOPE", "ROBIE_EZLYNX_WRITE_APPLICANT_IDS", "EZLYNX_WRITE_APPLICANT_IDS",
                 "ROBIE_PLAYGROUND", "ROBIE_CURRENT_JOB_ID", "ROBIE_JOB_ID", "JOB_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(scope, "ALLOWED_EZLYNX_WRITE_APPLICANT_IDS", frozenset({ALLOWED}))


# ------------------------------------------------------------------ fakes
class FakeDocumentApi:
    """DocumentApi search/download/upload. Search reports completeness."""

    def __init__(self):
        self.docs: dict[str, list[dict[str, Any]]] = {ALLOWED: [{"id": 111, "documentName": "Older.pdf"}]}
        self.bodies: dict[str, bytes] = {"111": b"older"}
        self.uploads: list[tuple[str, str]] = []
        self.next_id = 900
        self.complete = True
        self.list_new_upload = True
        self.stored_bytes: bytes | None = None
        self.fail_after_upload = False

    def search_applicant_documents(self, applicant_id):
        out = {"results": list(self.docs.get(applicant_id, [])), "pages_read": 1,
               "totalSize": len(self.docs.get(applicant_id, [])) + (0 if self.complete else 30)}
        out["complete"] = self.complete
        return out

    def download_document(self, document_id):
        return self.bodies[str(document_id)]

    def upload_applicant_document(self, applicant_id, name, data, **kwargs):
        scope.require_allowed_ezlynx_write_applicant(applicant_id)
        self.next_id += 1
        doc_id = str(self.next_id)
        self.uploads.append((applicant_id, name))
        self.bodies[doc_id] = self.stored_bytes if self.stored_bytes is not None else data
        if self.list_new_upload:
            self.docs.setdefault(applicant_id, []).append({"id": int(doc_id), "documentName": name})
        if self.fail_after_upload:
            raise ConnectionError("connection reset after the upload was accepted")
        return doc_id


class FakeDiscussionApi:
    """DiscussionApi: POST returns no note id, like EZLynx."""

    def __init__(self, existing=(("101", "an older note"),)):
        self.notes = [{"noteId": nid, "body": text} for nid, text in existing]
        self.owned = {ALLOWED: [DISCUSSION], OTHER: [OTHER_DISCUSSION]}
        self.posts: list[tuple[str, str]] = []
        self.incomplete_after_post = False
        self.duplicate_post = False
        self.drop_post = False
        self.fail_after_post = False
        self._next = 900

    def get_discussion_ids(self, applicant_id):
        return list(self.owned.get(applicant_id, []))

    def get_discussion(self, discussion_id):
        latest = self.notes[-1]["noteId"] if self.notes else ""
        return {"discussionId": discussion_id, "mostRecentNoteId": latest, "noteCount": len(self.notes)}

    def get_discussion_with_notes(self, discussion_id):
        rows = self.notes[:-1] if self.incomplete_after_post and self.posts else self.notes
        return {"discussionId": discussion_id, "notes": [dict(r) for r in rows]}

    def _add(self, text):
        self._next += 1
        self.notes.append({"noteId": str(self._next), "body": text})

    def append_note(self, discussion_id, body, *, note_type="Note", applicant_id=None):
        if applicant_id is not None:
            scope.require_allowed_ezlynx_write_applicant(applicant_id)
        self.posts.append((discussion_id, body))
        if not self.drop_post:
            self._add(body)
        if self.duplicate_post:
            self._add(body)
        if self.fail_after_post:
            raise ConnectionError("read timed out after the note was accepted")
        return {}


def _engine(store, docs=None, disc=None):
    workers, verifiers = {}, {}
    if docs is not None:
        workers[sw.DOCUMENT_UPLOAD_WORKER] = sw.EzlynxDocumentUploadWorker(store, client=docs)
        verifiers[sw.DOCUMENT_UPLOAD] = sw.EzlynxDocumentUploadVerifier(store, port=docs)
    if disc is not None:
        workers[sw.NOTE_APPEND_WORKER] = sw.EzlynxNoteAppendWorker(store, client=disc)
        verifiers[sw.NOTE_APPEND] = sw.EzlynxNoteAppendVerifier(store, port=disc)
    return JobEngine(store, workers, verifiers, enforce_recording_policy=False)


def _upload_job(store, durable, applicant=ALLOWED, data=FILE_BYTES, name=NAME):
    path = durable / "doc.pdf"
    path.write_bytes(data)
    return sw.ensure_document_upload_job(store, applicant_id=applicant, document_name=name,
                                         file_path=path, caller="test-caller")


def _note_job(store, applicant=ALLOWED, discussion=DISCUSSION, body=BODY, caller="caller-job-1"):
    return sw.ensure_note_append_job(store, applicant_id=applicant, discussion_id=discussion,
                                     body=body, caller_job_id=caller)


def _run(store, engine, job):
    return sw.run_shared_write_job(store, job["id"], engine)


# --------------------------------------------------------- document upload
def test_upload_done_with_expected_and_observed_evidence(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    job = _run(store, _engine(store, docs=docs), _upload_job(store, durable))
    assert job["status"] == "COMPLETE", job["last_error"]
    assert docs.uploads == [(ALLOWED, NAME)]
    evidence = [e for e in store.list_evidence(job["id"]) if e["verified"]][-1]
    assert evidence["expected"]["document_id"] == evidence["observed"]["document_id"] == "901"
    assert evidence["observed"]["search_complete"] is True
    assert evidence["observed"]["sha256"] == sw.sha256_hex(FILE_BYTES)
    assert sw.verified_destination_id(store, job["id"], "document_id") == "901"


def test_upload_rerun_finds_the_same_job_and_never_uploads_twice(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    first = _run(store, _engine(store, docs=docs), _upload_job(store, durable))
    again = _upload_job(store, durable)
    assert again["id"] == first["id"]
    assert _run(store, _engine(store, docs=docs), again)["status"] == "COMPLETE"
    assert len(docs.uploads) == 1


def test_upload_adopts_the_exact_document_already_on_file(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    docs.docs[ALLOWED].append({"id": 555, "documentName": NAME})
    docs.bodies["555"] = FILE_BYTES
    job = _run(store, _engine(store, docs=docs), _upload_job(store, durable))
    assert job["status"] == "COMPLETE" and docs.uploads == []
    assert sw.verified_destination_id(store, job["id"], "document_id") == "555"


def test_upload_unverified_when_the_document_is_not_listed(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    docs.list_new_upload = False
    job = _run(store, _engine(store, docs=docs), _upload_job(store, durable))
    assert job["status"] == "UNVERIFIED" and len(docs.uploads) == 1


def test_upload_unverified_when_the_stored_bytes_differ(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    docs.stored_bytes = b"different bytes"
    job = _run(store, _engine(store, docs=docs), _upload_job(store, durable))
    assert job["status"] == "UNVERIFIED" and len(docs.uploads) == 1


def test_upload_partial_list_after_upload_is_unverified_and_not_reuploaded(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    worker = sw.EzlynxDocumentUploadWorker(store, client=docs)
    real = worker.perform

    def perform_then_list_breaks(job, *, idempotency_key):
        result = real(job, idempotency_key=idempotency_key)
        docs.complete = False
        return result

    worker.perform = perform_then_list_breaks
    engine = JobEngine(store, {sw.DOCUMENT_UPLOAD_WORKER: worker},
                       {sw.DOCUMENT_UPLOAD: sw.EzlynxDocumentUploadVerifier(store, port=docs)},
                       enforce_recording_policy=False)
    job = _run(store, engine, _upload_job(store, durable))
    assert job["status"] == "UNVERIFIED" and "not read in full" in job["last_error"]
    assert len(docs.uploads) == 1
    assert _run(store, engine, job)["status"] == "UNVERIFIED" and len(docs.uploads) == 1


def test_upload_partial_list_before_upload_sends_nothing(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    docs.complete = False
    job = _run(store, _engine(store, docs=docs), _upload_job(store, durable))
    assert job["status"] != "COMPLETE" and docs.uploads == []


def test_a_search_without_an_explicit_complete_flag_is_not_proof():
    assert sw.document_search_read_every_page({"results": [], "complete": True})
    assert not sw.document_search_read_every_page({"results": []})
    assert not sw.document_search_read_every_page({"results": [], "complete": False})
    assert not sw.document_search_read_every_page([])


def test_upload_to_a_client_off_the_allowlist_is_refused_before_any_call(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    job = _run(store, _engine(store, docs=docs), _upload_job(store, durable, applicant=OTHER))
    assert job["status"] == "FAILED" and scope.EZLYNX_WRITE_SCOPE_REFUSED in job["last_error"]
    assert docs.uploads == []


def test_upload_fails_when_the_file_changed_after_the_job_was_made(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    job = _upload_job(store, durable)
    Path(job["payload"]["file_path"]).write_bytes(b"swapped")
    job = _run(store, _engine(store, docs=docs), job)
    assert job["status"] == "FAILED" and docs.uploads == []


def test_upload_verifier_refuses_a_worker_naming_another_client(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    job = _upload_job(store, durable)
    store.checkpoint(job["id"], sw.INTENT_KIND, {"prior_document_ids": [], "state": "posted"})
    result = sw.EzlynxDocumentUploadVerifier(store, port=docs).verify(
        job, {"destination": {"applicant_id": OTHER}, "detail": {}})
    assert not result.verified and "bound to" in result.error


def test_upload_crash_after_post_is_confirmed_once_without_reupload(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    docs.fail_after_upload = True  # accepted, then the reply was lost
    job = _run(store, _engine(store, docs=docs), _upload_job(store, durable))
    assert job["status"] == "COMPLETE", job["last_error"]
    assert len(docs.uploads) == 1


def test_upload_resume_after_process_died_between_post_and_readback(durable):
    store, docs = JobStore(durable / "jobs.db"), FakeDocumentApi()
    job = _upload_job(store, durable)
    # The earlier process reserved, uploaded, and died before recording the id.
    prior = [r["id"] for r in sw._document_rows(docs.search_applicant_documents(ALLOWED))]
    sw.reserve_intent(store, job["id"], {"prior_document_ids": prior, "state": "reserved", "document_id": ""})
    docs.upload_applicant_document(ALLOWED, NAME, FILE_BYTES)
    job = _run(store, _engine(store, docs=docs), job)
    assert job["status"] == "COMPLETE", job["last_error"]
    assert len(docs.uploads) == 1


# -------------------------------------------------------------- note append
def test_note_done_by_the_one_new_note_with_exact_text(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    job = _run(store, _engine(store, disc=disc), _note_job(store))
    assert job["status"] == "COMPLETE", job["last_error"]
    assert disc.posts == [(DISCUSSION, BODY)]
    evidence = [e for e in store.list_evidence(job["id"]) if e["verified"]][-1]
    assert evidence["observed"]["note_id"] == evidence["expected"]["note_id"] == "901"
    assert evidence["observed"]["new_matching_notes"] == 1
    assert evidence["observed"]["body_norm_sha256"] == sw.body_norm_sha256(BODY)


def test_an_older_note_with_the_same_words_is_not_taken(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi(existing=(("101", BODY),))
    job = _run(store, _engine(store, disc=disc), _note_job(store))
    assert job["status"] == "COMPLETE"
    assert sw.verified_destination_id(store, job["id"], "note_id") == "901"


def test_note_rerun_finds_the_same_job_and_never_reposts(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    first = _run(store, _engine(store, disc=disc), _note_job(store))
    again = _note_job(store, body="  " + BODY.replace(" ", "  ") + "\n")  # same normalized text
    assert again["id"] == first["id"]
    _run(store, _engine(store, disc=disc), again)
    assert len(disc.posts) == 1
    other_caller = _note_job(store, caller="caller-job-2")
    assert other_caller["id"] != first["id"]


def test_note_unverified_when_it_never_appears(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    disc.drop_post = True
    job = _run(store, _engine(store, disc=disc), _note_job(store))
    assert job["status"] == "UNVERIFIED" and len(disc.posts) == 1


def test_two_new_notes_with_this_text_are_unverified(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    disc.duplicate_post = True
    job = _run(store, _engine(store, disc=disc), _note_job(store))
    assert job["status"] == "UNVERIFIED" and len(disc.posts) == 1


def test_note_partial_read_after_post_is_unverified_and_not_reposted(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    disc.incomplete_after_post = True
    engine = _engine(store, disc=disc)
    job = _run(store, engine, _note_job(store))
    assert job["status"] == "UNVERIFIED" and "not read in full" in job["last_error"]
    assert _run(store, engine, job)["status"] == "UNVERIFIED" and len(disc.posts) == 1


def test_note_partial_read_before_post_sends_nothing(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    disc.get_discussion_with_notes = lambda discussion_id: {"notes": []}  # count says 1
    job = _run(store, _engine(store, disc=disc), _note_job(store))
    assert job["status"] != "COMPLETE" and disc.posts == []


def test_note_to_a_client_off_the_allowlist_is_refused(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    job = _run(store, _engine(store, disc=disc), _note_job(store, applicant=OTHER, discussion=OTHER_DISCUSSION))
    assert job["status"] == "FAILED" and scope.EZLYNX_WRITE_SCOPE_REFUSED in job["last_error"]
    assert disc.posts == []


def test_note_to_another_clients_discussion_is_refused(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    job = _run(store, _engine(store, disc=disc), _note_job(store, discussion=OTHER_DISCUSSION))
    assert job["status"] == "FAILED" and "not one of applicant" in job["last_error"]
    assert disc.posts == []


def test_note_crash_after_post_is_confirmed_once_without_repost(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    disc.fail_after_post = True
    job = _run(store, _engine(store, disc=disc), _note_job(store))
    assert job["status"] == "COMPLETE", job["last_error"]
    assert len(disc.posts) == 1


def test_note_resume_after_process_died_between_post_and_readback(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    job = _note_job(store)
    sw.reserve_intent(store, job["id"], {"prior_note_ids": sw.complete_note_ids(disc, DISCUSSION),
                                         "state": "reserved", "note_id": ""})
    disc.append_note(DISCUSSION, BODY)  # landed; the process died before recording anything
    job = _run(store, _engine(store, disc=disc), job)
    assert job["status"] == "COMPLETE", job["last_error"]
    assert len(disc.posts) == 1


def test_note_resume_when_the_lost_post_never_landed_is_unverified(durable):
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    job = _note_job(store)
    sw.reserve_intent(store, job["id"], {"prior_note_ids": sw.complete_note_ids(disc, DISCUSSION),
                                         "state": "reserved", "note_id": ""})
    job = _run(store, _engine(store, disc=disc), job)
    assert job["status"] == "UNVERIFIED" and disc.posts == []


def test_production_holds_a_new_type_until_three_clean_test_jobs(durable, monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    job = _run(store, _engine(store, disc=disc), _note_job(store))
    assert job["status"] == "NEEDS_CLARIFICATION" and "not Production-ready" in job["last_error"]
    assert disc.posts == []


# ------------------------------------------------------ COMPLETE refused
def _force_complete(store, job, expected, observed):
    store.claim(job["id"], "t", lease_seconds=60)
    store.transition(job["id"], JobStatus.RUNNING)
    store.checkpoint(job["id"], "action", {"action": job["action_type"],
                                           "destination": {"resource_id": job["payload"]["resource_id"]}})
    store.transition(job["id"], JobStatus.VERIFYING)
    store.add_evidence(job["id"], True, VerificationEvidence(
        method="t", source="t", expected=expected, observed=observed, authoritative=True,
        captured_at=datetime.now(timezone.utc).isoformat(),
        locator=job["payload"]["resource_id"] + "/x"))
    return store.transition(job["id"], JobStatus.COMPLETE, expected={JobStatus.VERIFYING},
                            authority=VERIFIER_AUTHORITY)


def test_store_refuses_note_complete_without_the_new_note_proof(durable):
    store = JobStore(durable / "jobs.db")
    job = _note_job(store)
    blob = {"resource_id": job["payload"]["resource_id"], "discussion_id": DISCUSSION, "note_id": "901"}
    with pytest.raises(PermissionError, match="note_append"):
        _force_complete(store, job, dict(blob), dict(blob))


def test_store_refuses_upload_complete_without_a_complete_search(durable):
    store = JobStore(durable / "jobs.db")
    job = _upload_job(store, durable)
    blob = {"resource_id": job["payload"]["resource_id"], "applicant_id": ALLOWED, "document_id": "901",
            "document_name": NAME, "sha256": sw.sha256_hex(FILE_BYTES), "search_complete": False}
    with pytest.raises(PermissionError, match="search_complete"):
        _force_complete(store, job, dict(blob), dict(blob))


def test_store_allows_upload_complete_with_full_evidence(durable):
    store = JobStore(durable / "jobs.db")
    job = _upload_job(store, durable)
    blob = {"resource_id": job["payload"]["resource_id"], "applicant_id": ALLOWED, "document_id": "901",
            "document_name": NAME, "sha256": sw.sha256_hex(FILE_BYTES), "search_complete": True}
    assert _force_complete(store, job, dict(blob), dict(blob))["status"] == "COMPLETE"


def test_contracts_name_the_verifiers_and_runtime_registers_them():
    from robie_job_engine.job_schema import bounded_schema_hold_reason, get_executable_skill_contract

    assert get_executable_skill_contract(sw.DOCUMENT_UPLOAD).independent_verifier == "EzlynxDocumentUploadVerifier"
    assert get_executable_skill_contract(sw.NOTE_APPEND).independent_verifier == "EzlynxNoteAppendVerifier"
    assert bounded_schema_hold_reason(sw.NOTE_APPEND, {}) == "missing required schema field: applicant_id"


def test_terminal_tab_cleanup_never_touches_the_browser_for_api_only_jobs(durable):
    from robie_job_engine.tab_cleanup import cleanup_terminal_job_tabs

    store, disc = JobStore(durable / "jobs.db"), FakeDiscussionApi()
    job = _run(store, _engine(store, disc=disc), _note_job(store))

    def must_not_reset():
        raise AssertionError("workspace reset attempted for an API-only job")

    result = cleanup_terminal_job_tabs(str(store.path), job["id"], workspace_resetter=must_not_reset,
                                       tabs=[])
    assert result["ok"] and "API-only" in result["skipped"]
