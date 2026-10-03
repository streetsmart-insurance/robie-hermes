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
    fuzzy_lookup_ids,
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


# ---------------------------------------------------------------------------
# Commercial-preference rule (Carlo 2026-09-29): an ambiguous insured name
# errs toward the commercial applicant — exactly one candidate with a
# non-empty DBA resolves to it, after the contact-info tie-breaker.
# The "merge duplicates" idea is dead: personal-vs-commercial splits for
# the same human are legitimate separate records.
# ---------------------------------------------------------------------------

def make_commercial_index():
    rows = [
        # Martin Omondi-shaped: same normalized name, one commercial
        # (DBA) record, one bare personal record, no distinguishing
        # contact info in either the records or the request.
        {"account_name": "Martin Omondi", "applicant_id": 201,
         "dba": "GTZ TRANSPORTATION", "email_primary": "", "phones": []},
        {"account_name": "Martin Omondi", "applicant_id": 202,
         "dba": "", "email_primary": "", "phones": []},
        # Both candidates commercial -> still ambiguous.
        {"account_name": "Jordan Ellis", "applicant_id": 203,
         "dba": "ELLIS TRUCKING", "email_primary": "", "phones": []},
        {"account_name": "Jordan Ellis", "applicant_id": 204,
         "dba": "ELLIS LOGISTICS", "email_primary": "", "phones": []},
        # Neither commercial -> still ambiguous (covered by the older
        # collision tests; here for the preference-path contrast).
        {"account_name": "Casey Rivera", "applicant_id": 205,
         "dba": "", "email_primary": "", "phones": []},
        {"account_name": "Casey Rivera", "applicant_id": 206,
         "dba": "", "email_primary": "", "phones": []},
    ]
    return build_index(rows, source_path="/tmp/fake.xlsx")


def test_ambiguous_name_resolves_to_commercial_record():
    index = make_commercial_index()
    facts = RequestFacts(insured_name="Martin Omondi")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 201
    assert "commercial preference" in res.evidence
    assert "GTZ TRANSPORTATION" in res.evidence


def test_ambiguous_name_with_two_commercial_records_holds():
    index = make_commercial_index()
    facts = RequestFacts(insured_name="Jordan Ellis")
    res = match_applicant(facts, index)
    assert res.status == AMBIGUOUS
    assert set(res.candidates) == {203, 204}


def test_ambiguous_name_with_no_commercial_records_holds():
    index = make_commercial_index()
    facts = RequestFacts(insured_name="Casey Rivera")
    res = match_applicant(facts, index)
    assert res.status == AMBIGUOUS
    assert set(res.candidates) == {205, 206}


def test_contact_tie_break_beats_commercial_preference():
    # Hard evidence from the request itself always wins over the
    # commercial prior: the sender email points at the BARE record.
    rows = [
        {"account_name": "Martin Omondi", "applicant_id": 201,
         "dba": "GTZ TRANSPORTATION", "email_primary": "", "phones": []},
        {"account_name": "Martin Omondi", "applicant_id": 202,
         "dba": "", "email_primary": "martin@example.com", "phones": []},
    ]
    index = build_index(rows, source_path="/tmp/fake.xlsx")
    facts = RequestFacts(insured_name="Martin Omondi",
                         requester_email="martin@example.com")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 202
    assert "tie-broken" in res.evidence


def test_dba_part_parsed_from_account_name_infix():
    from robie_job_engine.cert_applicant_index import _dba_part
    assert _dba_part("Martin Omondi DBA GTZ Transportation") == \
        "GTZ Transportation"
    assert _dba_part("Martin Omondi") == ""
    assert _dba_part(None) == ""


def test_dba_infix_counts_as_commercial_indicator():
    # Rows without a separate dba column: the "DBA" infix in the
    # account name still marks the commercial record.
    rows = [
        {"account_name": "Martin Omondi DBA GTZ Transportation",
         "applicant_id": 301, "email_primary": "", "phones": []},
        {"account_name": "Martin Omondi", "applicant_id": 302,
         "email_primary": "", "phones": []},
    ]
    index = build_index(rows, source_path="/tmp/fake.xlsx")
    assert index.dba_by_id[301] == "GTZ Transportation"
    assert 302 not in index.dba_by_id


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
# Typo-tolerant matching (bounded fuzzy fallback)
# ---------------------------------------------------------------------------
# Exact normalized matching stays first. Fuzzy fires only when the exact
# lookups found nothing, and exactly one candidate wins — otherwise hold.

def make_typo_index():
    rows = [
        {"account_name": "EPHE LLC", "applicant_id": 199205654,
         "email_primary": "sinancanlv@yahoo.com", "phones": []},
        {"account_name": "Abc Plumbing LLC", "applicant_id": 301,
         "email_primary": "", "phones": []},
        {"account_name": "Abc Plumbing LLP", "applicant_id": 302,
         "email_primary": "", "phones": []},
        {"account_name": "Fonseca General Contractor LLC",
         "applicant_id": 116349171, "email_primary": "office@fonsecagc.com",
         "phones": ["7327754443"]},
    ]
    return build_index(rows, source_path="/tmp/fake.xlsx")


def test_fuzzy_typo_matches_single_candidate():
    # The live EPHE miss shape: "EPHE LCC" for "EPHE LLC".
    index = make_typo_index()
    facts = RequestFacts(insured_name="EPHE LCC")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 199205654
    assert "fuzzy" in res.evidence


def test_exact_match_still_wins_before_fuzzy():
    index = make_typo_index()
    facts = RequestFacts(insured_name="EPHE LLC")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 199205654
    assert "fuzzy" not in res.evidence


def test_fuzzy_typo_plausibly_matching_two_clients_holds():
    # "Abc Plumbing LLQ" is one typo from BOTH Abc Plumbing LLC and
    # Abc Plumbing LLP — held, never guessed.
    index = make_typo_index()
    facts = RequestFacts(insured_name="Abc Plumbing LLQ")
    res = match_applicant(facts, index)
    assert res.status == AMBIGUOUS
    assert res.applicant_id is None
    assert set(res.candidates) == {301, 302}
    assert "never guessing" in res.hold_reason()


def test_fuzzy_sender_email_pointing_elsewhere_holds():
    # Weak signal + contradictory sender email = hold (same rule as
    # DBA-fragment matching).
    index = make_typo_index()
    facts = RequestFacts(insured_name="EPHE LCC",
                         requester_email="office@fonsecagc.com")
    res = match_applicant(facts, index)
    assert res.status == AMBIGUOUS
    assert res.applicant_id is None


def test_fuzzy_needs_minimum_name_length():
    # Short names collide too fast ("ABC" is one typo from "ABD",
    # "ACC", ...) — no fuzzy below 4 normalized chars.
    index = make_typo_index()
    ids, _best = fuzzy_lookup_ids("ABC", index)
    assert ids == []


def test_near_miss_reported_but_never_matched():
    # "EPHE LLCXXX" is one edit OUTSIDE the bound: still NO_MATCH, but
    # the near-miss id is surfaced for the health check.
    index = make_typo_index()
    facts = RequestFacts(insured_name="EPHE LLCXXX")
    res = match_applicant(facts, index)
    assert res.status == NO_MATCH
    assert res.near_miss_ids == [199205654]


# ---------------------------------------------------------------------------
# Ambiguous-name tie-breaking
# ---------------------------------------------------------------------------
# Same normalized name, 2+ applicants (different contact info). The
# request's own sender email / phone / policy number may corroborate
# exactly one candidate. Zero or 2+ corroborated candidates stay
# AMBIGUOUS — never guessed.

def make_tie_index():
    rows = [
        {"account_name": "Robert Lake", "applicant_id": 101,
         "email_primary": "rob@lakeone.com", "phones": ["2125550101"]},
        {"account_name": "Robert Lake", "applicant_id": 102,
         "email_primary": "rob@laketwo.com", "phones": ["2125550102"]},
    ]
    index = build_index(rows, source_path="/tmp/fake.xlsx")
    # by_policy is built by the EPHE follow-up's policy-number matching;
    # injected here to prove the tie-break composes with it.
    index.by_policy = {"9300216995": 102}
    return index


def test_tie_broken_by_sender_email():
    index = make_tie_index()
    facts = RequestFacts(insured_name="Robert Lake",
                         requester_email="rob@laketwo.com")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 102
    assert "tie-broken" in res.evidence


def test_tie_broken_by_phone():
    index = make_tie_index()
    facts = RequestFacts(insured_name="Robert Lake", phones=["2125550101"])
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 101
    assert "tie-broken" in res.evidence


def test_tie_broken_by_policy_number():
    index = make_tie_index()
    facts = RequestFacts(insured_name="Robert Lake",
                         policy_numbers=["9300216995"])
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 102
    assert "tie-broken" in res.evidence


def test_tie_unbreakable_when_contact_matches_none():
    index = make_tie_index()
    facts = RequestFacts(insured_name="Robert Lake",
                         requester_email="nobody@nowhere.com")
    res = match_applicant(facts, index)
    assert res.status == AMBIGUOUS
    assert res.applicant_id is None
    assert set(res.candidates) == {101, 102}


def test_tie_unbreakable_when_contact_matches_both():
    # Sender email says 102, extracted phone says 101 — still a tie.
    index = make_tie_index()
    facts = RequestFacts(insured_name="Robert Lake",
                         requester_email="rob@laketwo.com",
                         phones=["2125550101"])
    res = match_applicant(facts, index)
    assert res.status == AMBIGUOUS
    assert res.applicant_id is None


def test_tie_broken_on_dba_too():
    index = make_tie_index()
    facts = RequestFacts(dba="Robert Lake",
                         requester_email="rob@lakeone.com")
    res = match_applicant(facts, index)
    assert res.status == MATCHED
    assert res.applicant_id == 101


def test_exact_ambiguous_without_contact_info_stays_held():
    # Composition check: exact-name tie + no tie-break = AMBIGUOUS.
    # Fuzzy must NOT rescue it by matching some other name.
    index = make_tie_index()
    facts = RequestFacts(insured_name="Robert Lake")
    res = match_applicant(facts, index)
    assert res.status == AMBIGUOUS
    assert "fuzzy" not in res.evidence
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
