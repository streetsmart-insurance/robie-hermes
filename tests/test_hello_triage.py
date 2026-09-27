"""Tests for robie_job_engine/hello_triage.py — fake clients, no network, no secrets."""

from robie_job_engine.hello_triage import (
    CARRIER_NOTICE,
    JUNK,
    NEEDS_REVIEW,
    SURVEY,
    VENDOR,
    HelloTriage,
    classify_message,
    draft_note,
    extract_notice_fields,
    name_discussion,
    notice_type_of,
    resolve_applicant_id,
)


def meta(**kw):
    base = {"id": "m1", "from": "", "to": "", "subject": "", "date": "Mon, 21 Sep 2026",
            "snippet": "", "labels": []}
    if "from_" in kw:  # `from` is a keyword; callers pass from_=
        kw["from"] = kw.pop("from_")
    base.update(kw)
    return base


class FakeGmail:
    def __init__(self, metas, bodies=None):
        self._metas = metas
        self._bodies = bodies or {}

    def search_meta(self, query, max_results=25):
        assert "is:unread" in query
        return self._metas[:max_results]

    def get_body(self, msg_id):
        return self._bodies.get(msg_id)


class FakePolicySearch:
    def __init__(self, rows_by_number=None, error=None):
        self._rows = rows_by_number or {}
        self._error = error

    def search_policy_by_number(self, number):
        if self._error:
            raise self._error
        return {"status": "success", "data": self._rows.get(number, [])}


class FakeDiscussions:
    def __init__(self, by_applicant=None, error=None):
        self._by_app = by_applicant or {}
        self._error = error

    def get_discussions(self, applicant_id):
        if self._error:
            raise self._error
        return self._by_app.get(applicant_id, [])


def disc(did, title, n=3):
    return {"discussionId": did, "discussionTitle": title, "title": title,
            "noteCount": n, "mostRecentNoteId": "9", "lastModified": "2026-09-21T10:00:00Z"}


# --- classification -------------------------------------------------------

def test_classify_bare_policy_subject_from_carrier():
    cat, reason = classify_message(meta(
        from_="Sabrina Morgan <smorgan@fmiweb.com>",
        subject="4246005 MICHAEL KLOBY",
        snippet="Good afternoon, we are reviewing the self-inspection."))
    assert cat == "carrier_notice", reason


def test_classify_carrier_notice_cancellation():
    cat, reason = classify_message(meta(
        from_="RPS <RPS.Standardexpress@rpsins.com>",
        subject="Gasper Ferrito Dwelling Fire Policy DPNY2014090085 Customer Notification",
        snippet="cancellation effective 10/01/2026"))
    # subject has policy + carrier domain; snippet carries the notice keyword
    assert cat == CARRIER_NOTICE, (cat, reason)


def test_classify_carrier_notice_loss_runs():
    cat, _ = classify_message(meta(
        from_="lisa@tuscano.com",
        subject="Loss Run Request Pirin Construction LLC Pol# L259004982-1"))
    assert cat == CARRIER_NOTICE


def test_classify_vendor_webinar_from_carrier():
    cat, _ = classify_message(meta(
        from_="dantefrazier@geico.com",
        subject="New Jersey!!-GEICO Webinar-September 28"))
    assert cat == VENDOR


def test_classify_vendor_quote_blast():
    cat, _ = classify_message(meta(
        from_="hello@coverwhale.com",
        subject="Quoted: LADHAR TRANSPORT LLC (Trucking #261956222815)"))
    assert cat == VENDOR


def test_classify_survey():
    cat, _ = classify_message(meta(
        from_="noreply@survey.example.com",
        subject="How did we do? Take our 2-minute survey"))
    assert cat == SURVEY


def test_classify_junk_bounce():
    cat, _ = classify_message(meta(
        from_="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        subject="Delivery Status Notification (Failure)"))
    assert cat == JUNK


def test_classify_internal_mail_needs_review():
    cat, reason = classify_message(meta(
        from_="Mike Sosa <Mike@streetsmart.insurance>",
        subject="Fwd: A refund to your customer, Willie Elliott DBA ETS Trucking"))
    assert cat == NEEDS_REVIEW, (cat, reason)


def test_classify_ambiguous_needs_review():
    cat, _ = classify_message(meta(
        from_="someone@random-biz.com",
        subject="Quick question about Tuesday"))
    assert cat == NEEDS_REVIEW


# --- extraction -----------------------------------------------------------

def test_notice_type_rules():
    assert notice_type_of("Notice of Cancellation") == "cancellation"
    assert notice_type_of("Policy Non-Renewal") == "non-renewal"
    assert notice_type_of("Reinstatement confirmation") == "reinstatement"
    assert notice_type_of("Premium audit notice") == "audit"
    assert notice_type_of("Endorsement issued") == "endorsement"
    assert notice_type_of("Loss runs for X Pol# 1") == "loss_runs"
    assert notice_type_of("Your renewal documents") == "renewal"
    assert notice_type_of("Something vague") == "policy_activity"


def test_extract_fields_bare_policy_subject():
    f = extract_notice_fields(meta(
        from_="Sabrina Morgan <smorgan@fmiweb.com>",
        subject="4246005 MICHAEL KLOBY"))
    assert f["policy_number"] == "4246005"
    assert f["insured"] == "MICHAEL KLOBY"
    assert f["carrier"] == "Franklin Mutual"


def test_extract_fields_never_captures_the_word_number():
    f = extract_notice_fields(meta(
        from_="x@rpsins.com",
        subject="Your policy notice",
        snippet="Please see your Policy Number below."))
    assert f["policy_number"] is None
    f = extract_notice_fields(meta(
        from_="lisa@tuscano.com",
        subject="Loss Run Request Pirin Construction LLC Pol# L259004982-1"))
    assert f["carrier"] == "Tuscano"
    assert f["insured"] == "Pirin Construction LLC", f
    assert f["policy_number"] == "L259004982-1", f
    assert f["notice_type"] == "loss_runs"


def test_extract_fields_missing_insured_stays_none():
    f = extract_notice_fields(meta(
        from_="x@rpsins.com", subject="Cancellation notice"))
    assert f["insured"] is None
    assert f["policy_number"] is None


# --- applicant resolution (fail closed) -------------------------------------

def test_resolve_exactly_one_applicant():
    ps = FakePolicySearch({"L259004982-1": [{"policyId": "1", "applicantId": "182209499"}]})
    aid, skip = resolve_applicant_id(ps, "L259004982-1")
    assert aid == "182209499" and skip is None


def test_resolve_paged_results_shape():
    # Real PolicyApi wraps rows in {"results": [...]} and carries `accountId`.
    ps = FakePolicySearch({
        "L259004982-1": {"pageIndex": 1, "pageSize": 30, "totalSize": 1,
                         "results": [{"policyId": 64957207,
                                      "accountId": 44290430,
                                      "policyNumber": "L259004982-1"}]}
    })
    aid, skip = resolve_applicant_id(ps, "L259004982-1")
    assert aid == "44290430" and skip is None


def test_resolve_no_policy_number():
    aid, skip = resolve_applicant_id(FakePolicySearch(), None)
    assert aid is None and "no policy number" in skip


def test_resolve_policy_not_found():
    aid, skip = resolve_applicant_id(FakePolicySearch(), "NOPE123")
    assert aid is None and "not found" in skip


def test_resolve_two_applicants_refuses():
    ps = FakePolicySearch({"P1": [{"applicantId": "1"}, {"applicantId": "2"}]})
    aid, skip = resolve_applicant_id(ps, "P1")
    assert aid is None and "refusing to guess" in skip


def test_resolve_api_error_fails_closed():
    ps = FakePolicySearch(error=RuntimeError("boom"))
    aid, skip = resolve_applicant_id(ps, "P1")
    assert aid is None and "refusing to guess" in skip


# --- discussion naming (fail closed) ----------------------------------------

def test_name_discussion_single_titled():
    dl = FakeDiscussions({"182": [disc("d1", "Cancellation notice - Pirin")]})
    did, title, skip = name_discussion(dl, "182", "cancellation")
    assert (did, title, skip) == ("d1", "Cancellation notice - Pirin", None)


def test_name_discussion_hint_disambiguates():
    dl = FakeDiscussions({"182": [disc("d1", "Renewal docs"), disc("d2", "Cancellation notice")]})
    did, title, skip = name_discussion(dl, "182", "cancellation")
    assert did == "d2" and skip is None


def test_name_discussion_ambiguous_skips():
    dl = FakeDiscussions({"182": [disc("d1", "General"), disc("d2", "Other")]})
    did, title, skip = name_discussion(dl, "182", "cancellation")
    assert did is None and skip is not None


def test_name_discussion_no_applicant():
    did, title, skip = name_discussion(FakeDiscussions(), None, "cancellation")
    assert did is None and "no applicant" in skip


# --- note drafting ------------------------------------------------------------

def test_draft_note_cancellation_flagged():
    note, flags = draft_note({
        "notice_type": "cancellation", "carrier": "RPS",
        "insured": "Gasper Ferrito", "policy_number": "DPN-12345",
        "email_date": "Mon, 21 Sep 2026"})
    assert "Cancellation" in note and "DPN-12345" in note
    assert "followup_task" in flags
    assert "follow-up task" in note


def test_draft_note_phone_like_policy_withheld():
    # A 10-digit run inside a policy number is dialable-looking: the agency's
    # call automation would dial it, so the guard must keep it out of the note.
    note, flags = draft_note({
        "notice_type": "cancellation", "carrier": "RPS",
        "insured": "Gasper Ferrito", "policy_number": "DPNY2014090085",
        "email_date": "Mon, 21 Sep 2026"})
    assert "DPNY2014090085" not in note
    assert "policy_withheld_phone_guard" in flags


def test_draft_note_missing_fields_plain():
    note, flags = draft_note({
        "notice_type": "policy_activity", "carrier": None,
        "insured": None, "policy_number": None, "email_date": None})
    assert "not identified" in note and "not shown" in note
    assert flags == []


# --- full run -----------------------------------------------------------------

def test_full_run_evidence_pack_shape():
    gm = FakeGmail([
        meta(id="a1", from_="lisa@tuscano.com",
             subject="Loss Run Request Pirin Construction LLC Pol# L259004982-1",
             snippet="please send loss runs"),
        meta(id="b2", from_="dantefrazier@geico.com", subject="GEICO Webinar Sept 28"),
        meta(id="c3", from_="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
             subject="Delivery Status Notification (Failure)"),
        meta(id="d4", from_="Mike Sosa <Mike@streetsmart.insurance>",
             subject="Fwd: refund status"),
    ])
    ps = FakePolicySearch({"L259004982-1": [{"applicantId": "182209499"}]})
    dl = FakeDiscussions({"182209499": [disc("d9", "Loss runs - Pirin")]})
    pack = HelloTriage(gm, ps, dl).run(mailbox="hello@streetsmart.insurance")

    assert pack["mailbox"] == "hello@streetsmart.insurance"
    assert pack["phase"] == "phase1-dryrun-readonly"
    assert pack["n"] == 4
    counts = pack["summary"]["classification_counts"]
    assert counts == {"carrier_notice": 1, "vendor": 1, "junk": 1, "needs_review": 1}, counts
    assert pack["summary"]["applicant_match_rate"] == 1.0

    notice = pack["items"][0]
    assert notice["applicant_id"] == "182209499"
    assert notice["discussion_id"] == "d9"
    assert notice["discussion_title"] == "Loss runs - Pirin"
    assert notice["draft_note"] and "L259004982-1" in notice["draft_note"]
    assert notice["skip_reason"] is None

    review = pack["items"][3]
    assert review["classification"] == NEEDS_REVIEW
    assert review["skip_reason"] is not None
    assert review["applicant_id"] is None


def test_full_run_unresolved_applicant_recorded():
    gm = FakeGmail([meta(id="a1", from_="lisa@tuscano.com",
                          subject="Loss Run Request Pirin Construction LLC Pol# L259004982-1")])
    pack = HelloTriage(gm, FakePolicySearch(), FakeDiscussions()).run()
    assert pack["items"][0]["applicant_id"] is None
    assert pack["items"][0]["skip_reason"] is not None
    assert pack["unmatched_gmail_ids"] == ["a1"]
    assert pack["summary"]["applicant_match_rate"] == 0.0


def test_run_respects_max_items():
    gm = FakeGmail([meta(id=f"m{i}", subject="x") for i in range(10)])
    pack = HelloTriage(gm).run(max_items=3)
    assert pack["n"] == 3
