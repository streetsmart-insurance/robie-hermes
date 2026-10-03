#!/usr/bin/env python3
"""Smoke test for the certificate edge-case fixes (PR: cert sweep edge cases).

Runs real-shaped fixtures through the REAL extraction + matching code and
asserts each edge case resolves the way Carlo approved:

  1. EPHE shape             - name + policy number in the body match the
                               right applicant (policy corroborates).
  2. Typo tolerance         - "EPHE LCC" still matches EPHE LLC (exactly one
                               candidate); nothing is guessed when 0 or 2+.
  3. Multi-insured          - "… for EPHE LLC and XYZ Inc" fans out into one
                               independent record per company (ledger keys
                               m9 and m9#2); the second company can never be
                               silently dropped.
  4. Quoted-history hygiene - on a RE: thread the NEWEST block wins; ">"
                               lines and "On ... wrote:" history are
                               stripped first.
  5. PDF filename fallback  - a filename may SUGGEST the insured when the
                               body names nobody (weak source); it must
                               confirm a real client, never invent one.
  6. Commercial preference  - "Martin Omondi" (two records, one with a DBA)
                               resolves to the DBA/commercial record.
  7. Ambiguous hold         - "Mark Dinger" (two records, no DBA on either,
                               same contact info) holds AMBIGUOUS and names
                               BOTH applicant ids for the human.
  8. Contact tie-break      - "Rosa Martinez" from the sender's own email
                               resolves to the matching record.

Usage:
    python -m robie_job_engine.cert_edge_case_smoke

Exit 0 = every case passed. Exit 1 = at least one case failed (details on
stdout). This is the named smoke test for the edge-case deploy: run it
after every deploy of the cert sweep, in Test first, on Prod when safe.
"""

from __future__ import annotations

import sys
import traceback

sys.path.insert(0, ".")

from robie_job_engine.cert_applicant_index import (
    AMBIGUOUS,
    MATCHED,
    NO_MATCH,
    build_index,
    match_applicant,
)
from robie_job_engine.cert_intake import (
    CertAttachment,
    CertEmail,
    extract_request_facts,
)
from robie_job_engine.cert_intake_runner import _build_records_for_email


def _fixture_index():
    rows = [
        {"account_name": "EPHE LLC", "applicant_id": 199205654,
         "email_primary": "sinancanlv@yahoo.com", "phones": ["7025012909"],
         "dba": ""},
        {"account_name": "Martin Omondi", "applicant_id": 213255016,
         "email_primary": "g.omondi@example.com", "phones": ["2011111111"],
         "dba": "MARTIN O OMONDI DBA GTZ TRANSPORTATION"},
        {"account_name": "Martin Omondi", "applicant_id": 216482604,
         "email_primary": "m.omondi@example.com", "phones": ["2022222222"],
         "dba": ""},
        {"account_name": "Mark Dinger", "applicant_id": 80209632,
         "email_primary": "dingrronn@hotmail.com", "phones": ["8484664104"],
         "dba": ""},
        {"account_name": "Mark Dinger", "applicant_id": 148973561,
         "email_primary": "dingrronn@hotmail.com", "phones": ["8484664104"],
         "dba": ""},
        {"account_name": "Rosa Martinez", "applicant_id": 98475316,
         "email_primary": "reach.rosa.martinez@gmail.com",
         "phones": ["8563137807"], "dba": ""},
        {"account_name": "Rosa Martinez", "applicant_id": 97763610,
         "email_primary": "rosamariemartinez21@gmail.com",
         "phones": ["2158693863"], "dba": ""},
        {"account_name": "XYZ Inc", "applicant_id": 555002,
         "email_primary": "xyz@example.com", "phones": ["2044444444"],
         "dba": ""},
    ]
    index = build_index(rows, source_path="smoke-fixture")
    # Policy-number match key (populated from the applicant export in prod).
    index.by_policy = {"9300216995": 199205654}
    return index


def _email(gmail_id: str, subject: str, from_header: str, body: str,
           attachments: list | None = None) -> CertEmail:
    return CertEmail(
        gmail_id=gmail_id, thread_id="t-" + gmail_id,
        rfc_message_id="<" + gmail_id + "@smoke>",
        from_header=from_header, subject=subject, date="2026-09-29",
        body_text=body, attachments=attachments or [],
    )


def _att(filename: str) -> CertAttachment:
    return CertAttachment(filename=filename, mime_type="application/pdf",
                          content=b"%PDF-smoke")


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("1. EPHE shape: name + policy number match the right applicant")
def _c1(index):
    email = _email(
        "m1", "Renewal COI request", "Highway <no-reply@highway.com>",
        "Please issue a certificate of insurance for EPHE LLC.\n"
        "BIPD Policy #9300216995 expires tomorrow.\n")
    facts = extract_request_facts(email)
    assert (facts.insured_name or "").rstrip(".") == "EPHE LLC", \
        f"insured extracted as {facts.insured_name!r}, want 'EPHE LLC'"
    assert "9300216995" in facts.policy_numbers, \
        f"policy numbers {facts.policy_numbers!r} missing 9300216995"
    m = match_applicant(facts, index)
    assert m.status == MATCHED and m.applicant_id == 199205654, \
        f"match={m.status} id={m.applicant_id}, want MATCHED 199205654"
    return (f"insured={facts.insured_name!r} "
            f"policy={facts.policy_numbers} -> {m.applicant_id}")


@case("2. Typo tolerance: 'EPHE LCC' matches the single candidate")
def _c2(index):
    email = _email("m2", "COI request", "Someone <a@b.com>",
                   "Please issue a certificate of insurance for EPHE LCC.")
    facts = extract_request_facts(email)
    m = match_applicant(facts, index)
    assert m.status == MATCHED and m.applicant_id == 199205654, \
        f"match={m.status} id={m.applicant_id}, want MATCHED 199205654"
    assert "fuzzy" in m.evidence.lower(), \
        f"evidence {m.evidence!r} should say how it matched"
    return f"typo -> {m.applicant_id} ({m.evidence})"


@case("3. Multi-insured: two companies become two independent records")
def _c3(index):
    email = _email("m3", "COI requests", "Broker <broker@example.com>",
                   "Please issue a certificate of insurance "
                   "for EPHE LLC and XYZ Inc.")
    facts = extract_request_facts(email)
    records = _build_records_for_email(email, facts, index)
    assert len(records) == 2, \
        f"got {len(records)} records, want 2 (one per company)"
    by_key = {r.ledger_key: r for r in records}
    assert "m3" in by_key and "m3#2" in by_key, \
        f"ledger keys {sorted(by_key)} should be m3 and m3#2"
    ids = sorted(r.match.applicant_id for r in records)
    assert ids == [555002, 199205654], \
        f"matched ids {ids}, want both companies"
    assert all(r.match.status == MATCHED for r in records), \
        "both records should match independently"
    return ("fanned out: " +
            f"{[(r.ledger_key, r.match.applicant_id) for r in records]}")


@case("4. Quoted history: newest block wins, quoted EPHE ignored")
def _c4(index):
    email = _email(
        "m4", "Re: COI request", "Broker <broker@example.com>",
        "Please issue a certificate of insurance for XYZ Inc.\n"
        "\n"
        "On Sep 1, 2026, Highway wrote:\n"
        "> certificate of insurance for EPHE LLC\n"
        "> expiring tomorrow.\n")
    facts = extract_request_facts(email)
    assert (facts.insured_name or "").rstrip(".") == "XYZ Inc", \
        f"insured={facts.insured_name!r}, want newest block 'XYZ Inc'"
    m = match_applicant(facts, index)
    assert m.status == MATCHED and m.applicant_id == 555002, \
        f"match={m.status} id={m.applicant_id}, want MATCHED 555002"
    return f"newest block wins -> {m.applicant_id}"


@case("5a. PDF filename: suggests the insured when the body names nobody")
def _c5a(index):
    email = _email("m5", "COI", "Someone <a@b.com>", "Please see attached.",
                   attachments=[_att("xyz_inc_certificate_of_insurance.pdf")])
    facts = extract_request_facts(email)
    assert facts.insured_name_source == "pdf_filename", \
        f"source={facts.insured_name_source!r}, want 'pdf_filename'"
    m = match_applicant(facts, index)
    assert m.status == MATCHED and m.applicant_id == 555002, \
        f"match={m.status} id={m.applicant_id}, want MATCHED 555002"
    return (f"filename -> {m.applicant_id} "
            f"(source={facts.insured_name_source})")


@case("5b. PDF filename: never invents a client")
def _c5b(index):
    email = _email("m5b", "COI", "Someone <a@b.com>", "Please see attached.",
                   attachments=[_att("random_vendor_coi_request.pdf")])
    facts = extract_request_facts(email)
    m = match_applicant(facts, index)
    assert m.status == NO_MATCH, \
        f"match={m.status}, want NO_MATCH (held, never guessed)"
    return "non-client filename stays held"


@case("6. Commercial preference: Martin Omondi -> DBA/GTZ record")
def _c6(index):
    email = _email("m6", "COI request", "Someone <a@b.com>",
                   "Named Insured: Martin Omondi")
    facts = extract_request_facts(email)
    m = match_applicant(facts, index)
    assert m.status == MATCHED and m.applicant_id == 213255016, \
        (f"match={m.status} id={m.applicant_id}, "
         "want MATCHED 213255016 (DBA record)")
    assert "commercial preference" in m.evidence, \
        f"evidence {m.evidence!r} should say how it matched"
    return f"commercial record wins -> {m.applicant_id} ({m.evidence})"


@case("7. Ambiguous hold: Mark Dinger names BOTH ids, never guesses")
def _c7(index):
    email = _email("m7", "COI request", "Someone <a@b.com>",
                   "Insured: Mark Dinger")
    facts = extract_request_facts(email)
    m = match_applicant(facts, index)
    assert m.status == AMBIGUOUS, f"match={m.status}, want AMBIGUOUS"
    assert set(m.candidates) == {80209632, 148973561}, \
        f"candidates={m.candidates}, want both Mark Dinger ids"
    reason = m.hold_reason()
    assert "80209632" in reason and "148973561" in reason, \
        f"hold reason must name both ids for the human: {reason!r}"
    return f"held AMBIGUOUS, reason: {reason}"


@case("8. Contact tie-break: sender email picks the Rosa Martinez record")
def _c8(index):
    email = _email("m8", "COI request",
                   "Rosa Martinez <reach.rosa.martinez@gmail.com>",
                   "Please issue a certificate of insurance for Rosa Martinez.")
    facts = extract_request_facts(email)
    m = match_applicant(facts, index)
    assert m.status == MATCHED and m.applicant_id == 98475316, \
        f"match={m.status} id={m.applicant_id}, want MATCHED 98475316"
    assert "tie-broken" in m.evidence, \
        f"evidence {m.evidence!r} should say how the tie broke"
    return f"tie broken by sender email -> {m.applicant_id} ({m.evidence})"


def main() -> int:
    index = _fixture_index()
    failures = 0
    print(f"cert edge-case smoke: {len(CASES)} cases, "
          f"fixture index {index.row_count} rows")
    for name, fn in CASES:
        try:
            detail = fn(index)
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            failures += 1
            print(f"FAIL {name}\n     {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}\n     {detail}")
    print(f"result: {len(CASES) - failures}/{len(CASES)} passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
