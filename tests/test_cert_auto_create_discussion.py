"""Auto-created named discussions for certificate filing.

Carlo 2026-09-28: when a certificate request matches an applicant but no
existing discussion safely fits, the sweep auto-creates a named discussion
(POST v8/discussions/with-note) with the filing note as its first note —
no human gate. A follow-up for the same applicant + normalized holder
reuses the discussion instead of creating a duplicate.

Everything runs against fakes. No EZLynx, no Gmail, no network.
"""

import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine import ezlynx_write_scope as write_scope  # noqa: E402
from robie_job_engine.cert_filing import (  # noqa: E402
    AUTO_CREATE_REUSE_DAYS,
    DISCUSSION_AMBIGUOUS,
    DISCUSSION_NONE,
    DISCUSSION_RESOLVED,
    DRY_RUN,
    ERROR,
    FILED,
    FilingDeps,
    FilingStore,
    _auto_discussion_title,
    file_record,
    resolve_discussion,
)
from robie_job_engine.cert_task_registry import TaskRegistry  # noqa: E402
from robie_job_engine.cert_task_registry import holder_key_for  # noqa: E402
from robie_job_engine.cert_verification import (  # noqa: E402
    ACTION_NEW_REQUEST, VERIFIED, VerificationResult,
)
from robie_job_engine.ezlynx_discussions import (  # noqa: E402
    DiscussionApiError,
    build_with_note_payload,
    create_discussion_with_note,
)
from robie_job_engine.ezlynx_write_scope import (  # noqa: E402
    EzlynxWriteScopeError,
)

APP = 220250093          # allowlisted test applicant (default scope)
FOREIGN_APP = 999999991  # plausible id, NOT on the default allowlist


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeWithNoteClient:
    """Discussion client fake with with-note POST + GET read-back.

    Server-like: a successful with-note POST makes the new discussion show
    up in later get_discussions calls, like the real by-applicant list."""

    def __init__(self, rows=(), post_response=None, get_response=None,
                 post_error=None):
        self._rows = list(rows)
        self.posts = []           # (path, payload)
        self.gets = []            # discussion ids read back
        self.post_response = post_response
        self.get_response = get_response
        self.post_error = post_error

    def get_discussions(self, applicant_id):
        return list(self._rows)

    def get_discussion(self, discussion_id):
        self.gets.append(discussion_id)
        if callable(self.get_response):
            return self.get_response(discussion_id)
        return self.get_response

    def _post(self, path, payload):
        self.posts.append((path, payload))
        if self.post_error is not None:
            raise self.post_error
        resp = (self.post_response(path, payload)
                if callable(self.post_response) else self.post_response)
        if path == "v8/discussions/with-note" and isinstance(resp, dict):
            did = str(resp.get("DiscussionId") or
                      resp.get("discussionId") or "").strip()
            if did:
                self._rows.append({"id": did,
                                   "title": payload.get("title", ""),
                                   "noteCount": 1,
                                   "mostRecentNoteId": "n-first"})
        return resp


def make_verified(app_id=APP, **kw):
    v = VerificationResult(
        status=VERIFIED, applicant_id=app_id, insured_name="Acme LLC",
        policy_numbers=[], requested_action=ACTION_NEW_REQUEST,
        evidence=["dated applicant index hit"])
    for k, val in kw.items():
        setattr(v, k, val)
    return v


def make_record(**kw):
    facts = SimpleNamespace(
        insured_name="Acme LLC", policy_numbers=[],
        holder_names=["Big Client Inc"], requester_name="Bob",
        requester_email="bob@x.com", requester_is_third_party=False,
        pdf_unreadable=False,
    )
    rec = SimpleNamespace(
        gmail_id="g1", message_id="m1", thread_id="t1", subject="COI request",
        date="2026-09-26", facts=facts, attachments=[],
    )
    for k, val in kw.items():
        setattr(rec, k, val)
    return rec


class FakeVerifier:
    """Reads live from the client, like the real EzlynxReadClient."""

    def __init__(self, client):
        self._client = client

    def search_policies(self, policy_number):
        return []

    def get_discussions(self, applicant_id):
        return self._client.get_discussions(applicant_id)


class FakeNoteWriter:
    """Mimics add_note_to_discussion: picks the single discussion matching
    the title hint and reports its real id (the destination-agreement check
    in cert_filing depends on this)."""

    def __init__(self, client):
        self._client = client
        self.calls = []

    def __call__(self, applicant_id, note_text, **kw):
        self.calls.append((applicant_id, note_text, kw))
        if kw.get("dry_run"):
            return {"status": "dry_run", "discussion_id": None,
                    "note_id": None}
        hint = str(kw.get("title_hint") or "").strip().lower()
        rows = [r for r in self._client.get_discussions(applicant_id)
                if isinstance(r, dict) and str(r.get("title") or "").strip()]
        matched = [r for r in rows
                   if hint and hint in str(r.get("title")).lower()]
        chosen = matched if len(matched) == 1 else (
            rows if len(rows) == 1 else [])
        if len(chosen) != 1:
            return {"status": "pending", "reason": "no single discussion",
                    "discussion_id": None, "note_id": None}
        did = str(chosen[0].get("id"))
        return {"status": "filed", "note_id": "n-appended",
                "discussion_id": did, "read_back": True}


class FakeDocStore:
    """Writer + searcher sharing one document set, like EZLynx itself."""

    def __init__(self):
        self.names = set()
        self.write_calls = []

    def write(self, applicant_id, document_name, file_bytes, **kw):
        self.write_calls.append((applicant_id, document_name, kw))
        self.names.add(document_name)
        return {"document_id": "doc9", "read_back": True}

    def search(self, applicant_id):
        return {"results": [{"documentName": n} for n in self.names]}


class FakeRegistry:
    def __init__(self):
        self.entries = {}

    def get(self, *a):
        return self.entries.get(tuple(a))

    def put(self, entry):
        self.entries[(entry.applicant_id, entry.policy_key,
                      entry.holder_key)] = entry


class FakeZapier:
    def __init__(self):
        self.created = []

    def get_task_state(self, task_id):
        return "unknown"

    def create_task(self, **kw):
        self.created.append(kw)
        return SimpleNamespace(fired=True)


class FakeTaskProver:
    def __init__(self, task_id="task123", assignee="SCanales"):
        self.task_id = task_id
        self.assignee = assignee

    def __call__(self, applicant_id, task_title):
        return {"task_id": self.task_id, "assignee": self.assignee}


def make_deps(tmp, client, **kw):
    os.makedirs(tmp, exist_ok=True)
    doc_store = FakeDocStore()
    deps = FilingDeps(
        discussions_client=client,
        verifier=FakeVerifier(client),
        note_writer=FakeNoteWriter(client),
        doc_writer=doc_store.write,
        doc_searcher=doc_store.search,
        zapier=FakeZapier(),
        registry=FakeRegistry(),
        store=FilingStore(os.path.join(tmp, "filing.db")),
        task_prover=FakeTaskProver(),
    )
    deps.doc_store = doc_store
    for k, val in kw.items():
        setattr(deps, k, val)
    return deps


def success_client(rows=(), new_id="d-new"):
    title_holder = {}

    def post_response(path, payload):
        title_holder["title"] = payload["title"]
        return {"DiscussionId": new_id}

    def get_response(discussion_id):
        return {"id": discussion_id, "title": title_holder.get("title"),
                "noteCount": 1, "mostRecentNoteId": "n-first"}

    return FakeWithNoteClient(rows=rows, post_response=post_response,
                              get_response=get_response)


# ---------------------------------------------------------------------------
# unit: payload shape and create guards
# ---------------------------------------------------------------------------

def test_with_note_payload_exact_shape():
    payload = build_with_note_payload(220250093, "COI Request — X — 2026-09-26",
                                      "the note")
    assert payload == {
        "applicantId": "220250093",
        "title": "COI Request — X — 2026-09-26",
        "note": {"type": "Note", "body": "the note"},
    }


def test_with_note_payload_rejects_untitled_and_empty():
    with pytest.raises(DiscussionApiError):
        build_with_note_payload(APP, "Untitled", "body")
    with pytest.raises(DiscussionApiError):
        build_with_note_payload(APP, "  ", "body")
    with pytest.raises(DiscussionApiError):
        build_with_note_payload(APP, "Title", "   ")


def test_create_refuses_non_allowlisted_applicant_before_http():
    client = success_client()
    with pytest.raises(EzlynxWriteScopeError):
        create_discussion_with_note(client, FOREIGN_APP, "COI Request — X",
                                    "body")
    assert client.posts == [] and client.gets == []


def test_create_refuses_phone_number_in_note():
    client = success_client()
    with pytest.raises(DiscussionApiError):
        create_discussion_with_note(client, APP, "COI Request — X",
                                    "call me at 555-123-4567 about this")
    assert client.posts == [] and client.gets == []


def test_create_dry_run_posts_nothing():
    client = success_client()
    result = create_discussion_with_note(client, APP, "COI Request — X",
                                         "body", dry_run=True)
    assert result["status"] == "dry_run"
    assert client.posts == [] and client.gets == []


def test_create_success_read_back_proves_title_and_note_count():
    client = success_client(new_id="d-42")
    title = "COI Request — Big Client Inc — 2026-09-26"
    result = create_discussion_with_note(client, APP, title, "filing note")
    assert result["status"] == "created"
    assert result["discussion_id"] == "d-42"
    assert result["discussion_title"] == title
    assert result["note_id"] == "n-first"
    assert result["read_back"] is True
    path, payload = client.posts[0]
    assert path == "v8/discussions/with-note"
    assert payload["title"] == title
    assert payload["note"]["body"] == "filing note"
    assert client.gets == ["d-42"]


def test_create_missing_discussion_id_raises():
    client = FakeWithNoteClient(post_response={},
                                get_response={"id": "x", "title": "t",
                                              "noteCount": 1})
    with pytest.raises(DiscussionApiError):
        create_discussion_with_note(client, APP, "COI Request — X", "body")


def test_create_read_back_title_mismatch_raises():
    def get_response(discussion_id):
        return {"id": discussion_id, "title": "Something Else", "noteCount": 1}

    client = FakeWithNoteClient(post_response={"DiscussionId": "d-1"},
                                get_response=get_response)
    with pytest.raises(DiscussionApiError):
        create_discussion_with_note(client, APP, "COI Request — X", "body")


def test_create_read_back_zero_notes_raises():
    def get_response(discussion_id):
        return {"id": discussion_id, "title": "COI Request — X", "noteCount": 0}

    client = FakeWithNoteClient(post_response={"DiscussionId": "d-1"},
                                get_response=get_response)
    with pytest.raises(DiscussionApiError):
        create_discussion_with_note(client, APP, "COI Request — X", "body")


def test_create_read_back_absent_raises():
    client = FakeWithNoteClient(post_response={"DiscussionId": "d-1"},
                                get_response={})
    with pytest.raises(DiscussionApiError):
        create_discussion_with_note(client, APP, "COI Request — X", "body")


# ---------------------------------------------------------------------------
# resolve_discussion outcome codes
# ---------------------------------------------------------------------------

def _v(app_id=APP):
    return SimpleNamespace(applicant_id=app_id, policy_numbers=[])


def test_resolve_discussion_codes():
    did, title, how, code = resolve_discussion(
        _v(), ["Big Client Inc"], FakeRegistry(), FakeWithNoteClient(rows=[]))
    assert did is None and code == DISCUSSION_NONE

    rows = [{"id": "d1", "title": "COI for Big Client Inc"},
            {"id": "d2", "title": "Certificate — Big Client Inc renewal"}]
    did, title, how, code = resolve_discussion(
        _v(), ["Big Client Inc"], FakeRegistry(), FakeWithNoteClient(rows=rows))
    assert did is None and code == DISCUSSION_AMBIGUOUS

    rows = [{"id": "d1", "title": "COI for Big Client Inc"}]
    did, title, how, code = resolve_discussion(
        _v(), ["Big Client Inc"], FakeRegistry(), FakeWithNoteClient(rows=rows))
    assert did == "d1" and code == DISCUSSION_RESOLVED


# ---------------------------------------------------------------------------
# title format
# ---------------------------------------------------------------------------

def test_auto_discussion_title_format():
    rec = make_record(date="2026-09-26")
    assert _auto_discussion_title(["Big Client Inc"], rec) == \
        "COI Request — Big Client Inc — 2026-09-26"
    assert _auto_discussion_title([], rec) == \
        "COI Request — Certificate — 2026-09-26"


# ---------------------------------------------------------------------------
# filing: no discussion -> auto-create
# ---------------------------------------------------------------------------

def _file(record, deps, verified=None, dry_run=False):
    return file_record(record, verified or make_verified(), deps,
                       dry_run=dry_run)


def test_no_discussion_auto_creates_with_filing_note(tmp_path):
    tmp = str(tmp_path)
    client = success_client(rows=[])
    deps = make_deps(tmp, client)

    rec = make_record(message_id="m1", thread_id="t1")
    res = _file(rec, deps)

    assert res.status == FILED, res.hold_reasons
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "v8/discussions/with-note"
    assert payload["title"] == "COI Request — Big Client Inc — 2026-09-26"
    # the filing note is the first note: it names the filed email PDF
    assert "Certificate request" in payload["note"]["body"]
    assert "attachment(s) saved to the file" in payload["note"]["body"]
    assert res.discussion_id == "d-new"
    assert res.note_id == "n-first"
    # the note is NOT appended a second time by the note writer
    assert deps.note_writer.calls == []
    # ledger recorded for the 7-day reuse window
    entry = deps.store.auto_discussion_get(APP, holder_key_for(["Big Client Inc"]))
    assert entry is not None and entry["discussion_id"] == "d-new"
    # store marks the note written so a rerun does not duplicate it
    state = deps.store.get("g1")
    assert state["note_status"] == "written"
    assert state["discussion_id"] == "d-new"
    # the Zapier task flow is untouched
    assert len(deps.zapier.created) == 1


def test_ambiguous_discussions_auto_create(tmp_path):
    tmp = str(tmp_path)
    rows = [{"id": "d1", "title": "COI for Big Client Inc"},
            {"id": "d2", "title": "Certificate — Big Client Inc renewal"}]
    client = success_client(rows=rows)
    deps = make_deps(tmp, client)

    res = _file(make_record(), deps)

    assert res.status == FILED, res.hold_reasons
    assert len(client.posts) == 1
    assert res.discussion_id == "d-new"


def test_write_scope_refusal_holds_unverified(tmp_path):
    tmp = str(tmp_path)
    client = success_client(rows=[])
    deps = make_deps(tmp, client)
    rec = make_record()
    res = file_record(rec, make_verified(app_id=FOREIGN_APP), deps)
    assert res.status == ERROR
    assert any("UNVERIFIED" in h for h in res.hold_reasons)
    assert client.posts == []
    assert deps.store.auto_discussion_get(
        FOREIGN_APP, holder_key_for(["Big Client Inc"])) is None


def test_creation_failure_holds_unverified(tmp_path):
    tmp = str(tmp_path)
    client = FakeWithNoteClient(rows=[],
                                post_error=RuntimeError("transport blew up"),
                                get_response=None)
    deps = make_deps(tmp, client)

    rec = make_record(message_id="m1", thread_id="t1")
    res = _file(rec, deps)

    assert res.status == ERROR
    assert any("UNVERIFIED" in h for h in res.hold_reasons), res.hold_reasons
    state = deps.store.get("g1")
    assert state["note_status"] != "written"
    assert deps.store.auto_discussion_get(
        APP, holder_key_for(["Big Client Inc"])) is None
    # nothing filed: no task, no note
    assert deps.zapier.created == []
    assert deps.note_writer.calls == []


def test_dry_run_creates_nothing(tmp_path):
    tmp = str(tmp_path)
    client = success_client(rows=[])
    deps = make_deps(tmp, client)

    res = _file(make_record(), deps, dry_run=True)

    assert res.status == DRY_RUN
    assert client.posts == []
    assert deps.store.auto_discussion_get(
        APP, holder_key_for(["Big Client Inc"])) is None


# ---------------------------------------------------------------------------
# filing: dedup — reuse, never duplicate
# ---------------------------------------------------------------------------

def test_same_thread_reuses_discussion(tmp_path):
    tmp = str(tmp_path)
    client = success_client(rows=[])
    deps = make_deps(tmp, client)

    res1 = _file(make_record(message_id="m1", thread_id="t9"), deps)
    assert res1.status == FILED, res1.hold_reasons
    assert len(client.posts) == 1

    # follow-up email in the same thread: no new discussion
    rec2 = make_record(message_id="m2", thread_id="t9", gmail_id="g2",
                       subject="Re: COI request")
    res2 = _file(rec2, deps)
    assert res2.status == FILED, res2.hold_reasons
    assert len(client.posts) == 1
    assert res2.discussion_id == "d-new"
    # the second email's own note IS appended (it was never filed)
    assert len(deps.note_writer.calls) == 1


def test_seven_day_same_holder_reuses(tmp_path):
    tmp = str(tmp_path)
    client = success_client(rows=[])
    deps = make_deps(tmp, client)

    res1 = _file(make_record(message_id="m1", thread_id="t1"), deps)
    assert res1.status == FILED, res1.hold_reasons

    # new thread, same applicant + holder, within the reuse window
    rec2 = make_record(message_id="m2", thread_id="t2", gmail_id="g2")
    res2 = _file(rec2, deps)
    assert res2.status == FILED, res2.hold_reasons
    assert len(client.posts) == 1
    assert res2.discussion_id == "d-new"


def test_different_holder_creates_new_discussion(tmp_path):
    tmp = str(tmp_path)
    client = success_client(rows=[])
    deps = make_deps(tmp, client)

    res1 = _file(make_record(message_id="m1", thread_id="t1"), deps)
    assert res1.status == FILED, res1.hold_reasons

    facts = SimpleNamespace(
        insured_name="Acme LLC", policy_numbers=[],
        holder_names=["Other Corp"], requester_name="Bob",
        requester_email="bob@x.com", requester_is_third_party=False,
        pdf_unreadable=False)
    rec2 = make_record(message_id="m2", thread_id="t2", gmail_id="g2",
                       facts=facts)
    res2 = _file(rec2, deps)
    assert res2.status == FILED, res2.hold_reasons
    assert len(client.posts) == 2
    titles = [p[1]["title"] for p in client.posts]
    assert titles[0] == "COI Request — Big Client Inc — 2026-09-26"
    assert titles[1] == "COI Request — Other Corp — 2026-09-26"


def test_stale_ledger_creates_new_discussion(tmp_path):
    tmp = str(tmp_path)
    client = success_client(rows=[])
    deps = make_deps(tmp, client)

    # seed a ledger row 8 days old — outside the reuse window
    deps.store.auto_discussion_record(
        APP, holder_key_for(["Big Client Inc"]), "d-old", "old title")
    deps.store._db.execute(
        "UPDATE cert_auto_discussions SET created_at=?",
        (time.time() - 8 * 86400,))
    deps.store._db.commit()
    # the stale discussion still reads back fine; anything else reads back
    # like a freshly created discussion
    def get_response(did):
        if did == "d-old":
            return {"id": did, "title": "old title", "noteCount": 5}
        return {"id": did, "title": "COI Request — Big Client Inc — 2026-09-26",
                "noteCount": 1, "mostRecentNoteId": "n-first"}

    client.get_response = get_response

    res = _file(make_record(message_id="m1", thread_id="t1"), deps)
    assert res.status == FILED, res.hold_reasons
    assert len(client.posts) == 1
    assert res.discussion_id == "d-new"


def test_exact_title_on_applicant_reuses_without_post(tmp_path):
    """A create whose ledger row never landed is caught by the exact-title
    lookup in EZLynx — no duplicate discussion."""
    tmp = str(tmp_path)
    title = "COI Request — Big Client Inc — 2026-09-26"
    rows = [{"id": "d-crash", "title": title, "noteCount": 3}]
    client = FakeWithNoteClient(
        rows=rows, post_response={"DiscussionId": "d-dupe"},
        get_response=lambda did: {"id": did, "title": title, "noteCount": 3})
    deps = make_deps(tmp, client)

    res = _file(make_record(message_id="m1", thread_id="t1"), deps)
    assert res.status == FILED, res.hold_reasons
    assert client.posts == []
    assert res.discussion_id == "d-crash"
    # normal flow: the note is appended to the existing discussion
    assert len(deps.note_writer.calls) == 1


# ---------------------------------------------------------------------------
# store: ledger roundtrip
# ---------------------------------------------------------------------------

def test_auto_discussion_ledger_roundtrip(tmp_path):
    store = FilingStore(str(tmp_path / "f.db"))
    assert store.auto_discussion_get(APP, "bigclientinc") is None
    store.auto_discussion_record(APP, "bigclientinc", "d-7", "Some Title")
    got = store.auto_discussion_get(APP, "bigclientinc")
    assert got["discussion_id"] == "d-7"
    assert got["title"] == "Some Title"
    assert got["created_at"] > 0
    # different holder is a different key
    assert store.auto_discussion_get(APP, "othercorp") is None


def test_thread_discussion_lookup(tmp_path):
    store = FilingStore(str(tmp_path / "f.db"))
    assert store.thread_discussion("t1") is None
    store.claim("m1", "owner", thread_id="t1")
    store.set("m1", discussion_id="d-1")
    store.claim("m2", "owner", thread_id="t1")
    assert store.thread_discussion("t1", exclude_message_id="m2") == "d-1"
    assert store.thread_discussion("t1", exclude_message_id="m1") is None
    assert store.thread_discussion("other") is None


def test_claim_records_thread_id(tmp_path):
    store = FilingStore(str(tmp_path / "f.db"))
    assert store.claim("m1", "owner", thread_id="t5") is True
    row = store._db.execute(
        "SELECT thread_id FROM cert_filing WHERE message_id='m1'").fetchone()
    assert row[0] == "t5"


def test_reuse_window_constant():
    assert AUTO_CREATE_REUSE_DAYS == 7


def test_allowlist_default_is_test_only():
    # The standing default: only the test applicant may receive writes.
    assert write_scope.TEST_EZLYNX_WRITE_APPLICANT_ID == "220250093"
