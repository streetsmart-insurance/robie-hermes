"""robie-filer: matcher, renderer, dec-page summary, and the filing loop.

All data is synthetic. No Gmail, Sheets, or EZLynx call is made.
"""

from __future__ import annotations

import io
from base64 import urlsafe_b64encode
from pathlib import Path

import pypdf
import pytest

from robie_job_engine import robie_filer as rf
from robie_job_engine import robie_filer_match as m
from robie_job_engine.robie_filer_extract import summarize_pages
from robie_job_engine.robie_filer_render import attachment_parts, header, render_email_pdf

CLIENT = "220250093"
OTHER = "330000001"


def b64(text: str | bytes) -> str:
    raw = text.encode() if isinstance(text, str) else text
    return urlsafe_b64encode(raw).decode()


def make_message(mid="m1", *, frm="Client <client@example.com>", to="robie@streetsmart.insurance", cc="",
                 subject="Renewal docs", body="Hello", thread="t1", attachments=(), extra_headers=()):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Date", "value": "Thu, 08 Oct 2026 17:19:00 -0400"}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    headers.extend({"name": n, "value": v} for n, v in extra_headers)
    parts = [{"mimeType": "text/plain", "body": {"data": b64(body)}}]
    for name, mime, data in attachments:
        parts.append({"filename": name, "mimeType": mime, "body": {"data": b64(data), "size": len(data)},
                      "headers": [{"name": "Content-Disposition", "value": f'attachment; filename="{name}"'}]})
    return {"id": mid, "threadId": thread, "internalDate": "1791494340000",
            "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}


def index(**kw) -> m.BookIndex:
    idx = m.BookIndex()
    idx.by_policy.update(kw.get("policies", {}))
    idx.by_email.update(kw.get("emails", {}))
    idx.names.update({CLIENT: "ROBIE TEST LLC", OTHER: "OTHER CLIENT INC"})
    return idx


def decide(msg, idx=None, lookup=None, thread=()):
    from robie_job_engine.robie_filer_render import body_text

    return m.match_message(msg, header=header, body=body_text(msg), index=idx or index(),
                           policy_lookup=lookup, thread_applicants=thread)


# ------------------------------------------------------------- matcher
def test_policy_number_in_index_matches_one_client():
    d = decide(make_message(subject="Re: Example Co - XYZ9876543 pending"),
               index(policies={"XYZ9876543": {CLIENT}}))
    assert (d.outcome, d.applicant_id, d.how, d.policy_number) == (m.MATCHED, CLIENT, "policy #", "XYZ9876543")


def test_policy_lookup_fills_master_id_and_confirms():
    d = decide(make_message(subject="Policy Q 1234567 renewal"), index(),
               lookup=lambda n: [(CLIENT, "83669533")] if n == "Q 1234567" else [])
    assert (d.outcome, d.applicant_id, d.policy_master_id) == (m.MATCHED, CLIENT, "83669533")


def test_two_clients_from_policy_numbers_needs_review():
    d = decide(make_message(body="AB1234567 and CD7654321"),
               index(policies={"AB1234567": {CLIENT}, "CD7654321": {OTHER}}))
    assert d.outcome == m.REVIEW and sorted(d.candidates) == sorted([CLIENT, OTHER])


def test_contact_email_matches_on_sender_and_on_sent_recipient():
    idx = index(emails={"client@example.com": {CLIENT}})
    assert decide(make_message(), idx).applicant_id == CLIENT
    sent = make_message(frm="Robie <robie@streetsmart.insurance>", to="client@example.com")
    assert decide(sent, idx).how == "contact client@example.com"


def test_shared_email_is_ambiguous():
    d = decide(make_message(), index(emails={"client@example.com": {CLIENT, OTHER}}))
    assert d.outcome == m.REVIEW


def test_thread_match_and_conflicting_thread():
    assert decide(make_message(frm="x@unknown.com"), thread=[CLIENT]).how == "same thread"
    assert decide(make_message(frm="x@unknown.com"), thread=[CLIENT, OTHER]).outcome == m.REVIEW
    assert decide(make_message(frm="x@unknown.com")).outcome == m.REVIEW


@pytest.mark.parametrize("msg,reason", [
    (make_message(frm="Applied Reporting <DoNotReply@appliedsystems.com>"), "Applied Reporting report"),
    (make_message(frm="a@streetsmart.insurance", to="b@streetsmart.insurance"), "staff-only"),
    (make_message(subject="Automatic reply: STREETSMART Submission"), "auto reply"),
    (make_message(frm="Zapier <alerts@mail.zapier.com>"), "automated alert"),
])
def test_skip_rules(msg, reason):
    d = decide(msg)
    assert (d.outcome, d.reason) == (m.SKIPPED, reason)


def test_carlo_forward_matches_original_sender():
    fwd = make_message(frm="carlo@streetsmart.insurance", to="robie@streetsmart.insurance",
                       body="---------- Forwarded message ---------\nFrom: Client <client@example.com>\nHi")
    d = decide(fwd, index(emails={"client@example.com": {CLIENT}}))
    assert (d.outcome, d.applicant_id) == (m.MATCHED, CLIENT)


def test_policy_candidates_drop_phone_and_state_zip():
    found = m.policy_candidates("Call 732-555-1234, NJ 07728, policy XYZ9876543")
    assert found == ["XYZ9876543"]


def test_load_book_index_reads_both_emails_and_policies(tmp_path):
    csv_path = tmp_path / "index.csv"
    csv_path.write_text(
        "applicant_id,account_name,email_primary,email_business,policy_numbers\n"
        f"{CLIENT},ROBIE TEST LLC,a@x.com,b@x.com,XYZ 9876543;Q 1234567\n"
        f"{OTHER},OTHER,a@x.com,,\n"
    )
    idx = m.load_book_index(str(csv_path))
    assert idx.by_email["a@x.com"] == {CLIENT, OTHER}
    assert idx.by_policy["XYZ9876543"] == {CLIENT}
    assert idx.name(CLIENT) == "ROBIE TEST LLC"


# ----------------------------------------------------------- rendering
def test_email_pdf_is_valid_and_lists_headers_and_attachments():
    msg = make_message(subject="Dec page (final)", body="Line one\n" + "word " * 400,
                       attachments=[("dec.pdf", "application/pdf", b"%PDF-1.4 x")])
    reader = pypdf.PdfReader(io.BytesIO(render_email_pdf(msg)))
    text = "".join(page.extract_text() for page in reader.pages)
    assert "Subject: Dec page (final)" in text
    assert "Attachments: dec.pdf" in text


def test_small_inline_images_are_not_attachments():
    msg = make_message(attachments=[("logo.png", "image/png", b"x" * 100), ("dec.pdf", "application/pdf", b"x")])
    msg["payload"]["parts"][1]["headers"] = [{"name": "Content-Disposition", "value": "inline"}]
    assert [p["filename"] for p in attachment_parts(msg)] == ["dec.pdf"]


# ------------------------------------------------------- dec summary
def test_summary_finds_fields_with_page_numbers():
    pages = [
        "Cover letter\n",
        "Policy No Issued To\nQ 1234567 EXAMPLE LLC\nPeriod Transaction Type\n10/27/2026  10/27/2027 RENEWAL\n",
        "COMMON DECLARATION\nNamed Insured and Address\nEXAMPLE LLC\n12 MAIN ST\n"
        "PAYMENT METHOD Total Policy Premium         $2,965.00\n",
        "SCHEDULE OF EQUIPMENT\n    1 2022 CHIPPER 12      $80,000\n    2 2019 LOADER 5      $28,000\n"
        "ALL COVERED PROPERTY AT ALL LOCATIONS    $108,000\nDEDUCTIBLE   $1,000\n",
        "LOSS PAYEE SCHEDULE\nTHIS ENDORSEMENT CHANGES THE POLICY. PLEASE READ IT CAREFULLY.\nSCHEDULE\n"
        "Name of Person or Organization\nACME FINANCIAL SERVICES PO BOX 1\n",
    ]
    s = summarize_pages(pages)
    assert (s.policy_number[0].value, s.policy_number[0].page) == ("Q 1234567", 2)
    assert (s.term[0].value, s.term[0].page) == ("10/27/2026 to 10/27/2027", 2)
    assert (s.named_insured[0].value, s.named_insured[0].page) == ("EXAMPLE LLC", 3)
    assert (s.total_premium[0].value, s.total_premium[0].page) == ("$2,965.00", 3)
    assert [i.value for i in s.scheduled_items] == ["1. 2022 CHIPPER 12 $80,000", "2. 2019 LOADER 5 $28,000"]
    assert s.deductibles[0].page == 4
    assert [p.value for p in s.loss_payees_additional_insureds] == ["ACME FINANCIAL SERVICES PO BOX 1"]


# ------------------------------------------------------------- filer loop
class FakeSheet:
    def __init__(self, queue=(), review=()):
        self.queue = [list(r) for r in queue]
        self.review = [list(r) for r in review]
        self.queue_results: dict[int, tuple] = {}
        self.review_appended: list[list[str]] = []
        self.review_resolved: dict[int, str] = {}
        self.filed: list[list[str]] = []

    def ensure_layout(self):
        pass

    def queue_rows(self):
        return [(i + 2, dict(zip(rf.QUEUE_HEADERS, r + [""] * (11 - len(r))))) for i, r in enumerate(self.queue)
                if (i + 2) not in self.queue_results]

    def update_queue_result(self, row, status, ids, error):
        self.queue_results[row] = (status, ids, error)

    def review_rows(self):
        return [(i + 2, dict(zip(rf.REVIEW_HEADERS, r + [""] * (9 - len(r))))) for i, r in enumerate(self.review)
                if (i + 2) not in self.review_resolved]

    def append_review(self, values):
        self.review_appended.append(values)

    def update_review_resolution(self, row, text):
        self.review_resolved[row] = text

    def append_filed(self, values):
        self.filed.append(values)


class FakeMailbox:
    def __init__(self, *messages):
        self.messages = {msg["id"]: msg for msg in messages}

    def message(self, mid):
        return self.messages[mid]

    def attachment_bytes(self, mid, part):
        from base64 import urlsafe_b64decode

        return urlsafe_b64decode(part["body"]["data"])

    def new_message_ids(self, after, limit):
        return list(self.messages)


class FakeEzlynx:
    def __init__(self, fail=None, existing=None):
        self.uploads: list[tuple] = []
        self.notes: list[tuple] = []
        self.fail = fail
        self.existing = existing or {}

    def upload(self, applicant, name, data, *, policy_master_id, content_type):
        if self.fail:
            raise self.fail
        self.uploads.append((applicant, name, policy_master_id, content_type))
        return str(900000 + len(self.uploads))

    def find_document(self, applicant, name):
        return self.existing.get((applicant, name), "")

    def add_note(self, applicant, discussion, text):
        self.notes.append((applicant, discussion, text))
        return "1"

    def policy_lookup(self, number):
        return []


def make_filer(tmp_path, *, sheet=None, mailbox=None, ezlynx=None, live=True, auto=False, idx=None):
    return rf.Filer(sheet=sheet or FakeSheet(), mailbox=mailbox or FakeMailbox(), ezlynx=ezlynx or FakeEzlynx(),
                    ledger=rf.Ledger(tmp_path / "ledger.db"), index=idx or index(), state_dir=tmp_path,
                    options=rf.Options(live=live, auto=auto))


DEC = make_message("m1", attachments=[("dec.txt", "text/plain", b"dec page"), ("other.txt", "text/plain", b"x")])


def test_queue_attachment_filed_once_with_id_and_note(tmp_path):
    sheet = FakeSheet(queue=[["ATTACHMENT", "m1", "dec.txt", CLIENT, "83669533", "Dec Page 2026", "", "849932992"]])
    ez = FakeEzlynx()
    filer = make_filer(tmp_path, sheet=sheet, mailbox=FakeMailbox(DEC), ezlynx=ez)
    filer.run()
    assert ez.uploads == [(CLIENT, "Dec Page 2026", "83669533", "text/plain")]
    assert sheet.queue_results[2] == ("Filed", "900001", "")
    assert ez.notes == [(CLIENT, "849932992", "Saved to Documents: Dec Page 2026")]
    # Same row reset to blank: the ledger keeps it from uploading again.
    sheet.queue_results.clear()
    make_filer(tmp_path, sheet=sheet, mailbox=FakeMailbox(DEC), ezlynx=ez).run()
    assert len(ez.uploads) == 1


def test_queue_email_files_pdf_then_attachments(tmp_path):
    sheet = FakeSheet(queue=[["EMAIL", "m1", "", CLIENT]])
    ez = FakeEzlynx()
    make_filer(tmp_path, sheet=sheet, mailbox=FakeMailbox(DEC), ezlynx=ez).run()
    assert [u[1] for u in ez.uploads] == ["Email 2026-10-08 - Renewal docs.pdf", "dec.txt", "other.txt"]
    assert sheet.queue_results[2][1] == "900001, 900002, 900003"


def test_queue_errors_are_written_exactly(tmp_path):
    sheet = FakeSheet(queue=[
        ["ATTACHMENT", "m1", "missing.pdf", CLIENT],
        ["ATTACHMENT", "m1", "dec.txt", "not-a-number"],
        ["FAX", "m1", "", CLIENT],
    ])
    make_filer(tmp_path, sheet=sheet, mailbox=FakeMailbox(DEC)).run()
    assert sheet.queue_results[2][0] == "Error" and "missing.pdf' found 0 times" in sheet.queue_results[2][2]
    assert "applicant_id must be" in sheet.queue_results[3][2]
    assert "type must be ATTACHMENT or EMAIL" in sheet.queue_results[4][2]


def test_write_gate_refusal_is_reported_not_hidden(tmp_path):
    from robie_job_engine.ezlynx_write_scope import EzlynxWriteScopeError

    sheet = FakeSheet(queue=[["ATTACHMENT", "m1", "dec.txt", OTHER]])
    ez = FakeEzlynx(fail=EzlynxWriteScopeError("EZLYNX_WRITE_SCOPE_REFUSED: applicant 330000001"))
    make_filer(tmp_path, sheet=sheet, mailbox=FakeMailbox(DEC), ezlynx=ez).run()
    status, ids, error = sheet.queue_results[2]
    assert (status, ids) == ("Error", "") and "EZLYNX_WRITE_SCOPE_REFUSED" in error


def test_unconfirmed_upload_is_adopted_not_repeated(tmp_path):
    filer = make_filer(tmp_path, mailbox=FakeMailbox(DEC))
    filer.ledger.reserve_document("m1:att:dec.txt", "m1", CLIENT, "dec.txt")
    ez = FakeEzlynx(existing={(CLIENT, "dec.txt"): "777"})
    sheet = FakeSheet(queue=[["ATTACHMENT", "m1", "dec.txt", CLIENT]])
    make_filer(tmp_path, sheet=sheet, mailbox=FakeMailbox(DEC), ezlynx=ez).run()
    assert ez.uploads == [] and sheet.queue_results[2][:2] == ("Filed", "777")


def test_dry_run_writes_nothing(tmp_path):
    sheet = FakeSheet(queue=[["EMAIL", "m1", "", CLIENT, "", "", "", "849932992"]])
    ez = FakeEzlynx()
    filer = make_filer(tmp_path, sheet=sheet, mailbox=FakeMailbox(DEC), ezlynx=ez, live=False, auto=True)
    report = filer.run()
    assert ez.uploads == [] and ez.notes == [] and sheet.queue_results == {} and sheet.review_appended == []
    assert any("would_file" in item for item in report)


def test_auto_files_matched_reviews_unmatched_skips_reports(tmp_path):
    matched = make_message("a1", thread="t1", attachments=[("dec.txt", "text/plain", b"d")])
    follow_up = make_message("a2", frm="someone@unknown.com", thread="t1")
    unknown = make_message("a3", frm="stranger@unknown.com", thread="t9")
    report = make_message("a4", frm="DoNotReply@appliedsystems.com")
    sheet, ez = FakeSheet(), FakeEzlynx()
    filer = make_filer(tmp_path, sheet=sheet, mailbox=FakeMailbox(matched, follow_up, unknown, report), ezlynx=ez,
                       auto=True, idx=index(emails={"client@example.com": {CLIENT}}))
    filer.run()
    assert [u[0] for u in ez.uploads] == [CLIENT, CLIENT, CLIENT]  # a1 email + attachment, a2 email via thread
    assert [r[3] for r in sheet.review_appended] == ["a3"]
    assert filer.ledger.message_outcome("a4") == "SKIPPED"
    assert {row[5] for row in sheet.filed} == {"contact client@example.com", "same thread"}


def test_review_resolution_files_to_named_client(tmp_path):
    sheet = FakeSheet(review=[["d", "f", "s", "m1", "t1", "reason", "", CLIENT, ""]])
    ez = FakeEzlynx()
    make_filer(tmp_path, sheet=sheet, mailbox=FakeMailbox(DEC), ezlynx=ez).run()
    assert len(ez.uploads) == 3 and sheet.review_resolved[2].startswith("Filed 900001, 900002, 900003")


def test_note_text_drops_long_numbers():
    assert rf.note_text("Dec 732-555-1234 Q 1234567") == "Saved to Documents: Dec # Q 1234567"


def test_live_auto_requires_any_applicant():
    with pytest.raises(SystemExit):
        rf.main(["--live", "--auto"])


def test_any_applicant_refuses_without_a_write_scope_policy(monkeypatch, tmp_path):
    from robie_job_engine import ezlynx_write_scope

    monkeypatch.setattr(ezlynx_write_scope, "WRITE_SCOPE_POLICY_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(ezlynx_write_scope, "_OPERATION_SCOPE", None)
    with pytest.raises(SystemExit, match="write-scope policy"):
        rf._register_any_applicant_scope()
    assert ezlynx_write_scope.operation_scope_registered() is False
