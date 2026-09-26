"""Discussion-destination verification for certificate filing.

Carlo's question: "how do we know it's going into the right discussion?"
These tests pin the confidence ladder in resolve_discussion:

  1. task registry hit -> deterministic reuse (STRONG);
  2. exactly one cert-titled discussion naming the holder / policy -> STRONG;
  3. exactly one cert-titled discussion matching a holder fragment -> MEDIUM;
  4. multiple candidates -> HOLD, never guess (with dates in the reason);
  5. post-write: the note writer's reported discussion must agree with the
     resolver's choice, or the filing is an ERROR, never silently FILED.
"""
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "/tmp/cert_disc_work")

from robie_job_engine.cert_filing import (  # noqa: E402
    ERROR,
    FILED,
    HELD,
    FilingDeps,
    FilingResult,
    _file_claimed,
    _holder_named_in_title,
    _looks_like_followup,
    resolve_discussion,
)
from robie_job_engine.cert_intake import (  # noqa: E402
    CertEmail,
    extract_request_facts,
)


class FakeRegistry:
    def __init__(self, entry=None):
        self._entry = entry

    def get(self, *a):
        return self._entry

    def put(self, entry):
        self._entry = entry


class FakeZapier:
    def get_task_state(self, task_id):
        return "unknown"

    def create_task(self, **kw):
        return SimpleNamespace(reason="fake task created")


class FakeDiscussions:
    def __init__(self, rows):
        self._rows = rows

    def get_discussions(self, app_id):
        return self._rows


def _d(did, title, created):
    return {"id": did, "title": title, "noteCount": 2,
            "created": created, "lastModified": created}


def _verified(app_id=111, policies=None):
    return SimpleNamespace(applicant_id=app_id,
                           policy_numbers=policies or [],
                           status="VERIFIED")


# ---------------------------------------------------------------------------
# holder naming
# ---------------------------------------------------------------------------

def test_holder_named_in_title_word_order_and_suffix_tolerant():
    assert _holder_named_in_title(
        "Anderson Market & Metrovation Anderson, LLC",
        "Certificate of Insurance Request - ANDERSON MARKET LLC & "
        "METROVATION ANDERSON, LLC")


def test_holder_named_in_title_rejects_other_holder():
    assert not _holder_named_in_title(
        "Anderson Market", "Certificate of Insurance Request - Manasquan PBA")


def test_holder_named_in_title_single_distinctive_word():
    assert _holder_named_in_title(
        "Metrovation", "Certificate of Insurance Request - Metrovation LLC")
    # ... but a short generic word alone is not enough
    assert not _holder_named_in_title(
        "ABC", "Certificate of Insurance Request - ABC Supply")


def test_subject_holder_extraction():
    em = CertEmail(gmail_id="g", thread_id="t", rfc_message_id="r",
                   from_header="Michael Solomon <m@example.com>",
                   subject="Re: Certificate of Insurance LA Burger LLC to "
                           "Anderson Market & Metrovation Anderson, LLC",
                   date="Fri, 25 Sep 2026 21:06:08 +0000",
                   body_text="Please issue.", attachments=[])
    facts = extract_request_facts(em)
    assert facts.holder_names == ["Anderson Market & Metrovation Anderson, LLC"]


def test_subject_holder_extraction_rejects_plain_to():
    em = CertEmail(gmail_id="g", thread_id="t", rfc_message_id="r",
                   from_header="a@b.com",
                   subject="Certificate of Insurance Renewal",
                   date="Sat, 26 Sep 2026 12:00:00 +0000",
                   body_text="Please send the certificate to our office.",
                   attachments=[])
    facts = extract_request_facts(em)
    assert facts.holder_names == []


def test_subject_holder_extraction_accepts_clean_pattern():
    em = CertEmail(gmail_id="g", thread_id="t", rfc_message_id="r",
                   from_header="a@b.com",
                   subject="Certificate of Insurance Acme Builders to "
                           "Downtown Bank",
                   date="Sat, 26 Sep 2026 12:00:00 +0000",
                   body_text="x", attachments=[])
    facts = extract_request_facts(em)
    assert facts.holder_names == ["Downtown Bank"]


def test_subject_holder_extraction_rejects_mismatched_insured():
    # "to Downtown Bank" but the insured-part ("Renewal") is not the insured
    # -> "to" is just a preposition here; no holder is invented.
    em = CertEmail(gmail_id="g", thread_id="t", rfc_message_id="r",
                   from_header="a@b.com",
                   subject="Certificate of Insurance Renewal to Downtown Bank",
                   date="Sat, 26 Sep 2026 12:00:00 +0000",
                   body_text="Certificate for Acme Builders", attachments=[])
    facts = extract_request_facts(em)
    assert "Downtown Bank" not in facts.holder_names


def test_looks_like_followup():
    assert _looks_like_followup("Re: Certificate Request")
    assert _looks_like_followup("fwd: something")
    assert not _looks_like_followup("Renewal Certificate Request- Abg")


# ---------------------------------------------------------------------------
# resolution ladder
# ---------------------------------------------------------------------------

def test_registry_hit_is_deterministic():
    entry = SimpleNamespace(discussion_id="999")
    discs = [_d("999", "Certificate of Insurance Request - RMIS",
                "2026-09-15T17:19:14+00:00")]
    did, title, how = resolve_discussion(
        _verified(), ["RMIS"], FakeRegistry(entry), FakeDiscussions(discs))
    assert did == "999"
    assert "registry" in how


def test_single_holder_discussion_resolves_strong():
    discs = [
        _d("1", "Certificate of Insurance Request - ANDERSON MARKET LLC",
           "2026-09-25T19:30:38+00:00"),
        _d("2", "Certificate of Insurance Request - Manasquan PBA",
           "2026-05-19T19:44:32+00:00"),
    ]
    did, title, how = resolve_discussion(
        _verified(), ["Anderson Market"], FakeRegistry(),
        FakeDiscussions(discs),
        email_date="Fri, 25 Sep 2026 21:06:08 +0000")
    assert did == "1"
    assert how.startswith("STRONG")
    assert "created for it" in how


def test_policy_digits_narrow_multiple_candidates():
    discs = [
        _d("1", "Certificate of Insurance Request - RMIS",
           "2026-09-15T17:19:14+00:00"),
        _d("2", "Certificate of Insurance Request - RMIS policy 998877",
           "2026-09-02T15:50:34+00:00"),
    ]
    did, title, how = resolve_discussion(
        _verified(policies=["998877"]), ["RMIS"], FakeRegistry(),
        FakeDiscussions(discs),
        email_date="Sat, 26 Sep 2026 12:27:49 +0000")
    assert did == "2"
    assert how.startswith("STRONG")
    assert "policy digits" in how


def test_stale_same_holder_threads_hold_with_dates():
    discs = [
        _d("1", "Certificate of Insurance Request - RMIS",
           "2026-09-15T17:19:14+00:00"),
        _d("2", "Certificate of Insurance Request - RMIS",
           "2026-09-02T15:50:34+00:00"),
        _d("3", "Certificate of Insurance Request - RMIS",
           "2025-10-27T15:04:53+00:00"),
    ]
    did, title, how = resolve_discussion(
        _verified(), ["RMIS"], FakeRegistry(), FakeDiscussions(discs),
        email_date="Sat, 26 Sep 2026 12:27:49 +0000", is_followup=False)
    assert did is None
    assert "refusing to guess" in how
    assert "10 days old" in how  # newest thread is stale


def test_fresh_discussion_for_new_request_resolves_medium():
    discs = [
        _d("1", "Certificate of Insurance Request - RMIS",
           "2026-09-24T10:00:00+00:00"),  # 2 days before the email
        _d("2", "Certificate of Insurance Request - RMIS",
           "2026-09-02T15:50:34+00:00"),
    ]
    did, title, how = resolve_discussion(
        _verified(), ["RMIS"], FakeRegistry(), FakeDiscussions(discs),
        email_date="Sat, 26 Sep 2026 12:27:49 +0000", is_followup=False)
    assert did == "1"
    assert how.startswith("MEDIUM")


def test_fresh_discussion_does_not_resolve_a_followup():
    discs = [
        _d("1", "Certificate of Insurance Request - RMIS",
           "2026-09-24T10:00:00+00:00"),
        _d("2", "Certificate of Insurance Request - RMIS",
           "2026-09-02T15:50:34+00:00"),
    ]
    did, title, how = resolve_discussion(
        _verified(), ["RMIS"], FakeRegistry(), FakeDiscussions(discs),
        email_date="Sat, 26 Sep 2026 12:27:49 +0000", is_followup=True)
    assert did is None  # a reply may belong to the older thread


def test_no_candidates_holds():
    discs = [_d("1", "General Inquiry - billing question",
                "2026-09-01T10:00:00+00:00")]
    did, title, how = resolve_discussion(
        _verified(), ["RMIS"], FakeRegistry(), FakeDiscussions(discs))
    assert did is None
    assert "no certificates discussion" in how


def test_no_holder_extracted_holds_when_many():
    discs = [
        _d("1", "Certificate of Insurance Request - RMIS",
           "2026-09-15T17:19:14+00:00"),
        _d("2", "Certificate of Insurance Request - RXO",
           "2026-05-14T13:57:23+00:00"),
    ]
    did, title, how = resolve_discussion(
        _verified(), [], FakeRegistry(), FakeDiscussions(discs),
        email_date="Sat, 26 Sep 2026 12:27:49 +0000")
    assert did is None
    assert "no holder extracted" in how


def test_recency_alone_never_resolves():
    # Newest discussion is fresh but names a DIFFERENT holder -> hold.
    discs = [
        _d("1", "Certificate of Insurance Request - RXO",
           "2026-09-25T10:00:00+00:00"),
        _d("2", "Certificate of Insurance Request - RMIS",
           "2026-09-02T15:50:34+00:00"),
    ]
    did, title, how = resolve_discussion(
        _verified(), ["RMIS"], FakeRegistry(), FakeDiscussions(discs),
        email_date="Sat, 26 Sep 2026 12:27:49 +0000", is_followup=False)
    assert did == "2"  # the single RMIS-anchored one, not the fresh RXO one


# ---------------------------------------------------------------------------
# post-write destination agreement
# ---------------------------------------------------------------------------

class _Store:
    def claim(self, *a):
        return True

    def release(self, *a):
        pass

    def get(self, *a):
        return {}

    def set(self, *a, **k):
        pass


def _deps(note_result, discussions):
    def note_writer(applicant_id, note_text, **kw):
        return note_result

    return FilingDeps(
        discussions_client=FakeDiscussions(discussions),
        verifier=None, note_writer=note_writer,
        doc_writer=lambda *a, **k: {"document_id": "d1"},
        zapier=FakeZapier(), registry=FakeRegistry(), store=_Store())


def _record_and_verified(did_title):
    did, title = did_title
    record = SimpleNamespace(
        gmail_id="g1", subject="Certificate of Insurance Request",
        date="Sat, 26 Sep 2026 12:27:49 +0000",
        facts=SimpleNamespace(holder_names=["RMIS"]),
        attachments=[], email=SimpleNamespace())
    verified = SimpleNamespace(status="VERIFIED", applicant_id=111,
                               insured_name="Abg Transportation",
                               policy_numbers=[],
                               holder_names=["RMIS"],
                               requested_action="new_request",
                               evidence=["x"])
    return record, verified


def _patch_guard(monkeypatch):
    import robie_job_engine.cert_filing as cf
    monkeypatch.setattr(cf, "verify_filing_target", lambda *a: True)
    monkeypatch.setattr(cf, "summarize_for_note", lambda *a, **k: "note")


def test_writer_disagreement_is_error(monkeypatch):
    _patch_guard(monkeypatch)
    discs = [_d("1", "Certificate of Insurance Request - RMIS",
                "2026-09-26T10:00:00+00:00")]
    deps = _deps({"status": "filed", "note_id": "n1", "discussion_id": "2",
                  "read_back": True}, discs)
    record, verified = _record_and_verified(("1", "x"))
    res = FilingResult()
    out = _file_claimed(record, verified, deps, res, "g1", 111,
                        "owner", False)
    assert out.status == ERROR
    assert any("destination mismatch" in h for h in out.hold_reasons)


def test_writer_agreement_files_with_readback_evidence(monkeypatch):
    _patch_guard(monkeypatch)
    discs = [_d("1", "Certificate of Insurance Request - RMIS",
                "2026-09-26T10:00:00+00:00")]
    deps = _deps({"status": "filed", "note_id": "n1", "discussion_id": "1",
                  "read_back": True}, discs)
    record, verified = _record_and_verified(("1", "x"))
    res = FilingResult()
    out = _file_claimed(record, verified, deps, res, "g1", 111,
                        "owner", False)
    assert out.status == FILED
    assert any("read-back confirmed" in e for e in out.evidence)


def test_writer_without_readback_is_flagged_not_failed(monkeypatch):
    _patch_guard(monkeypatch)
    discs = [_d("1", "Certificate of Insurance Request - RMIS",
                "2026-09-26T10:00:00+00:00")]
    deps = _deps({"status": "filed", "note_id": "n1", "discussion_id": "1"},
                 discs)
    record, verified = _record_and_verified(("1", "x"))
    res = FilingResult()
    out = _file_claimed(record, verified, deps, res, "g1", 111,
                        "owner", False)
    assert out.status == FILED
    assert any("did not report a read-back" in e for e in out.evidence)
