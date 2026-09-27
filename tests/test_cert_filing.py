"""Chunk 3 offline tests: filing pipeline, task registry, Zapier client.

Everything runs against fakes. No EZLynx, no Gmail, no Zapier, no network.
"""

import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_filing import (
    DRY_RUN, FILED, HELD, FilingDeps, FilingResult, FilingStore, file_record,
    resolve_discussion,
)
from robie_job_engine.cert_task_registry import (
    CREATE, HOLD, NONE, REOPEN, REUSE, TASK_CLOSED, TASK_OPEN,
    TaskEntry, TaskRegistry, decide_task_action, holder_key_for,
    policy_key_for,
)
from robie_job_engine.cert_verification import (
    ACTION_ACK, ACTION_NEW_REQUEST, VERIFIED, FilingTargetMismatch,
    VerificationResult,
)
from robie_job_engine.cert_zapier import CertZapierClient


def make_verified(**kw):
    v = VerificationResult(status=VERIFIED, applicant_id=220250093,
                           insured_name="Acme LLC",
                           policy_numbers=["POL123"],
                           requested_action=ACTION_NEW_REQUEST)
    for k, val in kw.items():
        setattr(v, k, val)
    return v


def make_record(**kw):
    facts = SimpleNamespace(
        insured_name="Acme LLC", policy_numbers=["POL123"],
        holder_names=["Big Client Inc"], requester_name="Bob",
        requester_email="bob@x.com", requester_is_third_party=False,
        pdf_unreadable=False,
    )
    rec = SimpleNamespace(
        gmail_id="g1", message_id="m1", subject="COI request",
        date="2026-09-26", facts=facts, attachments=[],
    )
    for k, val in kw.items():
        setattr(rec, k, val)
    return rec


class FakeDiscussions:
    def __init__(self, rows):
        self.rows = rows

    def get_discussions(self, applicant_id):
        return self.rows


class FakeVerifier:
    """Anchors POL123 to applicant 220250093, like a real PolicyApi hit."""

    def __init__(self, discussions=None):
        self._discussions = discussions or []

    def search_policies(self, policy_number):
        if policy_number == "POL123":
            return [{"policyNumber": "POL123", "accountId": 220250093}]
        return []

    def get_discussions(self, applicant_id):
        return self._discussions


class FakeNoteWriter:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "filed", "note_id": "n1",
                                 "discussion_id": "d1"}

    def __call__(self, applicant_id, note_text, **kw):
        self.calls.append((applicant_id, note_text, kw))
        if kw.get("dry_run"):
            return {"status": "dry_run", "discussion_id": "d1", "note_id": None}
        return dict(self.result)


class FakeDocWriter:
    def __init__(self):
        self.calls = []

    def __call__(self, applicant_id, document_name, file_bytes, **kw):
        self.calls.append((applicant_id, document_name, kw))
        return {"document_id": "doc9", "read_back": True}


class FakeDocSearcher:
    """Read-only document search fake: pretends these names are in Documents."""

    def __init__(self, names=()):
        self.names = set(names)
        self.calls = []

    def __call__(self, applicant_id):
        self.calls.append(applicant_id)
        return {"results": [{"documentName": n} for n in self.names]}


class ExplodingSearcher:
    """Simulates a read-back that can never complete."""

    def __call__(self, applicant_id):
        raise RuntimeError("search transport failed")


class NamedFlakyDocWriter(FakeDocWriter):
    """Fails the POST once for the named documents, then behaves.

    Simulates the classic failure: the server processed the upload but the
    transport dropped the response.
    """

    def __init__(self, fail_names=()):
        super().__init__()
        self.fail_names = set(fail_names)
        self.failed = set()

    def __call__(self, applicant_id, document_name, file_bytes, **kw):
        self.calls.append((applicant_id, document_name, kw))
        if document_name in self.fail_names \
                and document_name not in self.failed:
            self.failed.add(document_name)
            raise RuntimeError("transport failed")
        return {"document_id": "doc9", "read_back": True}


class AlwaysFailDocWriter(FakeDocWriter):
    def __call__(self, applicant_id, document_name, file_bytes, **kw):
        self.calls.append((applicant_id, document_name, kw))
        raise RuntimeError("transport failed")


class FakeZapier:
    def __init__(self, state="unknown"):
        self.state = state
        self.created = []
        self.reopened = []

    def get_task_state(self, task_id):
        return self.state

    def create_task(self, **kw):
        self.created.append(kw)
        return SimpleNamespace(fired=True, reason="fake-fired")

    def reopen_task(self, **kw):
        self.reopened.append(kw)
        return SimpleNamespace(fired=True, reason="fake-reopened")


def make_deps(tmp, **kw):
    os.makedirs(tmp, exist_ok=True)
    registry = TaskRegistry(os.path.join(tmp, "tasks.db"))
    store = FilingStore(os.path.join(tmp, "filing.db"))
    rows = [{"id": "d1", "title": "Certificate request - Big Client Inc",
             "noteCount": 2}]
    deps = FilingDeps(
        discussions_client=FakeDiscussions(rows),
        verifier=FakeVerifier(discussions=rows),
        note_writer=FakeNoteWriter(),
        doc_writer=FakeDocWriter(),
        doc_searcher=FakeDocSearcher(),
        zapier=FakeZapier(),
        registry=registry,
        store=store,
    )
    for k, val in kw.items():
        setattr(deps, k, val)
    return deps


# --- registry keys --------------------------------------------------------

def test_policy_and_holder_keys():
    assert policy_key_for(["pol 123", "POL123"]) == "POL123"
    assert policy_key_for([]) == "none"
    assert holder_key_for(["Big Client, Inc."]) == "bigclientinc"
    assert holder_key_for([]) == "none"


def test_registry_roundtrip(tmp_path):
    r = TaskRegistry(str(tmp_path / "t.db"))
    e = TaskEntry(applicant_id=1, policy_key="P1", holder_key="h1",
                  discussion_id="d1", task_id="t1", task_status=TASK_OPEN)
    r.put(e)
    got = r.get(1, "P1", "h1")
    assert got.task_id == "t1" and got.discussion_id == "d1"
    assert r.get(1, "P1", "nope") is None


# --- task decision rules ----------------------------------------------------

def test_decide_new_request_no_entry_creates():
    assert decide_task_action(ACTION_NEW_REQUEST, None) == CREATE


def test_decide_open_task_reuses():
    e = TaskEntry(1, "P", "h", task_id="t1", task_status=TASK_OPEN)
    assert decide_task_action(ACTION_NEW_REQUEST, e, TASK_OPEN) == REUSE


def test_decide_closed_task_reopens_never_duplicates():
    e = TaskEntry(1, "P", "h", task_id="t1", task_status=TASK_CLOSED)
    action = decide_task_action(ACTION_NEW_REQUEST, e, TASK_CLOSED)
    assert action == REOPEN
    assert action != CREATE


def test_decide_acknowledgement_leaves_task_alone():
    e = TaskEntry(1, "P", "h", task_id="t1", task_status=TASK_OPEN)
    assert decide_task_action(ACTION_ACK, e, TASK_OPEN) == NONE
    assert decide_task_action(ACTION_ACK, None) == NONE


def test_decide_unknown_state_fails_closed_to_reuse():
    e = TaskEntry(1, "P", "h", task_id="t1", task_status=TASK_OPEN)
    assert decide_task_action(ACTION_NEW_REQUEST, e, "unknown") == REUSE


def test_decide_unknown_action_holds():
    assert decide_task_action("unknown", None) == HOLD


# --- discussion resolution ---------------------------------------------------

def test_resolve_prefers_registry(tmp_path):
    deps = make_deps(str(tmp_path))
    deps.registry.put(TaskEntry(220250093, "POL123", "bigclientinc",
                                discussion_id="d-reg"))
    v = make_verified()
    did, title, how = resolve_discussion(v, ["Big Client Inc"], deps.registry,
                                         deps.discussions_client)
    assert did == "d-reg" and how.startswith("task registry")


def test_resolve_matches_holder_discussion(tmp_path):
    deps = make_deps(str(tmp_path))
    v = make_verified()
    did, title, how = resolve_discussion(v, ["Big Client Inc"], deps.registry,
                                         deps.discussions_client)
    assert did == "d1" and "Big Client" in (title or "")


def test_resolve_holds_when_nothing_matches(tmp_path):
    deps = make_deps(str(tmp_path))
    deps.discussions_client = FakeDiscussions(
        [{"id": "d9", "title": "Renewal follow-up", "noteCount": 1}])
    v = make_verified()
    did, title, reason = resolve_discussion(v, ["Big Client Inc"], deps.registry,
                                            deps.discussions_client)
    assert did is None and "no certificates discussion" in reason


def test_resolve_holds_on_ambiguous_matches(tmp_path):
    deps = make_deps(str(tmp_path))
    deps.discussions_client = FakeDiscussions([
        {"id": "d1", "title": "Certificate request - Big Client Inc"},
        {"id": "d2", "title": "COI for Big Client Inc renewal"},
    ])
    v = make_verified()
    did, title, reason = resolve_discussion(v, ["Big Client Inc"], deps.registry,
                                            deps.discussions_client)
    assert did is None and "refusing to guess" in reason
    assert "COI for Big Client Inc renewal" in reason


# --- filing pipeline ----------------------------------------------------------

def test_file_record_happy_path(tmp_path):
    deps = make_deps(str(tmp_path))
    res = file_record(make_record(), make_verified(), deps)
    assert res.status == FILED, res.hold_reasons
    assert res.note_id == "n1"
    assert res.task_action == CREATE
    assert len(deps.zapier.created) == 1
    entry = deps.registry.get(220250093, "POL123", "bigclientinc")
    assert entry is not None and entry.discussion_id == "d1"


def test_file_record_refuses_unverified():
    deps = make_deps(tempfile.mkdtemp())
    v = make_verified(status="HOLD")
    res = file_record(make_record(), v, deps)
    assert res.status == HELD
    assert not deps.note_writer.calls


def test_file_record_triple_guard_blocks_wrong_discussion(tmp_path):
    deps = make_deps(str(tmp_path))

    class BadVerifier:
        def search_policies(self, policy_number):
            return [{"policyNumber": "POL123", "accountId": 999}]

        def get_discussions(self, applicant_id):
            return [{"id": "d1",
                     "title": "Certificate request - Big Client Inc"}]

    deps.verifier = BadVerifier()
    res = file_record(make_record(), make_verified(), deps)
    assert res.status == HELD
    assert any("triple guard" in h for h in res.hold_reasons)
    assert not deps.note_writer.calls


def test_file_record_followup_reuses_open_task(tmp_path):
    deps = make_deps(str(tmp_path))
    deps.registry.put(TaskEntry(220250093, "POL123", "bigclientinc",
                                discussion_id="d1", task_id="zt1",
                                task_status=TASK_OPEN))
    deps.zapier = FakeZapier(state="open")
    res = file_record(make_record(), make_verified(), deps)
    assert res.task_action == REUSE
    assert res.task_id == "zt1"
    assert not deps.zapier.created  # no duplicate task


def test_file_record_acknowledgement_files_note_leaves_task(tmp_path):
    deps = make_deps(str(tmp_path))
    deps.registry.put(TaskEntry(220250093, "POL123", "bigclientinc",
                                discussion_id="d1", task_id="zt1",
                                task_status=TASK_OPEN))
    v = make_verified(requested_action=ACTION_ACK)
    res = file_record(make_record(), v, deps)
    assert res.task_action == NONE
    assert res.note_id == "n1"  # every email is still filed
    assert not deps.zapier.created


def test_file_record_uploads_pdf_attachments(tmp_path):
    att = SimpleNamespace(filename="request.pdf", mime_type="application/pdf",
                          content=b"%PDF-1.4 fake")
    deps = make_deps(str(tmp_path))
    res = file_record(make_record(attachments=[att]), make_verified(), deps)
    assert res.status == FILED, res.hold_reasons
    # email PDF first, then the attachment
    assert res.document_ids == ["doc9", "doc9"]
    assert deps.doc_writer.calls[0][1].startswith("COI request email - ")
    assert deps.doc_writer.calls[1][1] == "COI email attachment - request.pdf"


def test_file_record_uploads_email_pdf_first_and_all_attachment_types(tmp_path):
    """Defect 2+3: the email PDF is filed first and non-PDF attachments are
    no longer silently dropped."""
    jpg = SimpleNamespace(filename="photo.jpg", content=b"\xff\xd8 fake-jpg")
    pdf = SimpleNamespace(filename="request.pdf", content=b"%PDF-1.4 fake")
    deps = make_deps(str(tmp_path))
    res = file_record(make_record(attachments=[jpg, pdf]), make_verified(),
                      deps)
    assert res.status == FILED, res.hold_reasons
    names = [c[1] for c in deps.doc_writer.calls]
    assert names[0].startswith("COI request email - ")
    assert names[0].endswith(".pdf")
    assert names[1] == "COI email attachment - photo.jpg"
    assert names[2] == "COI email attachment - request.pdf"
    ctypes = [c[2]["content_type"] for c in deps.doc_writer.calls]
    assert ctypes[0] == "application/pdf"
    assert ctypes[1] == "image/jpeg"
    assert ctypes[2] == "application/pdf"
    assert res.document_ids == ["doc9", "doc9", "doc9"]


def test_file_record_note_names_filed_documents(tmp_path):
    jpg = SimpleNamespace(filename="photo.jpg", content=b"fake")
    deps = make_deps(str(tmp_path))
    res = file_record(make_record(attachments=[jpg]), make_verified(), deps)
    assert res.status == FILED, res.hold_reasons
    note_text = deps.note_writer.calls[0][1]
    assert "2 attachment(s) saved to the file" in note_text


def test_file_record_uncertain_upload_recovered_via_readback(tmp_path):
    """Defect 1: POST raised but the file landed. Read-back by name finds
    it — the code must NOT re-send."""
    att_name = "COI email attachment - photo.jpg"
    writer = NamedFlakyDocWriter(fail_names={att_name})
    searcher = FakeDocSearcher(names={att_name})
    deps = make_deps(str(tmp_path), doc_writer=writer, doc_searcher=searcher)
    jpg = SimpleNamespace(filename="photo.jpg", content=b"fake")
    res = file_record(make_record(attachments=[jpg]), make_verified(), deps)
    assert res.status == FILED, res.hold_reasons
    assert len(writer.calls) == 2  # email PDF + one attachment attempt only
    assert any("not re-sending" in e for e in res.evidence)


def test_file_record_uncertain_upload_absent_gets_one_resend(tmp_path):
    """Defect 1: POST raised and the file is verifiably absent — exactly one
    re-send is safe."""
    att_name = "COI email attachment - photo.jpg"
    writer = NamedFlakyDocWriter(fail_names={att_name})
    searcher = FakeDocSearcher(names=set())
    deps = make_deps(str(tmp_path), doc_writer=writer, doc_searcher=searcher)
    jpg = SimpleNamespace(filename="photo.jpg", content=b"fake")
    res = file_record(make_record(attachments=[jpg]), make_verified(), deps)
    assert res.status == FILED, res.hold_reasons
    assert len(writer.calls) == 3  # email PDF + failed attempt + one re-send
    assert any("re-send" in e for e in res.evidence)


def test_file_record_uncertain_upload_unsearchable_is_unverified(tmp_path):
    """Defect 1: POST raised and the read-back itself fails — fail closed as
    UNVERIFIED. No blind re-send, no task, email stays unread."""
    att_name = "COI email attachment - photo.jpg"
    writer = NamedFlakyDocWriter(fail_names={att_name})
    deps = make_deps(str(tmp_path), doc_writer=writer,
                     doc_searcher=ExplodingSearcher())
    jpg = SimpleNamespace(filename="photo.jpg", content=b"fake")
    res = file_record(make_record(attachments=[jpg]), make_verified(), deps)
    assert res.status == "ERROR"
    assert any("UNVERIFIED" in h for h in res.hold_reasons)
    assert len(writer.calls) == 2  # email PDF + one attempt — never retried
    assert not deps.zapier.created  # no task on an unverified filing


def test_file_record_repeated_upload_failure_is_unverified(tmp_path):
    """Defect 1: the re-send also fails — UNVERIFIED, not an infinite loop."""
    writer = AlwaysFailDocWriter()
    deps = make_deps(str(tmp_path), doc_writer=writer,
                     doc_searcher=FakeDocSearcher(names=set()))
    res = file_record(make_record(), make_verified(), deps)
    assert res.status == "ERROR"
    assert any("UNVERIFIED" in h for h in res.hold_reasons)
    # email PDF: first attempt + exactly one re-send, then stop
    assert len(writer.calls) == 2
    assert not deps.zapier.created


def test_file_record_uncertain_note_outcome_reads_back_first(tmp_path):
    rows = [{"id": "d1",
             "title": "Certificate request - Big Client Inc", "noteCount": 2}]

    class ExplodingWriter(FakeNoteWriter):
        """Simulates the classic failure: the server processed the POST,
        but the transport dropped the response."""

        def __call__(self, applicant_id, note_text, **kw):
            self.calls.append((applicant_id, note_text, kw))
            rows[0]["noteCount"] += 1  # the write DID land server-side
            raise RuntimeError("transport failed")

    client = FakeDiscussions(rows)
    deps = make_deps(str(tmp_path), note_writer=ExplodingWriter(),
                     discussions_client=client,
                     verifier=FakeVerifier(discussions=rows))
    res = file_record(make_record(), make_verified(), deps)
    # read-back shows the count moved -> recovered, never blind-retried
    assert res.note_id == "recovered-via-readback"
    assert any("not retrying" in e for e in res.evidence)
    assert len(deps.note_writer.calls) == 1  # exactly one attempt

    # and when the count did NOT move, the failure is held, not retried
    rows2 = [{"id": "d1",
              "title": "Certificate request - Big Client Inc", "noteCount": 5}]

    class CleanExplodingWriter(FakeNoteWriter):
        def __call__(self, applicant_id, note_text, **kw):
            raise RuntimeError("transport failed")

    deps2 = make_deps(str(tmp_path / "second"),
                      note_writer=CleanExplodingWriter(),
                      discussions_client=FakeDiscussions(rows2),
                      verifier=FakeVerifier(discussions=rows2))
    res2 = file_record(make_record(), make_verified(), deps2)
    assert res2.status == "ERROR"
    assert any("note write failed" in h for h in res2.hold_reasons)


def test_file_record_dry_run_writes_nothing(tmp_path):
    deps = make_deps(str(tmp_path))
    res = file_record(make_record(), make_verified(), deps, dry_run=True)
    assert res.status == DRY_RUN
    assert res.note_id is None


def test_lease_blocks_concurrent_worker(tmp_path):
    deps = make_deps(str(tmp_path))
    assert deps.store.claim("g1", "worker-a")
    assert not deps.store.claim("g1", "worker-b")
    res = file_record(make_record(), make_verified(), deps, owner="worker-b")
    assert res.status == HELD
    assert any("lease" in h for h in res.hold_reasons)


# --- zapier client --------------------------------------------------------------

def test_zapier_refuses_without_script():
    z = CertZapierClient(trigger_script="/nonexistent/zap-trigger")
    with pytest.raises(RuntimeError):
        z.create_task(applicant_id=1, title="t", email_subject="s",
                      note_text="n")


def test_zapier_reopen_raises_until_hook_exists(tmp_path):
    z = CertZapierClient(
        trigger_script=str(tmp_path / "x"), dry_run=True)
    with pytest.raises(RuntimeError, match="no Zapier reopen hook"):
        z.reopen_task(task_id="t1", applicant_id=1, title="t",
                      note_text="n")


def test_record_task_callback_updates_registry(tmp_path):
    r = TaskRegistry(str(tmp_path / "t.db"))
    CertZapierClient.record_task_callback(r, 7, "P1", "h1", "zap-123")
    got = r.get(7, "P1", "h1")
    assert got.task_id == "zap-123" and got.task_status == TASK_OPEN


def test_file_record_passes_resolved_title_as_hint(tmp_path):
    deps = make_deps(str(tmp_path))
    file_record(make_record(), make_verified(), deps)
    assert deps.note_writer.calls, "note writer was not called"
    _applicant, _text, kw = deps.note_writer.calls[0]
    assert kw.get("title_hint") == "Certificate request - Big Client Inc"
