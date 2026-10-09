"""Tests for the hello intake match() step (hello_match.py).

Covers: PDF fixture text extraction, doc-identity extraction, policy
normalization ladder, fuzzy name scoring (including a pair that must NOT
match), portal-link deferral, the confidence ladder, doc-outranks-email,
the vendor-sender guard, and queue wiring with doc evidence.
"""

import os
import tempfile

import pytest

from robie_job_engine.hello_match import (
    best_name_matches,
    extract_doc_identity,
    extract_pdf_text,
    fuzzy_name_score,
    handle_match_result,
    match_hello,
    normalize_policy_number,
    policy_digit_core,
    policy_numbers_match,
    retrieve_attachment_texts,
    retrieve_link_docs,
)


# ---------------------------------------------------------------------------
# Fixture: a minimal one-page PDF built by hand (pypdf-readable)
# ---------------------------------------------------------------------------

def _make_pdf_bytes(lines: list[str]) -> bytes:
    """Build a minimal PDF with the given text lines. Keeps the test free
    of any PDF-writing dependency."""
    content_lines = []
    y = 720
    for line in lines:
        escaped = (line.replace("\\", "\\\\").replace("(", "\\(")
                   .replace(")", "\\)"))
        content_lines.append(
            f"BT /F1 12 Tf 72 {y} Td ({escaped}) Tj ET")
        y -= 20
    stream = "\n".join(content_lines).encode("latin-1")
    objs = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objs.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
    objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = [b"%PDF-1.4"]
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(sum(len(p) + 1 for p in out))
        out.append(f"{i} 0 obj".encode())
        out.append(body)
        out.append(b"endobj")
    xref_pos = sum(len(p) + 1 for p in out)
    out.append(f"xref\n0 {len(objs) + 1}".encode())
    out.append(b"0000000000 65535 f ")
    for off in offsets:
        out.append(f"{off:010d} 00000 n ".encode())
    out.append(b"trailer")
    out.append(f"<< /Size {len(objs) + 1} /Root 1 0 R >>".encode())
    out.append(b"startxref")
    out.append(str(xref_pos).encode())
    out.append(b"%%EOF")
    return b"\n".join(out)


@pytest.fixture()
def dec_page_pdf() -> bytes:
    return _make_pdf_bytes([
        "DECLARATIONS PAGE",
        "POLICY NUMBER: BP00109727",
        "Named Insured: SECI Construction Inc",
        "Policy Period: 01/01/2026 to 01/01/2027",
    ])


# ---------------------------------------------------------------------------
# Document retrieval + doc identity
# ---------------------------------------------------------------------------

def test_pdf_fixture_extracts_text(dec_page_pdf):
    text = extract_pdf_text(dec_page_pdf)
    assert text is not None
    assert "BP00109727" in text
    assert "SECI Construction Inc" in text


def test_retrieve_attachment_texts_pdf(dec_page_pdf):
    docs = retrieve_attachment_texts([("dec_page.pdf", dec_page_pdf)])
    assert len(docs) == 1
    assert docs[0]["text"] is not None
    assert "BP00109727" in docs[0]["text"]
    assert docs[0]["note"] is None


def test_retrieve_attachment_texts_unsupported_type():
    docs = retrieve_attachment_texts([("photo.png", b"\x89PNG\r\n")])
    assert docs[0]["text"] is None
    assert "unsupported" in docs[0]["note"]


def test_extract_doc_identity_from_text():
    ident = extract_doc_identity(
        "DECLARATIONS\nPOLICY NUMBER: BP00109727\n"
        "Named Insured: SECI Construction Inc\n")
    assert "BP00109727" in ident["policy_numbers"]
    assert "SECI Construction Inc" in ident["insured_names"]


def test_extract_doc_identity_empty():
    ident = extract_doc_identity("no usable anchors here at all")
    assert ident == {"policy_numbers": [], "insured_names": []}


# ---------------------------------------------------------------------------
# Policy-number reconciliation ladder
# ---------------------------------------------------------------------------

def test_normalize_policy_number():
    assert normalize_policy_number("008 985 366") == "008985366"
    assert normalize_policy_number("BP-00109727") == "BP00109727"
    assert normalize_policy_number("812601-884344-75") == "81260188434475"


def test_policy_numbers_match_exact_and_formatting():
    assert policy_numbers_match("BP00109727", "BP00109727")
    assert policy_numbers_match("008 985 366", "008985366")


def test_policy_numbers_match_digit_core_with_carrier_letter():
    # Carrier suffix letter: same policy, different rendering.
    assert policy_numbers_match("008985366C", "008 985 366")


def test_policy_numbers_do_not_match():
    assert not policy_numbers_match("BP00109727", "BP00109728")
    assert not policy_numbers_match("008985366", "008985367")
    assert not policy_numbers_match(None, "BP00109727")
    assert not policy_numbers_match("", "")


def test_policy_digit_core():
    assert policy_digit_core("008985366C") == "008985366"
    assert policy_digit_core("1-HNY-NJ-01-014332") == "101014332"


# ---------------------------------------------------------------------------
# Fuzzy name scoring
# ---------------------------------------------------------------------------

def test_fuzzy_name_abbreviation_scores_full():
    # Carlo's case: "Seci" vs "SECI Construction Inc" must be recognized.
    assert fuzzy_name_score("Seci", "SECI Construction Inc") == 1.0


def test_fuzzy_name_suffix_and_the_ignored():
    assert fuzzy_name_score("The Bernal Group", "Bernal Group NJ") == 1.0
    assert fuzzy_name_score("ABC Trucking LLC", "ABC Trucking") == 1.0


def test_fuzzy_name_one_token_different_must_not_match():
    # A different token means a different company — must stay below the
    # strong threshold so it can never auto-file.
    score = fuzzy_name_score("ABC Trucking LLC", "ABD Trucking LLC")
    assert score < 0.95


def test_fuzzy_name_unrelated_is_low():
    assert fuzzy_name_score("Seci Construction", "Bernal Group NJ") < 0.85


def test_best_name_matches_orders_and_filters():
    roster = [
        {"applicant_id": 1, "account_name": "SECI Construction Inc"},
        {"applicant_id": 2, "account_name": "Bernal Group NJ"},
    ]
    hits = best_name_matches("Seci", roster)
    assert len(hits) == 1
    assert hits[0][1]["applicant_id"] == 1
    assert hits[0][0] == 1.0


# ---------------------------------------------------------------------------
# Portal-link deferral
# ---------------------------------------------------------------------------

def test_portal_link_deferred_on_html():
    def fake_fetch(url):
        return b"<html><body>Sign in to view documents</body></html>", \
            "text/html"

    docs, portal_links = retrieve_link_docs(
        "View your documents: https://carrier.example.com/docs/123",
        url_fetch=fake_fetch)
    assert docs == []
    assert portal_links == ["https://carrier.example.com/docs/123"]


def test_portal_link_deferred_on_403():
    class Forbidden(Exception):
        code = 403

    def fake_fetch(url):
        raise Forbidden("login required")

    docs, portal_links = retrieve_link_docs(
        "https://carrier.example.com/portal/doc", url_fetch=fake_fetch)
    assert portal_links == ["https://carrier.example.com/portal/doc"]


def test_direct_pdf_link_is_fetched(dec_page_pdf):
    def fake_fetch(url):
        return dec_page_pdf, "application/pdf"

    docs, portal_links = retrieve_link_docs(
        "Your dec page: https://carrier.example.com/dec.pdf",
        url_fetch=fake_fetch)
    assert portal_links == []
    assert len(docs) == 1
    assert "BP00109727" in docs[0]["text"]


def test_unsubscribe_links_are_never_fetched():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"", "text/html"

    retrieve_link_docs(
        "Unsubscribe: https://example.com/unsubscribe?id=1 "
        "Track: https://tracking.example.com/pixel",
        url_fetch=fake_fetch)
    assert calls == []


# ---------------------------------------------------------------------------
# The match() ladder
# ---------------------------------------------------------------------------

_ROSTER = [
    {"applicant_id": 78540038, "account_name": "SECI Construction Inc",
     "emails": ["sami@seciinc.com"], "policy_numbers": ["BP00109727"]},
    {"applicant_id": 147197937, "account_name": "ABG Transportation LLC",
     "emails": ["dispatch@abgtrans.com"], "policy_numbers": ["008985366"]},
]


def _identity(**over):
    base = {"sender_email": "mela@seciinc.com", "sender_name": "Mela",
            "company_name": None, "policy_numbers": [], "mc_numbers": []}
    base.update(over)
    return base


def test_match_exact_policy_is_high():
    result = match_hello(
        identity=_identity(policy_numbers=["BP00109727"]),
        roster=_ROSTER)
    assert result["status"] == "matched"
    assert result["confidence"] == "high"
    assert result["applicant_id"] == 78540038
    assert result["matched_on"] == "policy_exact"


def test_match_normalized_policy_is_high():
    result = match_hello(
        identity=_identity(policy_numbers=["008985366C"]),
        roster=_ROSTER)
    assert result["status"] == "matched"
    assert result["confidence"] == "high"
    assert result["applicant_id"] == 147197937
    assert result["matched_on"] == "policy_normalized"


def test_match_report_email_is_high():
    result = match_hello(
        identity=_identity(sender_email="sami@seciinc.com"),
        roster=_ROSTER)
    assert result["status"] == "matched"
    assert result["matched_on"] == "report_email"


def test_match_sender_alias_is_high():
    store = {"aliases": [{"sender": "mela@seciinc.com",
                          "applicant_id": 78540038,
                          "account_name": "SECI Construction Inc",
                          "confidence": "strong"}]}
    result = match_hello(identity=_identity(), roster=_ROSTER,
                         alias_store=store)
    assert result["status"] == "matched"
    assert result["matched_on"] == "sender_alias"
    assert result["confidence"] == "high"


def test_match_name_only_is_medium_and_queued():
    # Strong name resemblance but no policy/email anchor: never auto-files.
    result = match_hello(
        identity=_identity(company_name="Seci"),
        roster=_ROSTER)
    assert result["status"] == "queue"
    assert result["confidence"] == "medium"
    assert "name_fuzzy" in result["strategies_tried"]


def test_match_ambiguous_name_is_low():
    roster = [
        {"applicant_id": 1, "account_name": "ABC Trucking"},
        {"applicant_id": 2, "account_name": "ABC Trucking Services"},
    ]
    result = match_hello(
        identity=_identity(company_name="ABC Trucking"),
        roster=roster)
    assert result["status"] == "queue"
    assert result["confidence"] == "low"
    assert "ambiguous" in result["hold_reason"]


def test_match_weak_name_is_low_with_strategies():
    result = match_hello(
        identity=_identity(company_name="Totally Different Company"),
        roster=_ROSTER)
    assert result["status"] == "queue"
    assert result["confidence"] == "low"
    assert result["strategies_tried"]  # the human sees what was tried


def test_doc_identity_outranks_email_claims(dec_page_pdf):
    docs = retrieve_attachment_texts([("dec.pdf", dec_page_pdf)])
    doc_idents = [extract_doc_identity(d["text"]) for d in docs
                  if d["text"]]
    # Email claims a policy that is not in the system; the doc has the truth.
    result = match_hello(
        identity=_identity(policy_numbers=["WRONG99999"],
                           company_name="Wrong Co"),
        roster=_ROSTER, doc_identities=doc_idents)
    assert result["status"] == "matched"
    assert result["applicant_id"] == 78540038
    assert result["doc_conflicts"]  # the disagreement is on the record


def test_vendor_sender_never_uses_alias():
    store = {"aliases": [{"sender": "notices@phly.com",
                          "applicant_id": 999,  # must never be honored
                          "account_name": "Should Not Match",
                          "confidence": "strong"}]}
    result = match_hello(
        identity=_identity(sender_email="notices@phly.com",
                           policy_numbers=["BP00109727"]),
        roster=_ROSTER, alias_store=store)
    # Alias ignored for the carrier sender; the real policy still matches.
    assert result["status"] == "matched"
    assert result["applicant_id"] == 78540038
    assert result["matched_on"] == "policy_exact"
    assert "hello_alias_skipped_vendor" in result["strategies_tried"]


def test_policy_anchor_via_policy_search():
    class FakePolicySearch:
        def search_policy_by_number(self, number):
            if "BP00109727" in number:
                return {"data": [{"applicantId": "78540038",
                                  "accountName": "SECI Construction Inc"}]}
            return {"data": []}

    result = match_hello(
        identity=_identity(policy_numbers=["BP00109727"]),
        roster=[], policy_search=FakePolicySearch())
    assert result["status"] == "matched"
    assert result["matched_on"] == "policy_anchor"
    assert result["applicant_id"] == 78540038


def test_policy_anchor_disagreement_refuses_to_guess():
    class FakePolicySearch:
        def search_policy_by_number(self, number):
            return {"data": [{"applicantId": "1"}, {"applicantId": "2"}]}

    result = match_hello(
        identity=_identity(policy_numbers=["BP00109727"]),
        roster=[], policy_search=FakePolicySearch())
    assert result["status"] == "queue"
    assert result["confidence"] == "low"


# ---------------------------------------------------------------------------
# Queue wiring
# ---------------------------------------------------------------------------

def test_handle_match_result_queues_with_doc_evidence(tmp_path):
    queue_path = str(tmp_path / "q.jsonl")
    result = match_hello(
        identity=_identity(company_name="Seci"),
        roster=_ROSTER,
        portal_links=["https://carrier.example.com/portal/doc"])
    assert result["status"] == "queue"
    handled = handle_match_result(
        result, sender_email="mela@seciinc.com",
        subject="Dec page attached", request_type="carrier_notice",
        route="applicable_csr", gmail_id="g123",
        queue_path=queue_path)
    assert handled["action"] == "queued"
    entry = handled["entry"]
    assert entry["status"] == "open"
    assert any("portal_link_needs_human" in e for e in entry["evidence"])
    assert "name_fuzzy" in entry["strategies_tried"]
    assert "portal_link_deferred" in entry["strategies_tried"]
    # No duplicate on re-run for the same gmail id.
    again = handle_match_result(
        result, sender_email="mela@seciinc.com",
        subject="Dec page attached", request_type="carrier_notice",
        route="applicable_csr", gmail_id="g123",
        queue_path=queue_path)
    assert again["entry"]["entry_id"] == entry["entry_id"]


def test_handle_match_result_matched_returns_filing_info(tmp_path):
    result = match_hello(
        identity=_identity(policy_numbers=["BP00109727"]),
        roster=_ROSTER)
    handled = handle_match_result(
        result, sender_email="mela@seciinc.com", subject="Renewal",
        queue_path=str(tmp_path / "q.jsonl"))
    assert handled["action"] == "file"
    assert handled["applicant_id"] == 78540038
    assert handled["confidence"] == "high"
    assert not os.path.exists(str(tmp_path / "q.jsonl"))  # nothing queued
