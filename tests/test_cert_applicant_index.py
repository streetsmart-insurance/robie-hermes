"""Tests for cert_applicant_index: normalization, matching, holds."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robie_job_engine.cert_applicant_index import (  # noqa: E402
    AMBIGUOUS,
    MATCHED,
    NO_MATCH,
    ApplicantIndex,
    build_index,
    match_applicant,
    normalize_account_name,
    normalize_email,
    phone_keys,
)
from robie_job_engine.cert_intake import RequestFacts  # noqa: E402


def make_index():
    rows = [
        {"account_name": "   Fonseca General Contractor LLC",
         "applicant_id": 116349171, "email_primary": "office@fonsecagc.com",
         "phones": ["7327754443"]},
        {"account_name": "MAR Engineering, P.C.", "applicant_id": 211158333,
         "email_primary": "info@mareng.com", "phones": []},
        {"account_name": "Dup Name LLC", "applicant_id": 111,
         "email_primary": "", "phones": []},
        {"account_name": "Dup Name LLC", "applicant_id": 222,
         "email_primary": "", "phones": []},
    ]
    return build_index(rows, source_path="/tmp/fake.xlsx")


def test_normalize_strips_whitespace_and_punctuation():
    assert normalize_account_name("   Fonseca General Contractor LLC") == \
        normalize_account_name("fonseca general contractor llc")
    assert normalize_account_name("MAR Engineering, P.C.") == \
        normalize_account_name("MAR Engineering PC")


def test_email_match_when_sender_is_client():
    index = make_index()
    facts = RequestFacts(requester_email="office@fonsecagc.com")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 116349171
    assert "sender email" in res.evidence


def test_insured_name_beats_sender_email():
    # Third-party sender (also a client) naming a different insured:
    # the certificate is FOR the insured, so the insured wins.
    index = make_index()
    facts = RequestFacts(insured_name="Fonseca General Contractor LLC",
                         requester_email="info@mareng.com")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 116349171
    assert res.requester_also_client is True


def test_dba_match():
    index = make_index()
    facts = RequestFacts(dba="Fonseca General Contractor LLC")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 116349171


def test_ambiguous_name_holds():
    index = make_index()
    facts = RequestFacts(insured_name="Dup Name LLC")
    res = match_applicant(facts, index)
    assert res.status == AMBIGUOUS
    assert res.applicant_id is None
    assert set(res.candidates) == {111, 222}
    assert "never guessing" in res.hold_reason()


def test_no_match_holds_for_ezlynx_lookup():
    index = make_index()
    facts = RequestFacts(insured_name="Brand New Client Inc")
    res = match_applicant(facts, index)
    assert res.status == NO_MATCH
    assert "EZLynx lookup" in res.hold_reason()


def test_holder_name_never_matches():
    # A holder that looks like one of our clients must NOT route the filing.
    index = make_index()
    facts = RequestFacts(holder_names=["Fonseca General Contractor LLC"])
    res = match_applicant(facts, index)
    assert res.status == NO_MATCH


def test_phone_match():
    index = make_index()
    facts = RequestFacts()
    res = match_applicant(facts, index, phones=["(732) 775-4443"])
    assert res.status == MATCHED
    assert res.applicant_id == 116349171


def test_phone_keys_last_ten():
    assert "7327754443" in phone_keys("1-732-775-4443")


def test_index_stats_visible():
    index = make_index()
    stats = index.stats()
    assert stats["rows"] == 4
    assert stats["ambiguous_names"] == 1


def test_bad_rows_skipped_not_fatal():
    index = build_index([
        {"account_name": "Good Co", "applicant_id": 5,
         "email_primary": "", "phones": []},
        {"account_name": "Bad Co", "applicant_id": "nonsense",
         "email_primary": "", "phones": []},
        {"account_name": "", "applicant_id": 0,
         "email_primary": "", "phones": []},
    ])
    assert index.row_count == 1
    assert normalize_email("  Office@Example.COM ") == "office@example.com"


def test_all_applicant_ids_covers_every_row():
    """all_applicant_ids returns every ID in the index (the sweep's allowlist).

    Rows missing a name, email, or phone must still contribute their ID —
    a client is a client even when the directory row is sparse.
    """
    index = build_index([
        {"account_name": "Named Co", "applicant_id": 11,
         "email_primary": "", "phones": []},
        {"account_name": "", "applicant_id": 22,
         "email_primary": "noname@example.com", "phones": []},
        {"account_name": "", "applicant_id": 33,
         "email_primary": "", "phones": ["+1 (555) 000-0033"]},
        {"account_name": "Bad Row", "applicant_id": "nonsense",
         "email_primary": "", "phones": []},
    ])
    assert index.all_applicant_ids() == [11, 22, 33]
