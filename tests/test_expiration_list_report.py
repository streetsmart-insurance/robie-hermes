"""Unit tests for the Weekly Expiration List report.

Sandeep Yadav's E1-E15 answers are LOCKED (2026-10-05); tests encode
those rules. Everything here runs offline against canned payloads shaped
like the live EZLynx responses (proven live 2026-10-05). No network, no
Sheets calls.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from robie_job_engine.reports.expiration_list import config, layout, logic, sheets, runner
from robie_job_engine.reports.expiration_list.logic import (
    AccountRow,
    RetentionRow,
    account_display_name,
    assigned_producer,
    author_label,
    build_account_row,
    clean_note_text,
    cycle_days_for_lob,
    discussion_tied_to_policy,
    fetch_retention_rows,
    group_rows,
    is_bot_note,
    is_nonrenewal_text,
    is_renewal_discussion,
    latest_staff_note,
    match_renewal_discussions,
    parse_ezlynx_date,
    parse_retention_row,
    producer_label,
    select_expiring_policies,
)
from robie_job_engine.reports.expiration_list.runner import build_rows, verify_sample
from robie_job_engine.reports.expiration_list.portal import (
    ExpirationListError,
    ExpirationPortalClient,
)

TODAY = date(2026, 10, 5)


def ms_date(d: date) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from robie_job_engine.reports.expiration_list import config

    ts = int(
        datetime(d.year, d.month, d.day, 12, 0, tzinfo=ZoneInfo(config.TIMEZONE)).timestamp()
        * 1000
    )
    return f"/Date({ts})/"


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


def test_parse_ezlynx_date_ms_and_iso():
    assert parse_ezlynx_date(ms_date(date(2026, 10, 6))) == date(2026, 10, 6)
    assert parse_ezlynx_date("2026-10-06T00:00:00") == date(2026, 10, 6)
    assert parse_ezlynx_date("2026-10-06") == date(2026, 10, 6)
    assert parse_ezlynx_date(None) is None
    assert parse_ezlynx_date("garbage") is None


# ---------------------------------------------------------------------------
# Retention rows: E1/E2/E3 (LOCKED)
# ---------------------------------------------------------------------------


class PagedClient(ExpirationPortalClient):
    def __init__(self, pages):
        self.pages = pages

    def retention_expiration_list(self, *, page_size=100, page_index=1, **kw):
        return {"results": self.pages[page_index - 1] if page_index - 1 < len(self.pages) else []}


def retention_raw(aid, days, **kw):
    raw = {"ID": aid, "DaysToExpiration": days, "FirstName": "Test",
           "LastName": f"Acct{aid}", "BusinessName": "", "PoliciesCount": 1,
           "EarliestExpirationDate": ms_date(TODAY + timedelta(days=days))}
    raw.update(kw)
    return raw


def test_retention_keeps_0_to_30_excludes_expired_and_future():
    # E1 (30-day window), E2 (0 included, negatives excluded) — LOCKED.
    client = PagedClient([
        [retention_raw(1, 0), retention_raw(2, 30), retention_raw(3, -5),
         retention_raw(4, 31), retention_raw(5, 90)],
    ])
    rows = fetch_retention_rows(client)
    assert sorted(r.applicant_id for r in rows) == [1, 2]


def test_retention_paginates_until_empty_page():
    client = PagedClient([
        [retention_raw(1, 5)],
        [retention_raw(2, 12)],
        [],
    ])
    rows = fetch_retention_rows(client)
    assert [r.applicant_id for r in rows] == [1, 2]


def test_retention_continues_while_last_row_within_window():
    # E3 (LOCKED): continue iff the LAST row of the current page has
    # DaysToExpiration <= 30 AND another page exists.
    client = PagedClient([
        [retention_raw(1, 25)],
        [retention_raw(2, 30)],
        [retention_raw(3, 35)],
    ])
    rows = fetch_retention_rows(client)
    assert [r.applicant_id for r in rows] == [1, 2]


def test_retention_stops_when_last_row_exceeds_window():
    client = PagedClient([
        [retention_raw(1, 5)],
        [retention_raw(2, 45), retention_raw(3, 60)],
        [retention_raw(4, 70)],
    ])
    rows = fetch_retention_rows(client)
    assert [r.applicant_id for r in rows] == [1]


def test_retention_skips_malformed_rows():
    client = PagedClient([[{"ID": "bad", "DaysToExpiration": "x"}, retention_raw(9, 3)]])
    rows = fetch_retention_rows(client)
    assert [r.applicant_id for r in rows] == [9]


def test_parse_retention_row_renewal_manager():
    # E9: Renewal Center "Renewal Manager" parsed (field name INFERRED).
    raw = retention_raw(1, 5, RenewalManager="Taylor Cimei")
    assert parse_retention_row(raw).renewal_manager == "Taylor Cimei"
    assert parse_retention_row(retention_raw(1, 5)).renewal_manager == ""


# ---------------------------------------------------------------------------
# Policy selection: E5 cycle days, E8 renewal exclusion, E15 cancellation
# ---------------------------------------------------------------------------


def card(num, exp, status=1, pending=False, ptype="", lob="Auto"):
    return {
        "policyNumber": num, "lob": lob, "expirationDate": ms_date(exp),
        "policyStatusViewModelID": status, "hasPending": pending, "pendingType": ptype,
    }


def test_select_expiring_policies_filters_status_window_and_renewed():
    cards = [
        card("A1", TODAY + timedelta(days=10)),                       # keep
        card("A2", TODAY + timedelta(days=31)),                       # keep (inclusive)
        card("A3", TODAY + timedelta(days=32)),                       # out of window
        card("A4", TODAY - timedelta(days=1)),                        # expired
        card("A5", TODAY + timedelta(days=5), status=2),               # inactive
        card("A6", TODAY + timedelta(days=5), status="x"),            # bad status
        card("A7", TODAY + timedelta(days=5), pending=True, ptype="Renewal"),  # E8: EXCLUDED
        card("A8", TODAY + timedelta(days=5), pending=True, ptype="Endorsement"),  # E15: kept, no effect
        card("A9", TODAY + timedelta(days=5), pending=True, ptype="Cancellation"),  # E15: kept
    ]
    out = select_expiring_policies(cards, today=TODAY)
    by_num = {p.policy_number: p for p in out}
    assert sorted(by_num) == ["A1", "A2", "A8", "A9"]  # A7 dropped (E8); A3/A4/A5/A6 out
    assert by_num["A8"].pending_type == "Endorsement"
    assert by_num["A9"].pending_type == "Cancellation"


def test_select_expiring_policies_renewal_exclusion_case_insensitive():
    cards = [card("R1", TODAY + timedelta(days=5), pending=True, ptype="RENEWAL")]
    assert select_expiring_policies(cards, today=TODAY) == []


def test_cycle_days_for_lob():
    # E5 (LOCKED): 90 personal lines, 120 commercial/trucking.
    assert cycle_days_for_lob("Homeowners") == (90, True)
    assert cycle_days_for_lob("Personal Auto") == (90, True)
    assert cycle_days_for_lob("Commercial Auto") == (120, True)
    assert cycle_days_for_lob("Workers Comp") == (120, True)
    assert cycle_days_for_lob("") == (120, False)          # missing -> 120 + flag
    assert cycle_days_for_lob("Some Unknown Line") == (120, False)  # unmapped -> 120 + flag


def test_select_expiring_policies_sets_cycle_days_and_lob_flags():
    cards = [
        card("H1", TODAY + timedelta(days=10), lob="Homeowners"),
        card("C1", TODAY + timedelta(days=10), lob="Commercial Auto"),
        card("X1", TODAY + timedelta(days=10), lob=""),
    ]
    out = select_expiring_policies(cards, today=TODAY)
    by_num = {p.policy_number: p for p in out}
    assert by_num["H1"].cycle_days == 90 and by_num["H1"].lob_flag is False
    assert by_num["C1"].cycle_days == 120 and by_num["C1"].lob_flag is False
    assert by_num["X1"].cycle_days == 120 and by_num["X1"].lob_flag is True


def test_policy_line_format_collapses_whitespace():
    out = select_expiring_policies(
        [{"policyNumber": "  HO-123  ", "lob": " Home\nOwners ",
          "expirationDate": ms_date(TODAY + timedelta(days=3)),
          "policyStatusViewModelID": 1, "hasPending": False, "pendingType": ""}],
        today=TODAY,
    )
    assert out[0].lob == "Home Owners"
    assert out[0].policy_number == "HO-123"


# ---------------------------------------------------------------------------
# Sidebar helpers: E6 (account-level assignment wins), E9 fallback chain
# ---------------------------------------------------------------------------


def test_account_display_name_prefers_full_account_name():
    sidebar = {"applicant": {"accountName": "Jane Smith & John Smith", "name": "Jane Smith",
                            "assignment": {"assignedTo": "Ashley Huntley"}}}
    assert account_display_name(sidebar) == "Jane Smith & John Smith"
    # E6 (LOCKED): account-level assignedTo wins.
    assert assigned_producer(sidebar) == "Ashley Huntley"


def test_assigned_producer_fallback_chain():
    # E9 (LOCKED): sidebar assignedTo -> Retention Center Renewal Manager ->
    # "" (routed to the Unassigned section).
    empty = {"applicant": {"accountName": "X", "assignment": {"assignedTo": ""}}}
    assert assigned_producer(empty) == ""
    assert assigned_producer(empty, fallback="Taylor Cimei") == "Taylor Cimei"


# ---------------------------------------------------------------------------
# Author labels: E11 (LOCKED) — "First L", no period
# ---------------------------------------------------------------------------


def test_author_label_first_name_plus_last_initial():
    assert author_label("Jazmin Molina") == "Jazmin M"
    assert author_label("Daniela Aguilar") == "Daniela A"
    assert author_label("Ashley Huntley") == "Ashley H"
    assert author_label("Madonna") == "Madonna"  # single token stays as-is
    assert author_label("") == ""
    assert author_label("  ") == ""


# ---------------------------------------------------------------------------
# Renewal discussion matching: E4 (LOCKED)
# ---------------------------------------------------------------------------


def disc(did, title, modified, notes=0):
    return {"discussionId": did, "title": title, "lastModified": ms_date(modified),
            "noteCount": notes}


def test_renewal_title_include_patterns():
    assert is_renewal_discussion("Homeowners Renewal")
    assert is_renewal_discussion("Renwal - Auto quote")       # misspelling
    assert is_renewal_discussion("NonRenewal- Homeowners | HO123")
    assert is_renewal_discussion("RQ upcoming NR")            # whole-word NR
    assert is_renewal_discussion("Mortgage Verification")
    assert is_renewal_discussion("Non-renewal notice")        # accepted with policy tie
    assert is_renewal_discussion("NR letter sent")            # whole-word NR


def test_renewal_title_whole_word_nr_not_inside_words():
    assert not is_renewal_discussion("Inroads follow-up")
    assert not is_renewal_discussion("TurnRound notes")


def test_renewal_title_reject_patterns():
    # Rejected first, case-insensitive substring on title.
    assert not is_renewal_discussion("Master Certificate Renewal")
    assert not is_renewal_discussion("Policy Change Request")
    assert not is_renewal_discussion("COI request")
    assert not is_renewal_discussion("Audit discussion")
    assert not is_renewal_discussion("policy audit review")
    assert not is_renewal_discussion("Certificate of renewal coverage")


def test_match_renewal_discussions_title_include_exclude_and_cycle():
    cycle_start = TODAY - timedelta(days=120)
    discs = [
        disc(1, "Auto Renewal FAHO624035", TODAY - timedelta(days=10)),
        disc(2, "Master Certificate Renewal", TODAY - timedelta(days=10)),
        disc(3, "COI request", TODAY - timedelta(days=10)),
        disc(4, "Policy audit review", TODAY - timedelta(days=10)),
        disc(5, "Auto Renewal FAHO624035", TODAY - timedelta(days=200)),
    ]
    matched = match_renewal_discussions(discs, cycle_start=cycle_start)
    assert [d["discussionId"] for d in matched] == [1]


def test_match_renewal_discussions_most_recent_first():
    cycle_start = TODAY - timedelta(days=120)
    discs = [
        disc(1, "Renewal FAHO1", TODAY - timedelta(days=30)),
        disc(2, "Renewal FAHO1", TODAY - timedelta(days=5)),
    ]
    matched = match_renewal_discussions(discs, cycle_start=cycle_start)
    assert [d["discussionId"] for d in matched] == [2, 1]


def test_discussion_tied_to_policy_title_or_notes():
    assert discussion_tied_to_policy("Auto Renewal FAHO624035", ["no numbers here"], ["FAHO624035"])
    assert discussion_tied_to_policy("Auto Renewal", ["Policy FAHO624035 attached"], ["FAHO624035"])
    assert not discussion_tied_to_policy("Auto Renewal", ["no numbers here"], ["FAHO624035"])
    assert not discussion_tied_to_policy("Auto Renewal FAHO624035", ["..."], [])  # no policy numbers -> no tie


def test_nonrenewal_text_markers():
    assert is_nonrenewal_text("Non-renewal - Auto")
    assert is_nonrenewal_text("NonRenewal- Homeowners")
    assert is_nonrenewal_text("RQ upcoming NR")
    assert not is_nonrenewal_text("Renewal quoted, bound")


def test_nonrenewal_prefix_exact_string():
    # E7 (LOCKED): exact marker with en dash.
    assert config.NONRENEWAL_NOTE_PREFIX == "NON-RENEWAL – "
    assert config.CANCELLATION_NOTE_PREFIX == "CANCELLATION PENDING – "


# ---------------------------------------------------------------------------
# Staff note selection (Step 3): E11, E12 (LOCKED)
# ---------------------------------------------------------------------------


def note(body, author, created="2026-09-20T10:00:00", policy="P1"):
    return {"note": body, "createdByName": author, "created": created, "policyNumber": policy}


def test_is_bot_note_markers():
    assert is_bot_note("Robie was here - filed note", "Ralph Smith")
    assert is_bot_note("Quote attached", "Automation Center")
    assert is_bot_note("Text sent by system", "Jane")
    assert is_bot_note("Lead Info Lead Id 123", "Jane")
    assert is_bot_note("UNDERWRITER EMAIL RESPONSE RECEIVED", "Jane")
    assert is_bot_note("anything", "Robie")
    assert is_bot_note("anything", "System")
    assert not is_bot_note("Called client, renewal quoted", "Daniela Aguilar")


def test_latest_staff_note_skips_bots_and_picks_most_recent():
    notes = [
        note("Robie was here", "Robie", "2026-09-25T10:00:00"),
        note("Older staff note", "Daniela Aguilar", "2026-09-10T10:00:00"),
        note("Newer <b>staff</b> note", "Jazmin Molina", "2026-09-20T10:00:00"),
    ]
    text, who = latest_staff_note(notes)
    assert text == "Newer staff note"
    assert who == "Jazmin M"  # E11


def test_latest_staff_note_placeholder_means_no_notes():
    notes = [note("Upcoming renewal add notes here", "System", "2026-09-20T10:00:00")]
    text, who = latest_staff_note(notes)
    assert text == config.NO_NOTES_TEXT
    assert who == ""


def test_latest_staff_note_empty_means_no_notes():
    text, who = latest_staff_note([])
    assert text == config.NO_NOTES_TEXT
    assert who == ""


def test_clean_note_text_truncates_at_sentence_boundary():
    # E12 (LOCKED): last ". " at or before char 450, punctuation included.
    text = "First sentence. " + "word " * 200 + " Final remark here!"
    cleaned = clean_note_text(text)
    assert cleaned == "First sentence.…"


def test_clean_note_text_word_boundary_fallback_no_ellipsis_short():
    # No sentence boundary -> word boundary + "…".
    long_text = "word " * 200
    cleaned = clean_note_text(long_text)
    assert len(cleaned) <= config.NOTE_CHAR_CAP
    assert cleaned.endswith("…")
    assert not cleaned.endswith(" …")
    # At or under the cap: untouched, no ellipsis.
    assert clean_note_text("x" * 450) == "x" * 450


def test_clean_note_text_sentence_boundary_with_bang():
    text = "Question asked? " + "word " * 200
    cleaned = clean_note_text(text)
    assert cleaned == "Question asked?…"


# ---------------------------------------------------------------------------
# Account row assembly (Steps 2-3): E6/E7/E8/E9/E11/E15 (LOCKED)
# ---------------------------------------------------------------------------


def full_client(**over):
    """Canned per-account client: sidebar + policies + discussions + details."""
    sidebar = {"applicant": {"accountName": "Faisal Panjwani",
                            "assignment": {"assignedTo": "Ashley Huntley"}}}
    cards = [card("FAHO624035", TODAY + timedelta(days=10), lob="Homeowners")]
    discussions = {"discussions": [], "pageCount": 1, "rowCount": 0}
    details = {}

    class C(ExpirationPortalClient):
        def sidebar(self, aid):
            return over.get("sidebar", sidebar)

        def policies(self, aid):
            return over.get("cards", cards)

        def paged_discussions(self, aid, *, page_number=1, page_size=60):
            return over.get("discussions", discussions)

        def discussion_detail(self, did, aid):
            return over.get("details", details).get(did, {"discussion": {"notes": []}})

    return C()


def retention_row(aid=21587064, days=10, **kw):
    r = RetentionRow(applicant_id=aid, days_to_expiration=days,
                     earliest_expiration=TODAY + timedelta(days=days))
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_build_row_normal_renewal_notes():
    details = {7: {"discussion": {"notes": [
        note("Renewal quoted, awaiting client", "Ashley Huntley", "2026-09-28T09:00:00"),
    ]}}}
    discussions = {"discussions": [disc(7, "Homeowners Renewal FAHO624035",
                                       TODAY - timedelta(days=7))],
                   "pageCount": 1, "rowCount": 1}
    client = full_client(discussions=discussions, details=details)
    row = logic.run_account(client, retention_row(), today=TODAY)
    assert row.account_name == "Faisal Panjwani"
    assert row.producer == "Ashley Huntley"
    assert row.notes == "Renewal quoted, awaiting client"
    assert row.last_activity_by == "Ashley H"  # E11
    assert row.policy_lines == ["Homeowners | FAHO624035"]
    assert row.nonrenewal is False
    assert row.cancellation_pending is False


def test_build_row_renewed_policies_drop_off():
    # E8 (LOCKED): pendingType="Renewal" excluded; all renewed -> account gone.
    cards = [card("FAHO624035", TODAY + timedelta(days=10), pending=True, ptype="Renewal")]
    client = full_client(cards=cards)
    assert logic.run_account(client, retention_row(), today=TODAY) is None


def test_build_row_partial_renewal_keeps_only_live_policies():
    # E8: the renewed policy drops off; the live one stays.
    cards = [
        card("P1", TODAY + timedelta(days=10), pending=True, ptype="Renewal"),
        card("P2", TODAY + timedelta(days=12)),
    ]
    client = full_client(cards=cards)
    row = logic.run_account(client, retention_row(), today=TODAY)
    assert row.policy_lines == ["Auto | P2"]


def test_build_row_nonrenewal_marker_exact_prefix():
    # E7 (LOCKED): "NON-RENEWAL – " (en dash) starts Notes.
    discussions = {"discussions": [disc(8, "Non-renewal notice FAHO624035",
                                       TODAY - timedelta(days=7))],
                   "pageCount": 1, "rowCount": 1}
    details = {8: {"discussion": {"notes": [
        note("Client declined renewal", "Ashley Huntley", "2026-09-28T09:00:00")]}}}
    client = full_client(discussions=discussions, details=details)
    row = logic.run_account(client, retention_row(), today=TODAY)
    assert row.nonrenewal is True
    assert row.notes == "NON-RENEWAL – Client declined renewal"


def test_build_row_pending_cancellation_red_flag():
    # E15 (LOCKED): "CANCELLATION PENDING – " prepended; red-fill intent set.
    cards = [card("FAHO624035", TODAY + timedelta(days=10), pending=True, ptype="Cancellation")]
    discussions = {"discussions": [disc(9, "Homeowners Renewal FAHO624035",
                                       TODAY - timedelta(days=7))],
                   "pageCount": 1, "rowCount": 1}
    details = {9: {"discussion": {"notes": [
        note("Carrier sent cancellation notice", "Ashley Huntley",
             "2026-09-28T09:00:00")]}}}
    client = full_client(cards=cards, discussions=discussions, details=details)
    row = logic.run_account(client, retention_row(), today=TODAY)
    assert row.cancellation_pending is True
    assert row.notes == "CANCELLATION PENDING – Carrier sent cancellation notice"


def test_build_row_pending_endorsement_no_effect():
    # E15 (LOCKED): Endorsement -> normal row, no marker.
    cards = [card("FAHO624035", TODAY + timedelta(days=10), pending=True, ptype="Endorsement")]
    client = full_client(cards=cards)
    row = logic.run_account(client, retention_row(), today=TODAY)
    assert row.cancellation_pending is False
    assert row.notes == config.NO_NOTES_TEXT


def test_build_row_policy_number_only_in_notes_accepted():
    # E4 (LOCKED): tie via notes (not title) is accepted.
    details = {10: {"discussion": {"notes": [
        note("Renewal quoted on policy FAHO624035", "Taylor Cimei", "2026-09-28T09:00:00"),
    ]}}}
    discussions = {"discussions": [disc(10, "Auto Renewal", TODAY - timedelta(days=7))],
                   "pageCount": 1, "rowCount": 1}
    client = full_client(discussions=discussions, details=details)
    row = logic.run_account(client, retention_row(), today=TODAY)
    assert row.notes == "Renewal quoted on policy FAHO624035"
    assert row.last_activity_by == "Taylor C"


def test_build_row_no_policy_tie_means_no_notes():
    # E4 (LOCKED): title matches but no policy number anywhere -> "No notes".
    details = {11: {"discussion": {"notes": [
        note("Renewal quoted", "Taylor Cimei", "2026-09-28T09:00:00"),
    ]}}}
    discussions = {"discussions": [disc(11, "Auto Renewal", TODAY - timedelta(days=7))],
                   "pageCount": 1, "rowCount": 1}
    client = full_client(discussions=discussions, details=details)
    row = logic.run_account(client, retention_row(), today=TODAY)
    assert row.notes == config.NO_NOTES_TEXT
    assert row.last_activity_by == ""


def test_build_row_rejected_discussion_titles_ignored():
    # E4 (LOCKED): Master Certificate Renewal / Policy Change Request /
    # COI / audit titles never feed Notes.
    discussions = {"discussions": [
        disc(12, "Master Certificate Renewal", TODAY - timedelta(days=7)),
        disc(13, "Policy Change Request", TODAY - timedelta(days=8)),
        disc(14, "COI request", TODAY - timedelta(days=9)),
    ], "pageCount": 1, "rowCount": 3}
    details = {d: {"discussion": {"notes": [
        note("some note", "Taylor Cimei", "2026-09-28T09:00:00")]}} for d in (12, 13, 14)}
    client = full_client(discussions=discussions, details=details)
    row = logic.run_account(client, retention_row(), today=TODAY)
    assert row.notes == config.NO_NOTES_TEXT


def test_build_row_task_notes_are_considered():
    details = {9: {"discussion": {"notes": [
        {"note": "", "createdByName": "System", "created": "2026-09-28T09:00:00",
         "task": {"taskNotes": [
             {"note": "Task note from staff", "createdByName": "Daniela Aguilar",
              "created": "2026-09-29T09:00:00"}]}},
    ]}}}
    discussions = {"discussions": [disc(9, "Renewal FAHO624035", TODAY - timedelta(days=7))],
                   "pageCount": 1, "rowCount": 1}
    client = full_client(discussions=discussions, details=details)
    row = logic.run_account(client, retention_row(), today=TODAY)
    assert row.notes == "Task note from staff"
    assert row.last_activity_by == "Daniela A"


def test_build_row_renewal_manager_fallback():
    # E9 (LOCKED): empty sidebar assignedTo -> Renewal Center Renewal Manager.
    sidebar = {"applicant": {"accountName": "No Assign", "assignment": {"assignedTo": ""}}}
    rr = retention_row(aid=77, days=2, renewal_manager="Taylor Cimei")
    row = logic.run_account(full_client(sidebar=sidebar), rr, today=TODAY)
    assert row.producer == "Taylor Cimei"


def test_run_account_collects_failures_fail_closed():
    class Broken(ExpirationPortalClient):
        def retention_expiration_list(self, *, page_size=100, page_index=1, **kw):
            return {"results": [retention_raw(99, 2)] if page_index == 1 else []}

        def sidebar(self, aid):
            raise ExpirationListError("sidebar down")

    rows, failures = build_rows(Broken())
    assert rows == []
    assert failures == [{"applicant_id": 99, "error": "sidebar down"}]


# ---------------------------------------------------------------------------
# Grouping (Step 5): E9/E10 — trailing "Unassigned" section
# ---------------------------------------------------------------------------


def mkrow(producer, name="Acct", days=5, aid=1):
    return AccountRow(applicant_id=aid, account_name=name, producer=producer,
                      days_to_expiration=days)


def test_group_rows_section_producer_order_and_unassigned():
    rows = [
        mkrow("Maria Bara", "T1", 3, 10),        # Trucking
        mkrow("Ashley Huntley", "P1", 9, 11),    # Personal
        mkrow("Taylor Cimei", "C1", 2, 12),      # Commercial
        mkrow("Taylor Cimei", "C2", 1, 13),      # Commercial, earlier expiry first
        mkrow("Nobody Here", "U1", 4, 14),       # unassigned
        mkrow("", "U2", 6, 15),                  # unassigned (empty fallback)
    ]
    grouped = group_rows(rows)
    assert [(s, p) for s, p, _ in grouped] == [
        ("Commercial", "Taylor Cimei"),
        ("Personal", "Ashley Huntley"),
        ("Trucking", "Maria Bara"),
        ("Unassigned", ""),
    ]
    commercial = grouped[0][2]
    assert [r.account_name for r in commercial] == ["C2", "C1"]
    unassigned = grouped[3][2]
    assert [r.account_name for r in unassigned] == ["U1", "U2"]


def test_producer_label():
    assert producer_label("Daniela Aguilar") == "Daniela"
    assert producer_label("Ashley Huntley") == "Ashley"
    assert producer_label("Taylor Cimei") == "Taylor Cimei"


# ---------------------------------------------------------------------------
# Layout: E15 red fill; E8 — no green
# ---------------------------------------------------------------------------


def test_build_sheet_rows_structure():
    rows = [
        AccountRow(applicant_id=12, account_name="C1 & Co", producer="Taylor Cimei",
                   days_to_expiration=2, policy_lines=["Auto | A1"],
                   notes="n", last_activity_by="Taylor"),
        AccountRow(applicant_id=11, account_name="P1", producer="Ashley Huntley",
                   days_to_expiration=9, policy_lines=["Home | H1"],
                   notes="m", last_activity_by="Ashley"),
    ]
    sheet_rows = layout.build_sheet_rows(rows)
    vals = [r.values for r in sheet_rows]
    # header
    assert vals[0] == ["", "Account Name", "Notes", "Policy No", "Last Activity by"]
    assert sheet_rows[0].bold is True
    # Commercial section row: yellow bold
    assert vals[1][0] == "Commercial" and sheet_rows[1].background == config.COLOR_SECTION_YELLOW
    # producer first row: blue in col A only
    assert vals[2][0] == "Taylor Cimei"
    assert sheet_rows[2].background == config.COLOR_PRODUCER_BLUE
    assert sheet_rows[2].background_cols == (0, 1)
    # hyperlink formula
    assert vals[2][1] == '=HYPERLINK("https://app.ezlynx.com/web/account/12/overview","C1 & Co")'
    # blank row after producer
    assert vals[3] == ["", "", "", "", ""]
    # Personal section
    assert vals[4][0] == "Personal"
    assert vals[5][0] == "Ashley"  # label mapping


def test_build_sheet_rows_cancellation_red_fill():
    # E15 (LOCKED): red fill across A-E on pending-Cancellation rows.
    rows = [AccountRow(applicant_id=12, account_name="C1", producer="Taylor Cimei",
                       days_to_expiration=2, policy_lines=["Auto | A1"],
                       notes="CANCELLATION PENDING – n", last_activity_by="Taylor C",
                       cancellation_pending=True)]
    sheet_rows = layout.build_sheet_rows(rows)
    acct = sheet_rows[2]
    assert acct.is_cancellation is True
    assert acct.background == config.COLOR_CANCELLATION_RED
    assert acct.background_cols == (0, 5)


def test_build_sheet_rows_multi_policy_line_breaks():
    rows = [mkrow("Taylor Cimei", "C1", 2, 12)]
    rows[0].policy_lines = ["Auto | A1", "Home | H1"]
    sheet_rows = layout.build_sheet_rows(rows)
    assert sheet_rows[2].values[3] == "Auto | A1\nHome | H1"


def test_html_table_reference():
    rows = [mkrow("Taylor Cimei", "C1", 2, 12)]
    rows[0].policy_lines = ["Auto | A1", "Home | H1"]
    table = layout.build_html_table(rows)
    assert "<table" in table and "Commercial" in table
    assert "https://app.ezlynx.com/web/account/12/overview" in table
    assert "Auto | A1<br>Home | H1" in table


def test_format_requests_payload_shape():
    rows = layout.build_sheet_rows([mkrow("Taylor Cimei", "C1", 2, 12)])
    reqs = layout.format_requests(12345, rows)
    kinds = {list(r)[0] for r in reqs}
    assert {"repeatCell", "updateDimensionProperties"} <= kinds
    # section row background request targets full width
    bg = [r["repeatCell"] for r in reqs if "repeatCell" in r
          and "backgroundColor" in r["repeatCell"]["cell"]["userEnteredFormat"]]
    assert any(r["range"]["startColumnIndex"] == 0 and r["range"]["endColumnIndex"] == 5
               for r in bg)
    # column widths present for A..E
    widths = [r["updateDimensionProperties"]["properties"]["pixelSize"]
              for r in reqs if "updateDimensionProperties" in r]
    assert widths == [180, 340, 420, 250, 110]
    # wrap on Notes column
    assert any(r["repeatCell"]["cell"]["userEnteredFormat"].get("wrapStrategy") == "WRAP"
               and r["repeatCell"]["range"]["startColumnIndex"] == 2
               for r in reqs if "repeatCell" in r)


def test_format_requests_cancellation_red_payload():
    # E15: the red fill is payload-level (dry-run testable), #f4cccc.
    rows = [mkrow("Taylor Cimei", "C1", 2, 12)]
    rows[0].cancellation_pending = True
    sheet_rows = layout.build_sheet_rows(rows)
    reqs = layout.format_requests(12345, sheet_rows)
    fills = [
        r["repeatCell"] for r in reqs if "repeatCell" in r
        and "backgroundColor" in r["repeatCell"]["cell"]["userEnteredFormat"]
    ]
    red = [f for f in fills
           if f["cell"]["userEnteredFormat"]["backgroundColor"]
           == {"red": 244 / 255, "green": 204 / 255, "blue": 204 / 255}]
    assert len(red) == 1
    assert red[0]["range"]["startColumnIndex"] == 0
    assert red[0]["range"]["endColumnIndex"] == 5


# ---------------------------------------------------------------------------
# Sheets fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_stable_and_sensitive():
    a = [["", "X", "n", "P", "J"]]
    assert sheets.fingerprint(a) == sheets.fingerprint([["", "X", "n", "P", "J"]])
    assert sheets.fingerprint(a) != sheets.fingerprint([["", "X", "n", "P", "K"]])
    # only A:E matter
    assert sheets.fingerprint([["a", "b", "c", "d", "e", "EXTRA"]]) == sheets.fingerprint(
        [["a", "b", "c", "d", "e"]])


# ---------------------------------------------------------------------------
# Runner: E13 dated tabs (Sheet1 never written), dry-run writes nothing,
# E14 checklist in every summary
# ---------------------------------------------------------------------------


class FakeService:
    """Minimal fake of the Sheets v4 service surface we use."""

    def __init__(self, existing=None, tabs=None):
        self.existing = existing or []
        self.writes = []
        self.cleared = []
        self.format_batches = []
        self.tabs = dict(tabs) if tabs is not None else {"Sheet1": 100}
        self.copied = []

    # spreadsheets() chaining
    def spreadsheets(self):
        return self

    def get(self, spreadsheetId=None):
        outer = self

        class _Get:
            def execute(self):
                return {"sheets": [{"properties": {"title": t, "sheetId": i}}
                                   for t, i in outer.tabs.items()]}
        return _Get()

    def values(self):
        outer = self

        class _Values:
            def get(self, spreadsheetId=None, range=None):
                class _R:
                    def execute(inner):
                        return {"values": outer.existing}
                return _R()

            def update(self, spreadsheetId=None, range=None, valueInputOption=None, body=None):
                outer.writes.append((range, body["values"]))
                class _R:
                    def execute(inner):
                        return {}
                return _R()

            def clear(self, spreadsheetId=None, range=None, body=None):
                outer.cleared.append(range)
                class _R:
                    def execute(inner):
                        return {}
                return _R()
        return _Values()

    def batchUpdate(self, spreadsheetId=None, body=None):
        outer = self
        outer.format_batches.append(body["requests"])

        class _R:
            def execute(inner):
                # apply renames so duplicate_tab() can find the copy
                for req in body["requests"]:
                    usp = req.get("updateSheetProperties")
                    if usp:
                        sid = usp["properties"]["sheetId"]
                        new_title = usp["properties"]["title"]
                        for t, i in list(outer.tabs.items()):
                            if i == sid:
                                del outer.tabs[t]
                                outer.tabs[new_title] = sid
                                break
                return {"replies": [{"addSheet": {"properties": {"sheetId": 999}}}]}
        return _R()

    def sheets(self):
        outer = self

        class _Sheets:
            def copyTo(self, spreadsheetId=None, sheetId=None, body=None):
                outer.copied.append(sheetId)
                title = next((t for t, i in outer.tabs.items() if i == sheetId), "Unknown")
                outer.tabs[f"Copy of {title}"] = 222
                class _R:
                    def execute(inner):
                        return {}
                return _R()
        return _Sheets()


def fixture_dir(tmp_path, tag, **kw):
    fx = tmp_path / tag
    fx.mkdir()
    (fx / "retention_page_1.json").write_text(json.dumps(
        {"results": [retention_raw(12, 2)]}))
    (fx / "sidebar_12.json").write_text(json.dumps(
        {"applicant": {"accountName": "C1", "assignment": {"assignedTo": "Taylor Cimei"}}}))
    (fx / "policies_12.json").write_text(json.dumps(
        {"policyCards": [card("A1", TODAY + timedelta(days=2), lob="Auto")]}))
    (fx / "discussions_12.json").write_text(json.dumps(kw.get(
        "discussions", {"discussions": [], "pageCount": 1, "rowCount": 0})))
    return fx


def dated_tab_name():
    return f"Expiration List {logic.today_et().isoformat()}"


def test_dry_run_writes_nothing_and_carries_checklist(monkeypatch, tmp_path):
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "state"))
    fx = fixture_dir(tmp_path, "fx")
    result = runner.run_report(fixtures=fx, write=False)
    assert result["accounts"] == 1
    assert result["write"] == "dry-run: no sheet writes"
    assert result["dry_run_preview"]["row_count"] > 0
    # E13: the would-be tab is the dated one, never Sheet1.
    assert result["dry_run_preview"]["tab"] == dated_tab_name()
    # E14: checklist rides on every summary.
    assert result["verification_checklist"] == config.VERIFICATION_CHECKLIST
    assert not (tmp_path / "state" / "state.json").exists()


def test_write_duplicates_sheet1_into_new_dated_tab(monkeypatch, tmp_path):
    # E13 (LOCKED): new tab born by duplicating Sheet1; Sheet1 never written.
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "state"))
    fake = FakeService()
    monkeypatch.setattr(runner.sheets, "connect", lambda: fake)
    fx = fixture_dir(tmp_path, "fxw")
    result = runner.run_report(fixtures=fx, write=True)
    tab = dated_tab_name()
    assert result["write"] == f"wrote 4 rows to new dated tab '{tab}' (duplicated from Sheet1)"
    assert fake.copied == [100]  # Sheet1's sheetId duplicated
    assert fake.writes  # values written
    assert all(rng.startswith(f"'{tab}'") for rng, _ in fake.writes)
    assert all("Sheet1" not in rng for rng, _ in fake.writes)  # Sheet1 never touched
    assert fake.format_batches
    state_file = tmp_path / "state" / "state.json"
    assert state_file.exists()
    saved = json.loads(state_file.read_text())
    assert saved["tab"] == tab
    assert saved["account_ids"] == [12]


def test_write_fails_closed_when_dated_tab_exists(monkeypatch, tmp_path):
    # E13 (LOCKED): fail closed if the dated tab already exists.
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "state"))
    fake = FakeService(tabs={"Sheet1": 100, dated_tab_name(): 111})
    monkeypatch.setattr(runner.sheets, "connect", lambda: fake)
    fx = fixture_dir(tmp_path, "fxe")
    with pytest.raises(sheets.SheetsError):
        runner.run_report(fixtures=fx, write=True)
    assert fake.writes == []
    assert fake.copied == []


def test_write_force_overwrites_only_same_named_dated_tab(monkeypatch, tmp_path):
    # E13 (LOCKED): --force may overwrite ONLY the same-named dated tab.
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "state"))
    fake = FakeService(tabs={"Sheet1": 100, dated_tab_name(): 111})
    monkeypatch.setattr(runner.sheets, "connect", lambda: fake)
    fx = fixture_dir(tmp_path, "fxf")
    result = runner.run_report(fixtures=fx, write=True, force=True)
    tab = dated_tab_name()
    assert result["write"] == f"wrote 4 rows to existing dated tab '{tab}' (--force)"
    assert fake.writes  # values written to the dated tab
    assert fake.cleared  # cleared first
    assert all("Sheet1" not in rng for rng, _ in fake.writes)
    assert fake.copied == []  # no duplication needed


def test_write_refuses_sheet1_as_target(monkeypatch, tmp_path):
    # E13 (LOCKED): Sheet1 is the read-only template — never written.
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "state"))
    fake = FakeService()
    monkeypatch.setattr(runner.sheets, "connect", lambda: fake)
    fx = fixture_dir(tmp_path, "fxs")
    with pytest.raises(sheets.SheetsError):
        runner.run_report(fixtures=fx, write=True, tab="Sheet1")


def test_write_duplication_fails_closed_without_sheet1(monkeypatch, tmp_path):
    # No template -> abort, nothing written.
    monkeypatch.setenv(config.STATE_DIR_ENV, str(tmp_path / "state"))
    fake = FakeService(tabs={})
    monkeypatch.setattr(runner.sheets, "connect", lambda: fake)
    fx = fixture_dir(tmp_path, "fxn")
    with pytest.raises(sheets.SheetsError):
        runner.run_report(fixtures=fx, write=True)
    assert fake.writes == []


def test_verify_sample_reports_diffs():
    rows = [mkrow("Taylor Cimei", "C1", 2, 12)]
    rows[0].policy_lines = ["Auto | A1"]
    rows[0].notes = "stale note"

    class C(ExpirationPortalClient):
        def sidebar(self, aid):
            return {"applicant": {"accountName": "C1",
                                  "assignment": {"assignedTo": "Taylor Cimei"}}}

        def policies(self, aid):
            return [card("A1", TODAY + timedelta(days=2), lob="Auto")]

        def paged_discussions(self, aid, *, page_number=1, page_size=60):
            return {"discussions": [], "pageCount": 1, "rowCount": 0}

        def discussion_detail(self, did, aid):
            return {"discussion": {"notes": []}}

    corrections = verify_sample(C(), rows, 1)
    assert corrections and corrections[0]["diffs"] == ["notes"]


def test_build_rows_collects_failures():
    class C(ExpirationPortalClient):
        def retention_expiration_list(self, *, page_size=100, page_index=1, **kw):
            return {"results": [retention_raw(99, 2)] if page_index == 1 else []}

        def sidebar(self, aid):
            raise ExpirationListError("sidebar down")

    rows, failures = build_rows(C())
    assert rows == []
    assert failures == [{"applicant_id": 99, "error": "sidebar down"}]


def test_summarize_keys():
    rows = [
        AccountRow(applicant_id=1, account_name="A1", producer="Nobody", days_to_expiration=2,
                   lob_flags=["X1"]),
        AccountRow(applicant_id=2, account_name="A2", producer="Taylor Cimei",
                   days_to_expiration=3, nonrenewal=True, cancellation_pending=True),
    ]
    s = runner.summarize(rows)
    assert s["accounts"] == 2
    assert s["nonrenewal"] == [2]
    assert s["cancellation_pending"] == [2]
    assert s["unassigned_producer"] == [{"applicant_id": 1, "producer": "Nobody"}]
    assert s["lob_review"] == [{"applicant_id": 1, "policies": ["X1"]}]
    assert s["verification_checklist"] == config.VERIFICATION_CHECKLIST


# --- X1 / X6 locks from Sandeep's 2026-10-06 answers ----------------------

def test_canonical_sheet_id_matches_sandeep_confirmed_url():
    # X1 (LOCKED 2026-10-06): Sandeep confirmed the canonical sheet URL
    # https://docs.google.com/spreadsheets/d/<id>/edit (gid=0).
    assert config.SHEET_ID == "1wj8ywd5zMs_ub2ugOxNEulRm09bF60BlYIjE44zUvTg"


def test_schedule_weekday_locked_to_monday():
    # X6 (LOCKED 2026-10-06): the three reports run every Monday.
    # Suggested ~8:00 AM ET is proposed-but-unconfirmed; no timer installed.
    assert config.SCHEDULE_WEEKDAY == "Monday"


def test_dry_run_is_still_default():
    # X1 lock keeps the default mode: zero sheet writes unless --write.
    import inspect
    sig = inspect.signature(runner.run_report)
    assert sig.parameters["write"].default is False
    assert sig.parameters["sheet_id"].default == config.SHEET_ID
