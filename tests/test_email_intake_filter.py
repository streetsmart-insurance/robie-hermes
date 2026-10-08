"""Email intake filter: only real requests become jobs (Carlo 2026-10-08).

Fixtures are shaped from real Prod robie@ mail of Oct 5-8, 2026 (subjects,
headers, first lines). Client names, policy numbers and people outside the
agency are scrubbed.
"""
from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from robie_job_engine.email_intake_filter import (
    GMAIL_REACTION_MIME,
    InboundEmail,
    classify_inbound,
    fingerprint,
    has_ask,
    new_text,
    payload_mime_types,
    reply_cc_for,
)
from robie_job_engine.end_state_report import _display_ask, summary_sentence

ROOT = Path(__file__).resolve().parents[1]
ROBIE = "robie@streetsmart.insurance"


def mail(
    *,
    subject: str,
    body: str,
    sender: str = "carlo@streetsmart.insurance",
    to: str = ROBIE,
    cc: str = "",
    extra_headers: dict | None = None,
    mime: tuple[str, ...] = (),
    msg_id: str = "m1",
    thread_id: str = "t1",
    attachments: tuple[str, ...] = (),
) -> InboundEmail:
    headers = {"to": to, "cc": cc, "message-id": f"<{msg_id}@mail.gmail.com>"}
    headers.update(extra_headers or {})
    return InboundEmail(
        gmail_message_id=msg_id,
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        body=body,
        headers=headers,
        mime_types=mime,
        attachment_names=attachments,
    )


ZAPIER_FWD = (
    "---------- Forwarded message ---------\n"
    "From: Zapier Alerts <alerts@mail.zapier.com>\n"
    "Date: Tue, 06 Oct 2026 04:59\n"
    "Subject: [ALERT] Possible error on your Ramsey email to EZLynx Zap\n"
    "To: <carlo@streetsmart.insurance>\n\nYour Zap hit an error."
)
CARRIER_FWD = (
    "---------- Forwarded message ---------\n"
    "From: Audit Desk <audit.desk@carrier.example>\n"
    "Date: Wed, Oct 7, 2026 at 9:41 AM\n"
    "Subject: Premium audit status request - WC policy WC 0000001 (ACME Construction)\n"
    "To: robie@streetsmart.insurance\n\n"
    "Please send the completed audit form for the expired term."
)
REACTION_BODY = (
    "\U0001F44D\n\nCarlo Ferrara reacted via Gmail\n"
    "<https://www.google.com/gmail/about/?utm_source=gmail-in-product&utm_campaign=emojireactionemail#app>\n\n"
    "On Wed, Oct 7, 2026 at 9:47 PM Client Person <client@example.com> wrote:\n\n> I am getting this together."
)


class JunkIsSkipped(unittest.TestCase):
    def assertSkip(self, email, reason):
        decision = classify_inbound(email)
        self.assertFalse(decision.keep, decision)
        self.assertEqual(decision.reason, reason, decision)
        return decision

    def test_gmail_reaction_by_text_robie_on_cc(self):
        # Prod 2026-10-07 22:01 ET: Carlo's thumbs-up on the audit-letter thread
        # became a job and mailed "did not finish" to Carlo and Erika.
        email = mail(
            subject="Re: Audit Request Letter Needed - ACME Construction - WC 0000001",
            body=REACTION_BODY,
            to="Client Person <client@example.com>",
            cc="Robie AI <robie@streetsmart.insurance>, Erika Palacios <erika@streetsmart.insurance>",
        )
        self.assertSkip(email, "gmail_reaction")

    def test_gmail_reaction_by_mime_part_even_addressed_to_robie(self):
        email = mail(subject="Re: anything", body="\U0001F44D", mime=("multipart/related", GMAIL_REACTION_MIME))
        self.assertSkip(email, "gmail_reaction")

    def test_mime_walk_finds_reaction_part(self):
        payload = {"mimeType": "multipart/related", "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain"}, {"mimeType": GMAIL_REACTION_MIME}, {"mimeType": "text/html"}]}]}
        self.assertIn(GMAIL_REACTION_MIME, payload_mime_types(payload))

    def test_forwarded_zapier_alerts(self):
        for subject in (
            "Fwd: [ALERT] Possible error on your Ramsey email to EZLynx Zap",
            "Fwd: \u274c One of your Zaps has been paused",
            "Fwd: Action Required: Out of Tasks!",
            "Fwd: 2962 held Tasks are still waiting to be replayed",
        ):
            self.assertSkip(mail(subject=subject, body=ZAPIER_FWD), "report_or_alert")

    def test_forward_of_automated_sender_with_plain_subject(self):
        self.assertSkip(mail(subject="Fwd: heads up", body=ZAPIER_FWD), "forwarded_automated_alert")

    def test_manual_renewal_report(self):
        body = "Manual Renewal Report \u2014 2026-10-07\nActive renewals: 112\nExpires: 2026-10-16 Account: ACME"
        self.assertSkip(mail(subject="Manual Renewal Report \u2014 2026-10-07 (112 active)", body=body), "report_or_alert")

    def test_newsletter_test_send(self):
        email = mail(
            subject="Test (Step 1): Columbus Day hours + deer season is here \u2014 watch the roads",
            body="Hi Jammie, HOLIDAY HOURS Monday, October 12 \u2014 Closed for Columbus Day. Please call us.",
        )
        self.assertSkip(email, "report_or_alert")

    def test_out_of_office(self):
        email = mail(
            sender="andrea@streetsmart.insurance",
            subject="Out of Office Re: Overdue policy change requests need an update",
            body="Hello, I will be out of the office from 10/06/2026 to 10/08/2026. Please call the office.",
        )
        self.assertSkip(email, "auto_reply")

    def test_auto_submitted_and_bulk_headers(self):
        self.assertSkip(mail(subject="Hi", body="please file this", extra_headers={"Auto-Submitted": "auto-generated"}), "auto_submitted")
        self.assertSkip(mail(subject="Hi", body="please file this", extra_headers={"Precedence": "bulk"}), "bulk_mail")

    def test_group_list_mail_not_addressed_to_robie(self):
        # "Unresolved Text Messages" goes to the StreetSmart@ group, not robie@.
        email = mail(
            sender="jake@streetsmart.insurance",
            subject="Unresolved Text Messages - October 2026",
            body="Hi, We have assigned unresolved text messages associated with the accounts below.",
            to="StreetSmart@streetsmart.insurance",
            cc="carlo@streetsmart.insurance",
            extra_headers={"List-Id": "<streetsmart.streetsmart.insurance>"},
        )
        self.assertSkip(email, "mailing_list")
        email = mail(
            sender="jake@streetsmart.insurance",
            subject="Unresolved Text Messages - October 2026",
            body="Hi, We have assigned unresolved text messages associated with the accounts below.",
            to="StreetSmart@streetsmart.insurance",
        )
        self.assertSkip(email, "robie_not_in_to")

    def test_google_chat_notice(self):
        email = mail(
            subject="Re: Personal Auto Policy F 0000000",
            body="Carlo Ferrara (carlo@streetsmart.insurance) started a conversation about this email in Google Chat Open chat",
            to="jake@streetsmart.insurance, robie@streetsmart.insurance",
        )
        self.assertSkip(email, "google_chat_notice")

    def test_robie_only_cc_is_fyi(self):
        email = mail(
            subject="Re: Corrected binder needed \u2014 ASP000000-01 (ACME Realty LLC) [EXTERNAL]",
            body="ACME Realty LLC is the correct named insured. We just need the address updated. Can you fix it?",
            to="underwriter@carrier.example",
            cc="robie@streetsmart.insurance",
        )
        self.assertSkip(email, "robie_not_in_to")

    def test_talking_about_robie_is_not_addressing_robie(self):
        email = mail(
            subject="Re: Bond 000 (Surety) \u2014 did we miss the cancellation notice?",
            body="Hey, what Robie is asking is: what happened with this cancellation? Did we miss it?",
            to="alejandro@streetsmart.insurance",
            cc="robie@streetsmart.insurance",
        )
        self.assertSkip(email, "robie_not_in_to")

    def test_reply_addressed_to_someone_else(self):
        for greeting in ("Hi Amanda, You should not receive any more of these emails. Thanks again!",
                         "Hello Joseph, sorry about this, this is an AI assistant, I already talked to the insured"):
            email = mail(subject="Re: change request form attached", body=greeting, cc="amanda@carrier.example")
            self.assertSkip(email, "addressed_to_someone_else")

    def test_answer_with_no_ask(self):
        email = mail(
            sender="alejandro@streetsmart.insurance",
            subject="Re: WOW: Was the August Google review paid?",
            body="I checked the August records and it was not paid there.\n[image: Kind regards,]\nAlejandro",
        )
        self.assertSkip(email, "no_ask")

    def test_bare_forward_with_other_recipients_is_skipped(self):
        email = mail(subject="Fwd: Premium audit status request", body=CARRIER_FWD,
                     to="robie@streetsmart.insurance, erika@streetsmart.insurance")
        self.assertSkip(email, "forward_no_note")

    def test_bare_forward_to_robie_alone_is_skipped(self):
        # Carlo 2026-10-08 6:45 AM: "skip forwards with no note". Prod Oct 6-7:
        # three premium-audit forwards from Carlo with no text above them.
        for subject in (
            "Fwd: Premium audit status request - WC policy WC 0000001 (ACME Construction)",
            "Fwd: [EXTERNAL] Premium audit status request - WC policy PWC0000000 (expired term)",
        ):
            email = mail(subject=subject, body=CARRIER_FWD, attachments=("audit.pdf",))
            decision = self.assertSkip(email, "forward_no_note")
            self.assertIn("audit.desk@carrier.example", decision.detail)

    def test_bare_forward_with_only_a_signature_is_skipped(self):
        email = mail(subject="Fwd: audit", body="Best Regards,\nCarlo\n\n" + CARRIER_FWD)
        self.assertSkip(email, "forward_no_note")

    def test_bare_forward_without_fwd_subject_is_skipped(self):
        self.assertSkip(mail(subject="Premium audit status request", body=CARRIER_FWD), "forward_no_note")


class RealRequestsAreKept(unittest.TestCase):
    def assertKeep(self, email, reason, db_path=None, **kw):
        decision = classify_inbound(email, db_path=db_path, **kw)
        self.assertTrue(decision.keep, decision)
        self.assertEqual(decision.reason, reason, decision)
        return decision

    def test_jake_pfa_email_still_queues(self):
        # Prod 2026-10-05: Jake's Neighborhood Express PFA request.
        email = mail(
            sender="jake@streetsmart.insurance",
            subject="NEIGHBORHOOD EXPRESS LLC",
            body="Please work up a finance agreement through Ascend for this insured, we only want the auto policy.",
            cc="carlo@streetsmart.insurance",
            attachments=("quote.pdf",),
        )
        self.assertKeep(email, "direct_ask")

    def test_forward_with_note_is_kept(self):
        # Real Prod notes from Oct 7: "file this" and
        # "please make sure this is taken care of", above a client email.
        fwd = CARRIER_FWD.replace("audit.desk@carrier.example", "client@example.com")
        self.assertKeep(mail(subject="Fwd: Action needed: carrier is waiting on your workers comp audit - Policy WC 0000002",
                             body="file this\n\n" + fwd), "direct_ask")
        self.assertKeep(mail(subject="Fwd: adding new location",
                             body="please make sure this is taken care of\n\n" + fwd), "direct_ask")

    def test_question_to_robie(self):
        email = mail(sender="sandeep@streetsmart.insurance",
                     subject="Re: Missed Call Report + Quarterly Incentive \u2014 status update",
                     body="What is the status update on the missed call report?\n\nBest Regards,\nSandeep\n\nOn Wed, Oct 7, 2026 at 2:00 AM Robie AI <robie@streetsmart.insurance> wrote:\n> old")
        self.assertKeep(email, "direct_ask")

    def test_robie_named_on_cc(self):
        email = mail(subject="Re: Corrected binder", body="Robie, please file the corrected binder.",
                     to="underwriter@carrier.example", cc="robie@streetsmart.insurance")
        self.assertKeep(email, "robie_named_with_ask")

    def test_hitl_reply_is_not_mistaken_for_a_report(self):
        email = mail(subject="Re: [ROBIE HITL] Job 1234abcd needs coverage amounts", body="A 300000 B 30000")
        self.assertKeep(email, "continuation")


class DedupeAndContinuation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "jobs.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, idempotency_key TEXT, action_type TEXT, payload_json TEXT, status TEXT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def add_job(self, job_id, gmail_id, payload, status="UNVERIFIED"):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (
            job_id, f"gmail:{gmail_id}", "hermes.email_task",
            json.dumps(payload, sort_keys=True, separators=(",", ":")), status))
        conn.commit()
        conn.close()

    def test_same_request_resent_on_thread_is_duplicate(self):
        body = "please file this\n\n" + CARRIER_FWD
        first = mail(subject="Fwd: Premium audit status request", body=body, msg_id="a", thread_id="T")
        d1 = classify_inbound(first, db_path=self.db)
        self.assertTrue(d1.keep)
        self.add_job("job-a", "a", {"gmail_thread_id": "T", "email_intake": d1.as_payload(),
                                    "request_text": f"Subject: {first.subject}\n\n{first.body}"})
        again = mail(subject="Fwd: Premium audit status request", body=body, msg_id="b", thread_id="T")
        d2 = classify_inbound(again, db_path=self.db)
        self.assertFalse(d2.keep)
        self.assertEqual(d2.reason, "duplicate")

    def test_old_job_without_intake_stamp_still_dedupes(self):
        body = "please file this\n\n" + CARRIER_FWD
        self.add_job("job-old", "old", {"gmail_thread_id": "T", "request_text": f"Subject: Fwd: audit\n\n{body}"})
        d = classify_inbound(mail(subject="Fwd: audit", body=body, msg_id="new", thread_id="T"), db_path=self.db)
        self.assertEqual(d.reason, "duplicate")

    def test_same_gmail_message_resumes_its_own_job(self):
        email = mail(subject="Fwd: audit", body="please file this", msg_id="a", thread_id="T")
        d = classify_inbound(email, db_path=self.db)
        self.add_job("job-a", "a", {"gmail_thread_id": "T", "email_intake": d.as_payload()}, status="RETRY_WAIT")
        self.assertTrue(classify_inbound(email, db_path=self.db).keep)

    def test_same_message_id_header_under_new_gmail_id(self):
        self.add_job("job-a", "a", {"gmail_thread_id": "OTHER", "email_intake": {"rfc822_message_id": "<m9@mail.gmail.com>"}})
        d = classify_inbound(mail(subject="x", body="please file this", msg_id="m9", thread_id="T2"), db_path=self.db)
        self.assertEqual(d.reason, "duplicate")

    def test_new_ask_on_same_thread_is_not_a_duplicate(self):
        self.add_job("job-a", "a", {"gmail_thread_id": "T", "request_text": "Subject: Re: x\n\nplease file this"})
        d = classify_inbound(mail(subject="Re: x", body="also update the address please", msg_id="b", thread_id="T"), db_path=self.db)
        self.assertTrue(d.keep)

    def test_bare_forward_on_waiting_thread_is_still_a_continuation(self):
        self.add_job("job-w", "w", {"gmail_thread_id": "T"}, status="NEEDS_CLARIFICATION")
        d = classify_inbound(mail(subject="Fwd: quote", body=CARRIER_FWD, msg_id="r", thread_id="T"), db_path=self.db)
        self.assertEqual(d.kind, "continuation")

    def test_answer_on_thread_robie_is_waiting_on(self):
        self.add_job("job-w", "w", {"gmail_thread_id": "T"}, status="AWAITING_HUMAN_INPUT")
        d = classify_inbound(mail(subject="Re: x", body="Terrorism included. Fee 150.", msg_id="r", thread_id="T"), db_path=self.db)
        self.assertTrue(d.keep)
        self.assertEqual(d.kind, "continuation")

    def test_reaction_on_waiting_thread_is_still_skipped(self):
        self.add_job("job-w", "w", {"gmail_thread_id": "T"}, status="AWAITING_HUMAN_INPUT")
        d = classify_inbound(mail(subject="Re: x", body=REACTION_BODY, msg_id="r", thread_id="T"), db_path=self.db)
        self.assertEqual(d.reason, "gmail_reaction")

    def test_open_ascend_session_is_continuation(self):
        d = classify_inbound(mail(subject="Re: PFA", body="Yes, 25% down.", thread_id="T"),
                             ascend_sessions={"T": {"closed": False}})
        self.assertEqual(d.kind, "continuation")


class ReplyRules(unittest.TestCase):
    def test_did_not_finish_goes_to_requester_only(self):
        cc = ["erika@streetsmart.insurance"]
        self.assertEqual(reply_cc_for("UNVERIFIED", "You asked Robie to x, and Robie did not finish it.", cc), [])
        self.assertEqual(reply_cc_for("FAILED", "Couldn't finish.", cc), [])
        self.assertEqual(reply_cc_for("", "anything", cc), [])
        self.assertEqual(reply_cc_for("COMPLETE", "Robie could not confirm how it ended.", cc), [])

    def test_finished_reply_keeps_cc(self):
        # 2026-10-02 rule: Jake stays on the PFA reply when the work is done.
        self.assertEqual(reply_cc_for("COMPLETE", "Done.", ["jake@streetsmart.insurance"]), ["jake@streetsmart.insurance"])

    def test_summary_uses_short_subject_not_subject_label(self):
        ask = _display_ask({"request_text": "Subject: Re: Fwd: Audit Request Letter Needed - ACME - WC 0000001\n\nBest Regards, Sandeep"})
        self.assertEqual(ask, "handle Audit Request Letter Needed - ACME - WC 0000001")
        line = summary_sentence(ask, "", "wrong")
        self.assertEqual(line, "You asked Robie to handle Audit Request Letter Needed - ACME - WC 0000001, and Robie did not finish it.")
        self.assertNotIn("Subject:", line)
        self.assertEqual(_display_ask({"request_text": "quote ACME LLC"}), "")


class TextHelpers(unittest.TestCase):
    def test_new_text_drops_quote_forward_and_signature(self):
        body = "Can you do it?\n\nBest Regards,\nSandeep\nOn Tue, Oct 6, 2026 at 7:39 PM Robie AI <robie@streetsmart.insurance> wrote:\n> please file"
        self.assertEqual(new_text(body), "Can you do it?")
        self.assertEqual(new_text(ZAPIER_FWD), "")

    def test_has_ask(self):
        self.assertTrue(has_ask("file this"))
        self.assertTrue(has_ask("This report is to be done every Monday."))
        self.assertFalse(has_ask("Please note that she is eligible for one bonus."))
        self.assertFalse(has_ask("Olenick auto - Is Active, Auto Pay is set up."))
        self.assertFalse(has_ask("Link: https://docs.google.com/x/edit?gid=1"))

    def test_fingerprint_ignores_prefixes_and_date_lines(self):
        a = fingerprint("Fwd: X", "hello\nDate: Tue\nbody")
        b = fingerprint("Fwd: Fwd: x", "hello\nDate: Wed\nbody")
        self.assertEqual(a, b)


class WatcherWiring(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "scripts/robie_email_agent.py").read_text(encoding="utf-8")

    def test_classifier_runs_before_download_and_queue(self):
        s = self.source
        self.assertLess(s.find("if not is_allowed_sender(sender)"), s.find("classify_inbound("))
        self.assertLess(s.find("classify_inbound("), s.find("attachments = download_attachments(service, msg_id, payload)"))
        self.assertLess(s.find("if not intake.keep:"), s.find("batch.append({"))
        self.assertIn("intake=item[\"intake\"].as_payload()", s)

    def test_skips_are_logged_and_not_reprocessed(self):
        s = self.source
        block = s[s.find("if not intake.keep:"): s.find("attachments = download_attachments(service, msg_id, payload)")]
        self.assertIn("processed_ids.add(msg_id)", block)
        self.assertIn("continue", block)
        self.assertNotIn("messages().send", block)
        self.assertIn("logger.info(intake.log_line(inbound))", s)

    def test_reply_cc_rule_is_applied(self):
        s = self.source
        self.assertLess(s.find("reply_cc_for(job_status, reply_body, cc_list)"), s.find("cc=cc_list,"))

    def test_guard_stores_intake_on_job_payload(self):
        guard = (ROOT / "robie_job_engine/email_guard.py").read_text(encoding="utf-8")
        tree = ast.parse(guard)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_guarded_email_task")
        self.assertIn("intake", [a.arg for a in fn.args.kwonlyargs])
        self.assertIn('payload["email_intake"] = dict(intake)', guard)


if __name__ == "__main__":
    unittest.main()
