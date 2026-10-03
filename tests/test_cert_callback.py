"""Chunk 4 tests: nonce-guarded Zap callback proof (cert_callback).

Everything runs against fakes. No EZLynx, no Gmail, no Zapier, no network.

Covers:
- filing_id uniqueness
- callback email parsing (valid / wrong subject / missing fields)
- callback validation: happy path, unknown nonce, replay, applicant
  mismatch, wrong assignee, closed status, and empty task_id ACCEPTED
  (the Zap's Create Note step exposes no mappable ID — the validated
  callback itself is the proof)
- the task_prover seam backed by validated callbacks
- email ingestion (accept + skip-already-processed)
- the re-drive gate: a pending fire HOLDs instead of re-firing the Zap
  (no duplicate EZLynx task), and a validated callback completes the
  filing without a second fire
- a stale same-title proof from an older fire never validates a new fire
"""

import base64
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_callback import (
    ASSIGNEE_SCANALES,
    CALLBACK_SUBJECT_PREFIX,
    CallbackStore,
    callback_task_prover,
    ingest_callback_emails,
    new_filing_id,
    parse_callback_email,
    require_callback_auth,
)
from robie_job_engine.cert_filing import (
    ERROR, FILED, FilingDeps, FilingStore, file_record,
)
from robie_job_engine.cert_task_registry import TaskRegistry, policy_key_for
from robie_job_engine.cert_verification import (
    ACTION_NEW_REQUEST, VERIFIED, VerificationResult,
)

# Reuse the chunk-3 fakes for the filing pipeline.
sys.path.insert(0, os.path.dirname(__file__))
from test_cert_filing import (  # noqa: E402
    FakeDocSearcher,
    FakeDocWriter,
    FakeDiscussions,
    FakeNoteWriter,
    FakeTaskProver,
    FakeVerifier,
    FakeZapier,
    StatefulFakeDocStore,
    make_record,
    make_verified,
)


APPLICANT = 220250093
TITLE = "Certificate request — Big Client Inc"


def make_store(tmp_path):
    return CallbackStore(str(tmp_path / "cb.db"))


def fire(store, filing_id="fire-1", applicant_id=APPLICANT,
         policy_key="POL123", holder_key="bigclientinc", title=TITLE,
         assignee=ASSIGNEE_SCANALES):
    store.record_fire(filing_id=filing_id, applicant_id=applicant_id,
                      policy_key=policy_key, holder_key=holder_key,
                      title=title, assignee=assignee)
    return filing_id


def good_body(filing_id="fire-1", task_id="t-999", applicant_id=APPLICANT,
              assignee=ASSIGNEE_SCANALES, status="open", title=TITLE):
    return (f"filing_id: {filing_id}\n"
            f"task_id: {task_id}\n"
            f"applicant_id: {applicant_id}\n"
            f"assignee: {assignee}\n"
            f"status: {status}\n"
            f"title: {title}\n")


# --- filing_id ------------------------------------------------------------

def test_filing_ids_are_unique():
    assert new_filing_id() != new_filing_id()


# --- email parsing ---------------------------------------------------------

def test_parse_callback_email_happy_path():
    fields = parse_callback_email(
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1",
        good_body())
    assert fields is not None
    assert fields["filing_id"] == "fire-1"
    assert fields["task_id"] == "t-999"
    assert fields["assignee"] == "SCanales"


def test_parse_callback_email_wrong_subject_is_none():
    assert parse_callback_email("COI request", good_body()) is None


def test_parse_callback_email_missing_field_is_none():
    body = good_body().replace("filing_id: fire-1\n", "")
    assert parse_callback_email(
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1", body) is None


def test_parse_callback_email_missing_task_id_still_parses():
    """task_id is opportunistic (the Zap's Create Note step exposes no
    mappable ID), so its absence must not reject the email."""
    body = good_body().replace("task_id: t-999\n", "")
    fields = parse_callback_email(
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1", body)
    assert fields is not None
    assert fields["filing_id"] == "fire-1"
    assert "task_id" not in fields


def test_parse_callback_email_never_raises():
    assert parse_callback_email(None, None) is None


# --- validation -------------------------------------------------------------

def test_validate_callback_happy_path(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    ok, reason = store.validate_callback(
        filing_id="fire-1", task_id="t-999", applicant_id=APPLICANT,
        assignee="SCanales", status="open")
    assert ok, reason
    proof = store.get_proof(APPLICANT, "POL123", "bigclientinc")
    assert proof is not None
    assert proof.task_id == "t-999"
    assert proof.assignee == "SCanales"
    # consumed: no longer pending
    assert store.find_pending(APPLICANT, "POL123", "bigclientinc") is None


def test_validate_callback_unknown_filing_id_rejected(tmp_path):
    store = make_store(tmp_path)
    ok, reason = store.validate_callback(
        filing_id="nope", task_id="t-1", applicant_id=APPLICANT,
        assignee="SCanales", status="open")
    assert not ok
    assert "no fire" in reason


def test_validate_callback_replay_rejected(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    ok, _ = store.validate_callback(
        filing_id="fire-1", task_id="t-999", applicant_id=APPLICANT,
        assignee="SCanales", status="open")
    assert ok
    ok2, reason2 = store.validate_callback(
        filing_id="fire-1", task_id="t-999", applicant_id=APPLICANT,
        assignee="SCanales", status="open")
    assert not ok2
    assert "replay" in reason2


def test_validate_callback_applicant_mismatch_rejected(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    ok, reason = store.validate_callback(
        filing_id="fire-1", task_id="t-999", applicant_id=999,
        assignee="SCanales", status="open")
    assert not ok
    assert "applicant" in reason


def test_validate_callback_wrong_assignee_rejected(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    ok, reason = store.validate_callback(
        filing_id="fire-1", task_id="t-999", applicant_id=APPLICANT,
        assignee="SomeoneElse", status="open")
    assert not ok
    assert "SCanales" in reason


def test_validate_callback_empty_task_id_accepted(tmp_path):
    """No task_id is fine: the Zap's EZLynx step is Create Note with no
    mappable ID output (verified 2026-09-27). The validated callback
    itself — filing nonce + applicant + assignee — is the proof."""
    store = make_store(tmp_path)
    fire(store)
    ok, reason = store.validate_callback(
        filing_id="fire-1", task_id="  ", applicant_id=APPLICANT,
        assignee="SCanales", status="open")
    assert ok, reason
    proof = store.get_proof(APPLICANT, "POL123", "bigclientinc")
    assert proof is not None
    assert proof.task_id == ""
    assert proof.assignee == "SCanales"


def test_validate_callback_closed_status_rejected(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    ok, reason = store.validate_callback(
        filing_id="fire-1", task_id="t-999", applicant_id=APPLICANT,
        assignee="SCanales", status="closed")
    assert not ok
    assert "status" in reason


# --- prover seam -------------------------------------------------------------

def test_prover_empty_without_proof(tmp_path):
    store = make_store(tmp_path)
    prove = callback_task_prover(store)
    assert prove(APPLICANT, TITLE) == {}


def test_prover_returns_validated_proof(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    store.validate_callback(
        filing_id="fire-1", task_id="t-999", applicant_id=APPLICANT,
        assignee="SCanales", status="open")
    prove = callback_task_prover(store)
    assert prove(APPLICANT, TITLE) == {"task_id": "t-999",
                                      "assignee": "SCanales"}


# --- ingestion ---------------------------------------------------------------

class FakeGmail:
    """Minimal intake port: list_message_ids + get_full_message."""

    def __init__(self, messages):
        # messages: gmail_id -> (subject, body) or (subject, body, from)
        self._messages = dict(messages)

    def list_message_ids(self, query, max_results=25):
        return list(self._messages)[:max_results]

    def get_full_message(self, gmail_id):
        msg = self._messages[gmail_id]
        subject, body = msg[0], msg[1]
        from_addr = msg[2] if len(msg) > 2 else ""
        raw = base64.urlsafe_b64encode(body.encode()).decode()
        headers = [{"name": "Subject", "value": subject}]
        if from_addr:
            headers.append({"name": "From", "value": from_addr})
        return {
            "payload": {
                "headers": headers,
                "parts": [{"mimeType": "text/plain",
                           "body": {"data": raw}}],
            },
        }


def test_ingest_accepts_callback_email(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1", good_body())})
    stats = ingest_callback_emails(gmail=gmail, store=store)
    assert stats["accepted"] == 1
    assert store.get_proof(APPLICANT, "POL123", "bigclientinc").task_id == \
        "t-999"


def test_ingest_skips_already_processed(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1", good_body())})
    ingest_callback_emails(gmail=gmail, store=store)
    stats = ingest_callback_emails(gmail=gmail, store=store)
    assert stats["accepted"] == 0  # replay rejected AND email skipped


def test_ingest_rejects_forged_callback(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1",
        good_body(assignee="Mallory"))})
    stats = ingest_callback_emails(gmail=gmail, store=store)
    assert stats["rejected"] == 1
    assert store.get_proof(APPLICANT, "POL123", "bigclientinc") is None


# --- sender authenticity -----------------------------------------------------

def test_ingest_rejects_wrong_sender(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1",
        good_body(), "Mallory <mallory@evil.example>")})
    stats = ingest_callback_emails(
        gmail=gmail, store=store, expected_senders="zapier.com")
    assert stats["rejected"] == 1
    assert stats["accepted"] == 0
    assert store.get_proof(APPLICANT, "POL123", "bigclientinc") is None


def test_ingest_rejects_missing_from_when_allowlisted(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1", good_body())})
    stats = ingest_callback_emails(
        gmail=gmail, store=store, expected_senders="zapier.com")
    assert stats["rejected"] == 1
    assert store.get_proof(APPLICANT, "POL123", "bigclientinc") is None


def test_ingest_accepts_allowlisted_domain_sender(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1",
        good_body(), "Zapier <zapier@mail.zapier.com>")})
    stats = ingest_callback_emails(
        gmail=gmail, store=store, expected_senders="zapier.com")
    assert stats["accepted"] == 1


def test_ingest_accepts_allowlisted_full_address(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1",
        good_body(), "zapier@zapier.com")})
    stats = ingest_callback_emails(
        gmail=gmail, store=store,
        expected_senders="noreply@other.com, zapier@zapier.com")
    assert stats["accepted"] == 1


def test_ingest_skips_sender_check_when_unconfigured(tmp_path):
    # No allowlist: any sender (or none) is fine — the nonce is the proof.
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1",
        good_body(), "anyone@anywhere.example")})
    stats = ingest_callback_emails(gmail=gmail, store=store,
                                   expected_senders="")
    assert stats["accepted"] == 1


def test_ingest_rejects_wrong_auth_secret(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1",
        good_body() + "auth: wrong-secret\n")})
    stats = ingest_callback_emails(gmail=gmail, store=store,
                                   auth_secret="correct-secret")
    assert stats["rejected"] == 1
    assert stats["accepted"] == 0
    assert store.get_proof(APPLICANT, "POL123", "bigclientinc") is None


def test_ingest_rejects_missing_auth_secret(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1", good_body())})
    stats = ingest_callback_emails(gmail=gmail, store=store,
                                   auth_secret="correct-secret")
    assert stats["rejected"] == 1
    assert store.get_proof(APPLICANT, "POL123", "bigclientinc") is None


def test_ingest_accepts_correct_auth_secret(tmp_path):
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1",
        good_body() + "auth: correct-secret\n")})
    stats = ingest_callback_emails(gmail=gmail, store=store,
                                   auth_secret="correct-secret")
    assert stats["accepted"] == 1
    assert store.get_proof(APPLICANT, "POL123", "bigclientinc") is not None


def test_ingest_skips_secret_check_when_unconfigured(tmp_path):
    # No secret configured: a body without auth is fine.
    store = make_store(tmp_path)
    fire(store)
    gmail = FakeGmail({"m1": (
        f"{CALLBACK_SUBJECT_PREFIX} filing_id=fire-1", good_body())})
    stats = ingest_callback_emails(gmail=gmail, store=store,
                                   auth_secret="")
    assert stats["accepted"] == 1


# --- re-drive gate: no duplicate Zap fire ------------------------------------

class FilingIdFakeZapier(FakeZapier):
    """Fake Zapier that mints filing_ids like the real client."""

    def create_task(self, **kw):
        filing_id = new_filing_id()
        self.created.append({**kw, "filing_id": filing_id})
        return SimpleNamespace(fired=True, reason="fake-fired",
                              filing_id=filing_id)


def make_cb_deps(tmp, **kw):
    os.makedirs(tmp, exist_ok=True)
    cb_store = CallbackStore(os.path.join(tmp, "cb.db"))
    rows = [{"id": "d1", "title": "Certificate request - Big Client Inc",
             "noteCount": 2}]
    doc_store = StatefulFakeDocStore()

    class TwoPolicyVerifier(FakeVerifier):
        """Anchors POL123 and POL999 to applicant 220250093."""

        def search_policies(self, policy_number):
            if policy_number in ("POL123", "POL999"):
                return [{"policyNumber": policy_number,
                         "accountId": APPLICANT}]
            return []

    deps = FilingDeps(
        discussions_client=FakeDiscussions(rows),
        verifier=TwoPolicyVerifier(discussions=rows),
        note_writer=FakeNoteWriter(),
        doc_writer=doc_store.write,
        doc_searcher=doc_store.search,
        zapier=FilingIdFakeZapier(),
        registry=TaskRegistry(os.path.join(tmp, "tasks.db")),
        store=FilingStore(os.path.join(tmp, "filing.db")),
        callback_store=cb_store,
        task_prover=callback_task_prover(cb_store),
    )
    deps.doc_store = doc_store
    for k, val in kw.items():
        setattr(deps, k, val)
    return deps


def test_redrive_holds_on_pending_fire_without_refire(tmp_path):
    """Sweep 1 fires the Zap (UNVERIFIED, no callback yet). Sweep 2's
    re-drive must HOLD on the pending fire — never fire the Zap again."""
    deps = make_cb_deps(str(tmp_path))
    record, verified = make_record(), make_verified()

    res1 = file_record(record, verified, deps)
    assert res1.status == ERROR  # no callback yet
    assert len(deps.zapier.created) == 1
    filing_id = deps.zapier.created[0].get("filing_id")
    assert filing_id  # the nonce went into the payload

    res2 = file_record(make_record(), make_verified(), deps)
    assert res2.status == ERROR
    assert any("awaiting its callback" in h for h in res2.hold_reasons)
    assert len(deps.zapier.created) == 1, \
        "re-drive must not fire the Zap a second time"


def test_validated_callback_completes_filing_without_refire(tmp_path):
    """Callback arrives between sweeps: re-drive completes FILED with the
    proven task_id, still exactly one Zap fire."""
    deps = make_cb_deps(str(tmp_path))
    cb_store = deps.callback_store

    res1 = file_record(make_record(), make_verified(), deps)
    assert res1.status == ERROR
    filing_id = deps.zapier.created[0]["filing_id"]

    ok, reason = cb_store.validate_callback(
        filing_id=filing_id, task_id="t-4242", applicant_id=APPLICANT,
        assignee="SCanales", status="open")
    assert ok, reason

    res2 = file_record(make_record(), make_verified(), deps)
    assert res2.status == FILED, res2.hold_reasons
    assert res2.task_id == "t-4242"
    assert len(deps.zapier.created) == 1


def test_stale_same_title_proof_never_validates_new_fire(tmp_path):
    """An older fire's proof (same applicant + title, different policy)
    must not count as proof for a new fire: only the new fire's own
    filing_id validates."""
    deps = make_cb_deps(str(tmp_path))
    cb_store = deps.callback_store

    # Older fire, same title, proven long ago.
    cb_store.record_fire(filing_id="fire-old", applicant_id=APPLICANT,
                         policy_key="POL123", holder_key="bigclientinc",
                         title=TITLE, assignee="SCanales")
    ok, _ = cb_store.validate_callback(
        filing_id="fire-old", task_id="t-old", applicant_id=APPLICANT,
        assignee="SCanales", status="open")
    assert ok

    # New request (different policy) fires fresh.
    verified = make_verified(policy_numbers=["POL999"])
    res = file_record(make_record(), verified, deps)
    assert res.status == ERROR  # no callback for the NEW fire yet
    assert res.task_id is None  # the stale t-old proof was not used
    new_filing = deps.zapier.created[0]["filing_id"]
    assert cb_store.get_proof_by_filing(new_filing) is None


# ---------------------------------------------------------------------------
# require_callback_auth: fail closed unless sender/secret auth is configured.
# The sweep must not file anything (no document, note, or Zap write) when
# the callback cannot be authenticated.
# ---------------------------------------------------------------------------

def _set_callback_auth(monkeypatch, senders="zapier@zapier.com",
                       secret="test-secret-123"):
    if senders is None:
        monkeypatch.delenv("CERT_CALLBACK_SENDERS", raising=False)
    else:
        monkeypatch.setenv("CERT_CALLBACK_SENDERS", senders)
    if secret is None:
        monkeypatch.delenv("CERT_CALLBACK_SECRET", raising=False)
    else:
        monkeypatch.setenv("CERT_CALLBACK_SECRET", secret)


def test_require_callback_auth_returns_configured_values(monkeypatch):
    _set_callback_auth(monkeypatch, senders="zapier@zapier.com",
                       secret="s3cr3t")
    senders, secret = require_callback_auth()
    assert senders == "zapier@zapier.com"
    assert secret == "s3cr3t"


def test_require_callback_auth_raises_without_senders(monkeypatch):
    _set_callback_auth(monkeypatch, senders=None, secret="s3cr3t")
    with pytest.raises(RuntimeError, match="CERT_CALLBACK_SENDERS"):
        require_callback_auth()


def test_require_callback_auth_raises_without_secret(monkeypatch):
    _set_callback_auth(monkeypatch, senders="zapier@zapier.com",
                       secret=None)
    with pytest.raises(RuntimeError, match="CERT_CALLBACK_SECRET"):
        require_callback_auth()


def test_require_callback_auth_raises_without_both(monkeypatch):
    _set_callback_auth(monkeypatch, senders=None, secret=None)
    with pytest.raises(RuntimeError,
                       match="CERT_CALLBACK_SENDERS.*CERT_CALLBACK_SECRET"):
        require_callback_auth()


def test_require_callback_auth_raises_on_blank_senders(monkeypatch):
    _set_callback_auth(monkeypatch, senders="   ", secret="s3cr3t")
    with pytest.raises(RuntimeError, match="CERT_CALLBACK_SENDERS"):
        require_callback_auth()


def test_require_callback_auth_raises_on_blank_secret(monkeypatch):
    _set_callback_auth(monkeypatch, senders="zapier@zapier.com",
                       secret="  ")
    with pytest.raises(RuntimeError, match="CERT_CALLBACK_SECRET"):
        require_callback_auth()
