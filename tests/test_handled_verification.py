"""Synthetic-only tests for handled verification (reply/forward/summary).

Fixtures use fictional people and example.com clients only - no real names,
addresses, or content.
"""

from datetime import datetime, timezone

import pytest

from robie_job_engine.gmail_accountability import GmailAccountabilityError
from robie_job_engine.handled_verification import (
    FYI_ACTION_FLAG,
    GMAIL_READONLY_SCOPE,
    clean_body,
    collect_handled_verification,
    is_related_send,
    norm_subject,
    summarize,
    thread_has_internal_reply,
    verification_window,
    verify_inbound_message,
    verify_mailbox_handled,
)


AS_OF = datetime(2026, 9, 29, 14, tzinfo=timezone.utc)
EMPLOYEE = "rep-a@streetsmart.insurance"
SHARED = "hello@streetsmart.insurance"


def _millis(value: datetime) -> str:
    return str(int(value.timestamp() * 1000))


def _msg(mid, thread, sender, when, subject="Test subject line", extra_headers=None, body=None,
         to_addr=None):
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": when.strftime("%a, %d %b %Y %H:%M:%S +0000")},
        {"name": "Message-ID", "value": f"<{mid}@mail.example.com>"},
        {"name": "To", "value": to_addr or EMPLOYEE},
    ]
    for name, value in (extra_headers or {}).items():
        headers.append({"name": name, "value": value})
    payload = {"headers": headers, "mimeType": "text/plain"}
    if body is not None:
        import base64
        payload["body"] = {"data": base64.urlsafe_b64encode(body.encode()).decode()}
    return {"id": mid, "threadId": thread, "internalDate": _millis(when), "payload": payload}


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Messages:
    def __init__(self, service):
        self.service = service

    def list(self, userId=None, q=None, maxResults=None, pageToken=None):
        return _Request({"messages": self.service.match(q)[: max_results_stub(maxResults)]})

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        return _Request(self.service.messages[id])


class _Threads:
    def __init__(self, service):
        self.service = service

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        return _Request(self.service.threads[id])


class _Users:
    def __init__(self, service):
        self.service = service

    def messages(self):
        return _Messages(self.service)

    def threads(self):
        return _Threads(self.service)

    def getProfile(self, **_kwargs):
        return _Request({"emailAddress": self.service.mailbox})


def max_results_stub(value):
    return int(value or 100)


class FakeGmail:
    """Minimal in-memory Gmail: messages dict, threads dict, tiny q matcher."""

    def __init__(self, mailbox, messages=(), threads=None):
        self.mailbox = mailbox
        self.messages = {m["id"]: m for m in messages}
        self.threads = threads or {}
        self.queries = []

    def users(self):
        return _Users(self)

    def match(self, query):
        self.queries.append(query)
        terms = query.split()
        out = []
        for msg in self.messages.values():
            headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
            labels = set(msg.get("labelIds") or [])
            ts = int(msg["internalDate"]) / 1000
            ok = True
            for term in terms:
                if term == "in:sent":
                    ok &= "SENT" in labels
                elif term == "-in:sent":
                    ok &= "SENT" not in labels
                elif term.startswith("to:"):
                    ok &= term[3:].lower() in headers.get("to", "").lower()
                elif term.startswith("-from:"):
                    ok &= term[6:].lower() not in headers.get("from", "").lower()
                elif term.startswith("from:"):
                    ok &= term[5:].lower() in headers.get("from", "").lower()
                elif term.startswith("after:"):
                    ok &= ts >= datetime.strptime(term[6:], "%Y/%m/%d").replace(tzinfo=timezone.utc).timestamp()
                elif term.startswith("before:"):
                    ok &= ts < datetime.strptime(term[7:], "%Y/%m/%d").replace(tzinfo=timezone.utc).timestamp()
            if ok:
                out.append({"id": msg["id"]})
        return out


WHEN_IN = datetime(2026, 9, 25, 15, tzinfo=timezone.utc)
WHEN_OLD = datetime(2025, 3, 1, 15, tzinfo=timezone.utc)
WHEN_LATER = datetime(2026, 9, 26, 9, tzinfo=timezone.utc)


def _inbound(subject="Proof of garaging"):
    return _msg("in1", "t1", "Client Person <client@example.com>", WHEN_IN, subject)


def test_norm_subject_strips_reply_and_forward_prefixes():
    assert norm_subject("Re: Fwd:  Proof of   Garaging ") == "proof of garaging"
    assert norm_subject("FW: Certificates") == "certificates"


def test_is_related_send_matches_reference_header():
    sent = {"subject": "Fwd: Proof of garaging", "references": "<a@x> <in1@mail.example.com>"}
    detail, verified = is_related_send(sent, "Proof of garaging", "<in1@mail.example.com>")
    assert verified is True
    assert "references original message" in detail


def test_is_related_send_subject_alone_never_matches():
    # Review blocker 1: a bare subject collision must not mark mail handled.
    assert is_related_send({"subject": "Fwd: Proof of garaging", "references": ""},
                           "Proof of garaging", "<zzz@mail.example.com>",
                           "Client Person <client@example.com>") is None


def test_is_related_send_subject_plus_client_is_possible_not_verified():
    detail, verified = is_related_send(
        {"subject": "Fwd: Proof of garaging", "references": "", "to": "client@example.com"},
        "Proof of garaging", "<zzz@mail.example.com>", "Client Person <client@example.com>")
    assert verified is False
    detail2, verified2 = is_related_send(
        {"subject": "Re: Proof of garaging request", "references": "", "cc": "Client <client@example.com>"},
        "Proof of garaging request", "<zzz@mail.example.com>", "client@example.com")
    assert verified2 is False


def test_is_related_send_rejects_unrelated_and_tiny_subjects():
    assert is_related_send({"subject": "Weekly update", "references": ""},
                           "Proof of garaging", "<zzz@mail.example.com>") is None
    assert is_related_send({"subject": "Re: a", "references": ""},
                           "a", "<zzz@mail.example.com>") is None


def test_thread_reply_must_postdate_the_inbound_message():
    old_internal = _msg("m-old", "t1", EMPLOYEE, WHEN_OLD)
    inbound = _inbound()
    later_internal = _msg("m-new", "t1", "Teammate <teammate@streetsmart.insurance>", WHEN_LATER)
    thread_old_only = {"messages": [old_internal, inbound]}
    thread_with_reply = {"messages": [old_internal, inbound, later_internal]}
    assert thread_has_internal_reply(thread_old_only, EMPLOYEE, "in1") is None
    hit = thread_has_internal_reply(thread_with_reply, EMPLOYEE, "in1")
    assert hit and hit["via"] == "reply"


def test_thread_reply_ignores_later_external_sender():
    inbound = _inbound()
    later_client = _msg("m-ext", "t1", "Client Person <client@example.com>", WHEN_LATER)
    thread = {"messages": [inbound, later_client]}
    assert thread_has_internal_reply(thread, EMPLOYEE, "in1") is None


def test_clean_body_strips_quotes_signatures_and_disclaimers():
    raw = "Please add the new truck to the policy.\n\nOn Mon, someone wrote:\n> old text\n--\nRep A\nConfidentiality notice: ..."
    assert clean_body(raw) == "Please add the new truck to the policy."


def test_summarize_flags_action_and_fyi():
    action = summarize("Please send me the updated certificate today. Thanks.")
    assert action["action_needed"] == "likely yes"
    assert "certificate" in action["summary"]
    fyi = summarize("Here is the newsletter you subscribed to. Enjoy reading it.")
    assert fyi["action_needed"] == FYI_ACTION_FLAG
    assert summarize("")["action_needed"] == "unknown"


def _services(employee_messages, shared_messages=(), threads=None):
    return {
        EMPLOYEE: FakeGmail(EMPLOYEE, employee_messages, threads),
        SHARED: FakeGmail(SHARED, shared_messages),
    }


def test_verify_prefers_in_thread_reply():
    inbound = _inbound()
    reply = _msg("m-reply", "t1", EMPLOYEE, WHEN_LATER)
    threads = {"t1": {"messages": [inbound, reply]}}
    services = _services([inbound], threads=threads)
    result, _ = verify_inbound_message(
        services[EMPLOYEE], services, EMPLOYEE, inbound, [EMPLOYEE, SHARED],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["handled_via"] == "reply"
    assert result["summary"] == ""


def test_verify_finds_forward_by_reference_in_employee_sent():
    inbound = _inbound()
    fwd = _msg("m-fwd", "t2", EMPLOYEE, WHEN_LATER, "Fwd: Proof of garaging",
               extra_headers={"References": "<in1@mail.example.com>"})
    fwd["labelIds"] = ["SENT"]
    threads = {"t1": {"messages": [inbound]}}
    services = _services([inbound, fwd], threads=threads)
    result, _ = verify_inbound_message(
        services[EMPLOYEE], services, EMPLOYEE, inbound, [EMPLOYEE, SHARED],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["handled_via"] == "forward"
    assert EMPLOYEE in result["detail"]


def test_verify_subject_plus_client_send_is_possible_not_handled():
    inbound = _inbound()
    fwd = _msg("m-fwd", "t2", SHARED, WHEN_LATER, "Fwd: Proof of garaging",
               to_addr="Client Person <client@example.com>")
    fwd["labelIds"] = ["SENT"]
    threads = {"t1": {"messages": [inbound]}}
    services = _services([inbound], [fwd], threads)
    result, _ = verify_inbound_message(
        services[EMPLOYEE], services, EMPLOYEE, inbound, [EMPLOYEE, SHARED],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["handled_via"] == "unhandled"
    assert result["detail"].startswith("possible related send")
    assert SHARED in result["detail"]


def test_verify_unhandled_gets_summary_and_action_flag():
    inbound = _inbound()
    inbound["payload"]["mimeType"] = "text/plain"
    import base64
    inbound["payload"]["body"] = {
        "data": base64.urlsafe_b64encode(b"Please confirm the VIN for the new unit today.").decode()
    }
    threads = {"t1": {"messages": [inbound]}}
    services = _services([inbound], threads=threads)
    result, _ = verify_inbound_message(
        services[EMPLOYEE], services, EMPLOYEE, inbound, [EMPLOYEE, SHARED],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["handled_via"] == "unhandled"
    assert result["action_needed"] == "likely yes"
    assert "VIN" in result["summary"]


def test_verify_check_failed_is_reported_not_raised():
    inbound = _inbound()
    services = _services([inbound], threads={})  # missing thread -> error path
    result = verify_mailbox_handled(
        EMPLOYEE, services, sent_mailboxes=[EMPLOYEE],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["check_failed"] == 1
    assert result["reply_rate"] is None
    assert result["rate_status"].startswith("unevaluable")


def test_internal_senders_are_never_graded_as_client_mail():
    inbound = _inbound()
    coworker = _msg("in2", "t9", "Coworker <coworker@streetsmart.insurance>", WHEN_IN)
    services = _services([inbound, coworker], threads={"t1": {"messages": [inbound]}})
    result = verify_mailbox_handled(
        EMPLOYEE, services, sent_mailboxes=[EMPLOYEE],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["received"] == 1


def test_reply_rate_excludes_fyi_from_denominator():
    handled = _inbound("Proof of garaging")
    reply = _msg("m-reply", "t1", EMPLOYEE, WHEN_LATER)
    fyi = _msg("in3", "t3", "Newsletter <news@example.com>", WHEN_IN, "Your weekly digest",
               body="Here is the newsletter you subscribed to. Enjoy reading it.")
    threads = {
        "t1": {"messages": [handled, reply]},
        "t3": {"messages": [fyi]},
    }
    services = _services([handled, fyi], threads=threads)
    result = verify_mailbox_handled(
        EMPLOYEE, services, sent_mailboxes=[EMPLOYEE],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["received"] == 2
    assert result["handled_via_reply"] == 1
    assert result["unhandled_fyi"] == 1
    assert result["reply_rate"] == 1.0


def test_verification_window_covers_lookback_and_includes_today():
    after, before = verification_window(AS_OF, 7)
    assert after == "2026/09/23"
    assert before == "2026/09/30"


def test_collect_handled_verification_end_to_end():
    inbound = _inbound()
    reply = _msg("m-reply", "t1", EMPLOYEE, WHEN_LATER)
    threads = {"t1": {"messages": [inbound, reply]}}

    def factory(_service_account, mailbox):
        assert mailbox == EMPLOYEE
        return FakeGmail(mailbox, [inbound], threads)

    summary = collect_handled_verification(
        environment={"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "hermes@example.test"},
        service_factory=factory,
        approved_users=[EMPLOYEE],
        shared_mailboxes=[],
        as_of=AS_OF,
    )
    assert summary["source_status"] == "available"
    assert summary["scope"] == GMAIL_READONLY_SCOPE
    assert summary["body_access"] is True
    assert summary["handled_via_reply"] == 1
    assert summary["reply_rate"] == 1.0


def test_collect_verifies_every_delegated_mailbox():
    inbound = _inbound()

    def factory(_service_account, mailbox):
        return FakeGmail("someone-else@streetsmart.insurance", [inbound], {})

    with pytest.raises(GmailAccountabilityError, match="verification mismatch"):
        collect_handled_verification(
            environment={"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "hermes@example.test"},
            service_factory=factory,
            approved_users=[EMPLOYEE],
            shared_mailboxes=[],
            as_of=AS_OF,
        )


def test_collect_fails_closed_without_service_account_or_users():
    summary = collect_handled_verification(environment={}, approved_users=[EMPLOYEE], service_factory=lambda *_a: None)
    assert summary["source_status"].startswith("missing")
    summary = collect_handled_verification(
        environment={"ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT": "hermes@example.test"},
        approved_users=[],
        service_factory=lambda *_a: None,
    )
    assert summary["source_status"].startswith("missing")


def test_report_worker_wires_opt_in_handled_verification(tmp_path):
    """Manifest flag on -> gmail snapshot gains handled_verification key."""
    import json
    from robie_job_engine.accountability_jobs import AccountabilityReportWorker

    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "source_status": "available",
        "employees": {"Alex Example": {"email": "alex@streetsmart.insurance"}},
    }), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "output_dir": str(tmp_path / "reports"),
        "sources": {"roles_json": str(roles)},
        "rules": {"require_complete_evidence": False},
        "collection": {"gmail_accountability": {
            "enabled": True,
            "verify_handled": {"enabled": True, "lookback_days": 3},
        }},
    }), encoding="utf-8")
    job = {"action_type": "accountability.weekly", "payload": {"manifest_path": str(manifest)}}
    result = AccountabilityReportWorker().perform(job, idempotency_key="verify-wiring")
    # No delegated SA in the test env: verification fails closed into a
    # "missing ..." source status, tolerated because require_complete_evidence=false.
    assert result.succeeded, result.error
    snapshots = list((tmp_path / "reports").glob("gmail-*.json"))
    assert len(snapshots) == 1
    snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert "handled_verification" in snapshot
    assert snapshot["handled_verification"]["source_status"].startswith("missing")


def test_report_worker_leaves_snapshot_untouched_without_opt_in(tmp_path):
    import json
    from robie_job_engine.accountability_jobs import AccountabilityReportWorker

    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "source_status": "available",
        "employees": {"Alex Example": {"email": "alex@streetsmart.insurance"}},
    }), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "output_dir": str(tmp_path / "reports"),
        "sources": {"roles_json": str(roles)},
        "rules": {"require_complete_evidence": False},
        "collection": {"gmail_accountability": {"enabled": True}},
    }), encoding="utf-8")
    job = {"action_type": "accountability.weekly", "payload": {"manifest_path": str(manifest)}}
    AccountabilityReportWorker().perform(job, idempotency_key="verify-off")
    snapshot = json.loads(list((tmp_path / "reports").glob("gmail-*.json"))[0].read_text(encoding="utf-8"))
    assert "handled_verification" not in snapshot


def test_daily_report_renders_verification_table_and_summaries():
    from robie_job_engine.reporting_suite import ReportingSuite

    email_data = {
        "source_status": "available",
        "by_employee": {},
        "handled_verification": {
            "source_status": "available",
            "by_employee": {
                "rep-a@streetsmart.insurance": {
                    "received": 3,
                    "handled_via_reply": 1,
                    "handled_via_forward": 1,
                    "unhandled_action": 1,
                    "unhandled_fyi": 0,
                    "check_failed": 0,
                    "reply_rate": round(2 / 3, 3),
                    "items": [
                        {"handled_via": "unhandled", "subject": "Certificate question",
                         "summary": "Client asks for an updated certificate.",
                         "action_needed": "likely yes"},
                    ],
                },
            },
        },
    }
    report = ReportingSuite().build_daily_report({}, {}, email_data=email_data)
    assert "EMAIL HANDLED VERIFICATION" in report
    assert "| rep-a@streetsmart.insurance | 1 | 1 | 1 | 0 | 0 | 67% |" in report
    assert "Certificate question" in report
    assert "Client asks for an updated certificate." in report


def test_reports_render_nothing_without_verification_data():
    from robie_job_engine.reporting_suite import ReportingSuite

    daily = ReportingSuite().build_daily_report({}, {}, email_data={"source_status": "available", "by_employee": {}})
    assert "EMAIL HANDLED VERIFICATION" not in daily
    flagged = ReportingSuite().build_daily_report(
        {}, {}, email_data={"source_status": "available", "by_employee": {},
                            "handled_verification": {"source_status": "missing delegated service account"}})
    assert "EMAIL HANDLED VERIFICATION" in flagged
    assert "UNVERIFIED" in flagged


# ------------------------------------------------------ review regression tests

def test_old_send_with_matching_subject_never_marks_newer_inbound_handled():
    """Review blocker 1: a send that PREDATES the inbound is not its forward,
    even with subject + client-recipient match (e.g. an old hello@ send)."""
    inbound = _inbound()
    old_send = _msg("m-old-send", "t2", SHARED, WHEN_OLD, "Fwd: Proof of garaging",
                    to_addr="client@example.com")
    old_send["labelIds"] = ["SENT"]
    threads = {"t1": {"messages": [inbound]}}
    services = _services([inbound], [old_send], threads)
    result, _capped = verify_inbound_message(
        services[EMPLOYEE], services, EMPLOYEE, inbound, [EMPLOYEE, SHARED],
        after="2025/01/01", before="2026/09/30",
    )
    assert result["handled_via"] == "unhandled"


def test_lookalike_domain_is_not_internal():
    """Review blocker 2: exact-domain parsing, not substring."""
    from robie_job_engine.handled_verification import is_internal
    assert is_internal("Rep A <rep-a@streetsmart.insurance>")
    assert not is_internal("Attacker <x@notstreetsmart.insurance.evil.com>")
    assert not is_internal("Client Person <client@example.com>")
    assert not is_internal("")


def test_lookalike_domain_sender_is_graded_as_client_mail():
    inbound = _msg("in1", "t1", "Spoofer <x@notstreetsmart.insurance.evil.com>", WHEN_IN)
    threads = {"t1": {"messages": [inbound]}}
    services = _services([inbound], threads=threads)
    result = verify_mailbox_handled(
        EMPLOYEE, services, sent_mailboxes=[EMPLOYEE],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["received"] == 1


def test_no_keyword_unhandled_is_unknown_not_fyi_and_stays_in_denominator():
    """Review blocker 3: ambiguous mail can no longer hide as FYI."""
    inbound = _inbound()
    inbound["payload"]["mimeType"] = "text/plain"
    import base64
    inbound["payload"]["body"] = {
        "data": base64.urlsafe_b64encode(b"The garage address changed last month.").decode()
    }
    handled = _msg("in2", "t2", "Other Client <other@example.com>", WHEN_IN, "Second matter")
    reply = _msg("m-reply", "t2", EMPLOYEE, WHEN_LATER)
    threads = {"t1": {"messages": [inbound]}, "t2": {"messages": [handled, reply]}}
    services = _services([inbound, handled], threads=threads)
    result = verify_mailbox_handled(
        EMPLOYEE, services, sent_mailboxes=[EMPLOYEE],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["unhandled_unknown"] == 1
    assert result["unhandled_fyi"] == 0
    assert result["received"] == 2
    assert result["reply_rate"] == 0.5  # unknown item still counts against the rate


def test_positive_fyi_signal_is_excluded_from_denominator():
    summary = summarize("Here is your weekly digest. Unsubscribe anytime.", "news@example.com")
    assert summary["action_needed"] == FYI_ACTION_FLAG
    noreply = summarize("Your receipt is attached.", "no-reply@example.com")
    assert noreply["action_needed"] == FYI_ACTION_FLAG


def test_inbound_cap_flags_mailbox_partial():
    """Review blocker 4: hitting the inbound cap marks output partial."""
    msgs = [_msg(f"in{i}", f"t{i}", f"Client {i} <c{i}@example.com>", WHEN_IN) for i in range(3)]
    threads = {f"t{i}": {"messages": [m]} for i, m in enumerate(msgs)}
    services = _services(msgs, threads=threads)
    result = verify_mailbox_handled(
        EMPLOYEE, services, sent_mailboxes=[EMPLOYEE],
        after="2026/09/22", before="2026/09/30", max_messages=2,
    )
    assert result["received"] == 2
    assert result["inbound_capped"] is True
    assert result["partial"] is True


def test_errored_check_makes_rate_unevaluable():
    """Review blocker 5: an errored item poisons the mailbox rate."""
    good = _msg("in2", "t2", "Other Client <other@example.com>", WHEN_IN, "Second matter")
    reply = _msg("m-reply", "t2", EMPLOYEE, WHEN_LATER)
    bad = _inbound()  # thread t1 missing from the fake -> per-item error
    threads = {"t2": {"messages": [good, reply]}}
    services = _services([good, bad], threads=threads)
    result = verify_mailbox_handled(
        EMPLOYEE, services, sent_mailboxes=[EMPLOYEE],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["handled_via_reply"] == 1
    assert result["check_failed"] == 1
    assert result["reply_rate"] is None
    assert result["rate_status"].startswith("unevaluable")



# --------------------------------------------- re-review regression tests (round 2)

def test_later_unrelated_send_same_client_same_subject_not_verified():
    """Re-review 1: a later, unrelated send to the same client under the same
    recurring subject must NOT count as verified handled without Message-ID
    linkage; it is labeled a possible related send for human judgment."""
    inbound = _inbound()
    unrelated = _msg("m-unrel", "t9", EMPLOYEE, WHEN_LATER, "Fwd: Proof of garaging",
                     to_addr="client@example.com")
    unrelated["labelIds"] = ["SENT"]
    threads = {"t1": {"messages": [inbound]}}
    services = _services([inbound, unrelated], threads=threads)
    result = verify_mailbox_handled(
        EMPLOYEE, services, sent_mailboxes=[EMPLOYEE],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["handled_via_forward"] == 0
    assert result["items"][0]["handled_via"] == "unhandled"
    assert result["items"][0]["detail"].startswith("possible related send")


def test_forged_display_name_with_employee_address_is_not_internal_reply():
    """Re-review 2: a From header whose display name contains the employee
    address but whose actual address is external is not an internal reply."""
    inbound = _inbound()
    forged = _msg("m-forged", "t1", f'"{EMPLOYEE}" <attacker@evil.example.com>', WHEN_LATER)
    threads = {"t1": {"messages": [inbound, forged]}}
    services = _services([inbound], threads=threads)
    result, _ = verify_inbound_message(
        services[EMPLOYEE], services, EMPLOYEE, inbound, [EMPLOYEE],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["handled_via"] == "unhandled"


def test_exact_employee_sender_still_counts_as_reply():
    inbound = _inbound()
    reply = _msg("m-reply", "t1", f"Rep A <{EMPLOYEE}>", WHEN_LATER)
    threads = {"t1": {"messages": [inbound, reply]}}
    services = _services([inbound], threads=threads)
    result, _ = verify_inbound_message(
        services[EMPLOYEE], services, EMPLOYEE, inbound, [EMPLOYEE],
        after="2026/09/22", before="2026/09/30",
    )
    assert result["handled_via"] == "reply"


def test_partial_mailbox_rate_renders_as_sampled_not_complete():
    """Re-review 3: a capped window must never render as a bare rate."""
    from robie_job_engine.reporting_suite import _email_verification_lines
    lines = _email_verification_lines({"handled_verification": {
        "source_status": "available",
        "by_employee": {"rep-a@streetsmart.insurance": {
            "received": 5, "handled_via_reply": 2, "handled_via_forward": 1,
            "unhandled_action": 1, "unhandled_unknown": 1, "unhandled_fyi": 0,
            "check_failed": 0, "reply_rate": 0.6, "rate_status": "ok", "partial": True,
            "items": []}},
    }})
    table_row = [line for line in lines if line.startswith("| rep-a")][0]
    assert "SAMPLED ONLY" in table_row
    assert "not a complete rate" in table_row


def test_unverified_possible_send_labeled_in_unhandled_bullets():
    from robie_job_engine.reporting_suite import _email_verification_lines
    lines = _email_verification_lines({"handled_verification": {
        "source_status": "available",
        "by_employee": {"rep-a@streetsmart.insurance": {
            "received": 1, "handled_via_reply": 0, "handled_via_forward": 0,
            "unhandled_action": 1, "unhandled_unknown": 0, "unhandled_fyi": 0,
            "check_failed": 0, "reply_rate": 0.0, "rate_status": "ok", "partial": False,
            "items": [{"handled_via": "unhandled", "subject": "Proof of garaging",
                       "summary": "Client asks for garaging proof.",
                       "action_needed": "likely yes",
                       "detail": "possible related send (unverified, not counted as handled): rep-a sent matching subject"}]}},
    }})
    bullet = [line for line in lines if line.startswith("•")][0]
    assert "possible related send (unverified, not counted as handled)" in bullet
