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
    normalize_policy_number,
    phone_keys,
)
from robie_job_engine.cert_intake import (  # noqa: E402
    CertEmail,
    RequestFacts,
    extract_request_facts,
)


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


# ---------------------------------------------------------------------------
# Policy-number matching (regression: the 2026-09-29 EPHE LLC miss —
# "COI for EPHE LLC" named a client whose policy number was in the book,
# but policy numbers were never a match key).
# ---------------------------------------------------------------------------

HIGHWAY_SUBJECT = "Renewal COI Request: COI for EPHE LLC Expires Tomorrow"
HIGHWAY_BODY = (
    "Highway COI Request for EPHE LLC\n\nHi there,\n\n"
    "The certificate of insurance (COI) we have on file for *EPHE LLC* "
    "has a policy expiring tomorrow. Please provide a new COI for the "
    "upcoming policy period so we can update our records.\n\n"
    "Policy #: 9300216995\n"
)


def make_policy_index():
    rows = [
        {"account_name": "EPHE LLC", "applicant_id": 199205654,
         "email_primary": "sinancanlv@yahoo.com", "phones": ["7025012909"],
         "policy_numbers": "9300216995"},
        {"account_name": "Other Corp", "applicant_id": 111,
         "email_primary": "", "phones": [],
         "policy_numbers": "ADMP000656-02; B01053720"},
        # Two rows sharing one policy number: must hold, never guess.
        {"account_name": "Shared Pol A", "applicant_id": 301,
         "email_primary": "", "phones": [], "policy_numbers": "SHARED-1"},
        {"account_name": "Shared Pol B", "applicant_id": 302,
         "email_primary": "", "phones": [], "policy_numbers": "SHARED-1"},
    ]
    return build_index(rows, source_path="/tmp/fake-policies.csv")


def _highway_email():
    return CertEmail(
        gmail_id="g-highway", thread_id="t1",
        rfc_message_id="<highway@example.com>",
        from_header="Highway <no-reply@highway.com>",
        subject=HIGHWAY_SUBJECT, date="Mon, 28 Sep 2026 22:00:00 -0400",
        body_text=HIGHWAY_BODY,
    )


def test_normalize_policy_number_folds_case_and_whitespace():
    assert normalize_policy_number(" 9300216995 ") == "9300216995"
    assert normalize_policy_number("admp000656-02") == "ADMP000656-02"
    assert normalize_policy_number(None) == ""


def test_policy_index_built_from_semicolon_list():
    index = make_policy_index()
    assert index.stats()["unique_policies"] == 4
    assert index.by_policy["9300216995"] == [199205654]
    assert index.by_policy["B01053720"] == [111]


def test_highway_email_matches_ephe_end_to_end():
    # (a) The real miss, replayed: Highway-shaped email extracts
    # insured "EPHE LLC" + policy 9300216995 and matches the book row.
    index = make_policy_index()
    facts = extract_request_facts(_highway_email())
    assert facts.insured_name == "EPHE LLC"
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 199205654


def test_policy_number_match_without_name():
    # (b) No extractable name at all: the policy number alone routes.
    index = make_policy_index()
    facts = RequestFacts(policy_numbers=["9300216995"],
                         requester_email="no-reply@highway.com")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 199205654
    assert "policy number" in res.evidence


def test_name_match_beats_conflicting_policy():
    # A name hit must never be overridden by a policy pointing elsewhere.
    index = make_policy_index()
    facts = RequestFacts(insured_name="Other Corp",
                         policy_numbers=["9300216995"])
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 111


def test_shared_policy_number_holds_ambiguous():
    index = make_policy_index()
    facts = RequestFacts(policy_numbers=["SHARED-1"])
    res = match_applicant(facts, index)
    assert res.status == AMBIGUOUS
    assert set(res.candidates) == {301, 302}


def test_unknown_coi_for_name_holds():
    # (c) Extracted name not in the book + unknown policy: hold, never
    # misroute to a different client.
    index = make_policy_index()
    facts = RequestFacts(insured_name="Nonexistent Company LLC",
                         policy_numbers=["ZZZ999"])
    res = match_applicant(facts, index)
    assert res.status == NO_MATCH
    assert "EZLynx lookup" in res.hold_reason()


def test_holder_with_unmatched_policy_still_holds():
    # (d) A holder that IS a client is never a match key — even when a
    # policy number is present but matches nothing.
    index = make_policy_index()
    facts = RequestFacts(holder_names=["EPHE LLC"],
                         policy_numbers=["ZZZ999"])
    res = match_applicant(facts, index)
    assert res.status == NO_MATCH


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
