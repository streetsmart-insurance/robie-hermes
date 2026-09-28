"""False-positive battery for the 4359 phase-2 follow-up worker.

Phase 2 (daily): reply tracking, confirmation checking, 14-day escalation.

Every test here proves the worker does NOT do something it shouldn't:
attribute a reply to the wrong policy, invent a status from an ambiguous
or automated reply, confirm a change without the required evidence, nag a
CSR who already engaged, escalate early/late/ twice, or trust a reply from
a terminated CSR. Fake clients only: no network, no secrets, no sends.

Carlo's build requirements covered:
- reply -> wrong policy/change does not update status
- ambiguous reply stays no_signal
- automated/out-of-office mail stays no_signal
- different-policy endorsement cannot confirm the change
- confirmation requires the queue-drop evidence (no field-match guessing)
- escalation boundary: 13 days no, 14 days no, 15 days yes
- terminated-CSR reply -> needs_human
- duplicate reply ingestion is idempotent
- dry-run sends nothing and mutates no state
"""

from datetime import date

import pytest

from robie_job_engine.overdue_policy_change_reports import (
    ACTION,
    NotificationStore,
    OverduePolicyChangeReportWorker,
    build_roster_maps,
    notification_key,
    parse_4359_csv,
)
from robie_job_engine.policy_change_followup import (
    ESCALATION_AFTER_DAYS,
    ESCALATION_TO,
    STATUS_BLOCKED,
    STATUS_CONFIRMED,
    STATUS_DISCREPANCY,
    STATUS_DOCS_CLAIMED,
    STATUS_IN_PROGRESS,
    STATUS_NEEDS_HUMAN,
    STATUS_NO_SIGNAL,
    DryRunFollowupStore,
    FollowupStore,
    PolicyChangeFollowupWorker,
    build_escalation_email,
    classify_reply,
    escalation_due,
    evaluate_confirmation,
    find_endorsement_documents,
    ingest_replies,
)

# Tuesday 2026-09-29 — the weekly rhythm's escalation day.
TUESDAY = date(2026, 9, 29)
MONDAY = date(2026, 9, 28)

HEADER = (
    "Account Name,Applicant ID,Policy Number,Line Of Business,Effective Date,"
    "Master Company,Request Status,Created By,Written Premium,Premium - Annualized,"
    "Branch,Department,Service Team,Assigned Producer,CSR,Preferred Language,"
    "Applicant Labels,Policy Labels,Change Request Created Date"
)


def csv_bytes(*rows: str) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode("utf-8")


def row(account="SAPP Construction Corp", applicant="41055091", policy="S 2391821",
        created="2026-08-17", csr="Eimy Ramos", status="Open",
        carrier="Selective Insurance", lob="Commercial Pkg",
        producer="Sandy Santana") -> str:
    return (
        f"{account},{applicant},{policy},{lob},2025-12-10,{carrier},{status},"
        f"\"Quezada, Zeus\",$28936.00,$28936.00,Streetsmart Insurance,"
        f"Commercial Lines,,{producer},{csr},English,,,{created}"
    )


def key_for(csr="Eimy Ramos", policy="S 2391821", created="2026-08-17") -> str:
    return notification_key(csr, policy, created)


def roster():
    return {
        "source_status": "available",
        "employees": {
            "Eimy Ramos": {"role": "Commercial Lines Account Technician",
                           "email": "eimy@streetsmart.insurance",
                           "department": "Commercial Lines", "manager": "",
                           "status": "Active"},
            "Sandy Santana": {"role": "Commercial Lines Department Manager",
                               "email": "sandy@streetsmart.insurance",
                               "department": "Commercial Lines", "manager": "",
                               "status": "Active"},
        },
    }


def sent_store_with_thread(tmp_path, key, first_nag, thread_id="thread-1"):
    store = NotificationStore(tmp_path / "sent.json")
    store._sent[key] = {"date": first_nag.isoformat(),
                        "message_id": "m1", "thread_id": thread_id}
    store.save()
    return NotificationStore(tmp_path / "sent.json")


def followup_worker(tmp_path, *, rows=None, threads=None, documents=None,
                    csr_is_active=None, followup=None, escalation_to=None):
    """Phase-2 worker with every external access faked."""
    threads = threads or {}
    documents = documents if documents is not None else []
    sent = []

    def fake_mailer(*, to, subject, text_body, html_body):
        record = {"kind": "live-test", "destination": list(to),
                  "subject": subject, "text_body": text_body,
                  "html_body": html_body, "message_id": f"e{len(sent)}"}
        sent.append(record)
        if escalation_to is not None:
            escalation_to.append(record)
        return record

    queue_rows = parse_4359_csv(csv_bytes(*rows)) if rows else []
    if followup is None:
        followup = FollowupStore(tmp_path / "followup.json")

    worker = PolicyChangeFollowupWorker(
        queue_reader=lambda payload: queue_rows,
        reply_reader=lambda tid: threads.get(tid, []),
        document_search=lambda applicant_id: documents,
        escalation_mailer=fake_mailer,
        sent_store=NotificationStore(tmp_path / "sent.json"),
        followup_store=followup,
        csr_is_active=csr_is_active,
    )
    return worker, sent


# -- reply classification ------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Working on it — carrier said 2-3 days.",
    "I'll have this done today.",
    "Following up with the underwriter now.",
])
def test_classify_in_progress(text):
    assert classify_reply(text) == STATUS_IN_PROGRESS


@pytest.mark.parametrize("text", [
    "Waiting on the carrier, they haven't responded.",
    "Blocked — no response from Selective underwriting.",
])
def test_classify_blocked(text):
    assert classify_reply(text) == STATUS_BLOCKED


@pytest.mark.parametrize("text", [
    "Endorsement received, attached below.",
    "Got the dec page from Selective this morning.",
])
def test_classify_docs_claimed(text):
    assert classify_reply(text) == STATUS_DOCS_CLAIMED


@pytest.mark.parametrize("text", [
    "Thanks.",
    "Got it, thanks!",
    "Please advise on next steps.",
    "I don't handle this account, please contact underwriting.",
    "",
])
def test_classify_ambiguous_stays_no_signal(text):
    assert classify_reply(text) == STATUS_NO_SIGNAL


@pytest.mark.parametrize("text", [
    "Out of office until Monday.",
    "Automatic reply: I am currently away with limited access to email.",
    "This is an auto-reply. Do not reply to this message.",
])
def test_classify_automated_stays_no_signal(text):
    assert classify_reply(text) == STATUS_NO_SIGNAL


def test_classify_docs_claimed_outranks_blocked():
    # Mixed signal: the doc claim is the stronger, checkable assertion.
    assert classify_reply(
        "Was blocked for weeks, but the endorsement is in now."
    ) == STATUS_DOCS_CLAIMED


# -- reply ingestion -----------------------------------------------------------


def _ingest(followup, key, messages, thread_id="thread-1", csr_is_active=None):
    key_context = {key: {"policy_digits": "2391821", "csr": "Eimy Ramos",
                         "applicant_id": "41055091"}}
    sent = NotificationStore.__new__(NotificationStore)
    sent._sent = {key: {"date": "2026-09-14", "message_id": "m0",
                        "thread_id": thread_id}}
    return ingest_replies(
        sent_store=sent,
        followup=followup,
        read_thread=lambda tid: messages if tid == thread_id else [],
        key_context=key_context,
        csr_is_active=csr_is_active,
        today=TUESDAY,
    ), followup.get(key)


def test_ingest_in_progress_reply_updates_status(tmp_path):
    followup = FollowupStore(tmp_path / "followup.json")
    key = key_for()
    stats, record = _ingest(followup, key, [
        {"id": "r1", "from": "robie@streetsmart.insurance",
         "date": "Tue, 29 Sep 2026 09:00:00 -0400", "text": "nag body"},
        {"id": "r2", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
         "date": "Tue, 29 Sep 2026 10:00:00 -0400",
         "text": "Working on it — carrier said 2-3 days."},
    ])
    assert stats["replies_ingested"] == 1
    assert record["status"] == STATUS_IN_PROGRESS
    assert record["last_processed_message_id"] == "r2"
    assert record["last_reply_date"] == "2026-09-29"
    assert any(e["event"] == "status_change" for e in record["history"])


def test_ingest_duplicate_message_is_idempotent(tmp_path):
    followup = FollowupStore(tmp_path / "followup.json")
    key = key_for()
    messages = [
        {"id": "r1", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
         "date": "Tue, 29 Sep 2026 10:00:00 -0400", "text": "Working on it."},
    ]
    stats1, _ = _ingest(followup, key, messages)
    stats2, record = _ingest(followup, key, messages)
    assert stats1["replies_ingested"] == 1
    assert stats2["replies_ingested"] == 0
    changes = [e for e in record["history"] if e["event"] == "status_change"]
    assert len(changes) == 1


def test_ingest_wrong_policy_reply_does_not_update_status(tmp_path):
    followup = FollowupStore(tmp_path / "followup.json")
    key = key_for()
    stats, record = _ingest(followup, key, [
        {"id": "r1", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
         "date": "Tue, 29 Sep 2026 10:00:00 -0400",
         # Digits 1388156 belong to a DIFFERENT policy, not S 2391821.
         "text": "Endorsement received for policy 13WECCE5FJ6, all set."},
    ])
    assert stats["misattributed_replies"] == 1
    assert stats["replies_ingested"] == 0
    assert record["status"] == STATUS_NO_SIGNAL
    assert any(e["event"] == "misattributed_reply" for e in record["history"])


def test_ingest_ambiguous_reply_keeps_no_signal(tmp_path):
    followup = FollowupStore(tmp_path / "followup.json")
    key = key_for()
    _, record = _ingest(followup, key, [
        {"id": "r1", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
         "date": "Tue, 29 Sep 2026 10:00:00 -0400", "text": "Thanks."},
    ])
    assert record["status"] == STATUS_NO_SIGNAL


def test_ingest_does_not_downgrade_engaged_status(tmp_path):
    followup = FollowupStore(tmp_path / "followup.json")
    key = key_for()
    _ingest(followup, key, [
        {"id": "r1", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
         "date": "Tue, 29 Sep 2026 10:00:00 -0400", "text": "Working on it."},
    ])
    _, record = _ingest(followup, key, [
        {"id": "r2", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
         "date": "Tue, 29 Sep 2026 11:00:00 -0400", "text": "Thanks."},
    ])
    assert record["status"] == STATUS_IN_PROGRESS


def test_ingest_skips_own_messages(tmp_path):
    followup = FollowupStore(tmp_path / "followup.json")
    key = key_for()
    stats, record = _ingest(followup, key, [
        {"id": "r1", "from": "robie@streetsmart.insurance",
         "date": "Tue, 29 Sep 2026 09:00:00 -0400", "text": "weekly nag"},
    ])
    assert stats["replies_ingested"] == 0
    assert record["status"] == STATUS_NO_SIGNAL


def test_ingest_terminated_csr_reply_goes_needs_human(tmp_path):
    followup = FollowupStore(tmp_path / "followup.json")
    key = key_for()
    _, record = _ingest(
        followup, key,
        [{"id": "r1", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
          "date": "Tue, 29 Sep 2026 10:00:00 -0400",
          "text": "Working on it — will update soon."}],
        csr_is_active=lambda csr: False,
    )
    assert record["status"] == STATUS_NEEDS_HUMAN
    assert "no longer an active employee" in record["history"][-1]["detail"]


def test_ingest_late_reply_does_not_reopen_confirmed(tmp_path):
    followup = FollowupStore(tmp_path / "followup.json")
    key = key_for()
    followup.set_status(key, STATUS_CONFIRMED, TUESDAY, "queue drop")
    _, record = _ingest(followup, key, [
        {"id": "r9", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
         "date": "Tue, 29 Sep 2026 12:00:00 -0400", "text": "Working on it."},
    ])
    assert record["status"] == STATUS_CONFIRMED
    assert any(e["event"] == "reply_after_confirmed" for e in record["history"])


# -- confirmation checking -----------------------------------------------------


def test_confirmation_requires_queue_drop():
    verdict = evaluate_confirmation(
        item={"Policy Number": "S 2391821", "Account Name": "SAPP",
              "created_date": "2026-08-17"},
        still_on_open_queue=False, documents=[],
        reply_text="Endorsement received.",
    )
    assert verdict["verdict"] == STATUS_CONFIRMED


def test_confirmation_still_open_no_docs_is_needs_human():
    verdict = evaluate_confirmation(
        item={"Policy Number": "S 2391821", "Account Name": "SAPP",
              "created_date": "2026-08-17"},
        still_on_open_queue=True, documents=[],
        reply_text="Endorsement received.",
    )
    assert verdict["verdict"] == STATUS_NEEDS_HUMAN
    assert "SAPP" in verdict["detail"] and "S 2391821" in verdict["detail"]


def test_confirmation_still_open_with_candidate_doc_is_needs_human():
    # Document metadata has no machine-readable insured/effective-date/
    # premium fields: a same-policy endorsement doc is a lead, not proof.
    verdict = evaluate_confirmation(
        item={"Policy Number": "S 2391821", "Account Name": "SAPP",
              "created_date": "2026-08-17"},
        still_on_open_queue=True,
        documents=[{"id": "99", "name": "Endorsement S2391821 09-2026.pdf"}],
        reply_text="Endorsement received.",
    )
    assert verdict["verdict"] == STATUS_NEEDS_HUMAN
    assert "no machine-readable" in verdict["detail"]


def test_confirmation_reply_names_different_policy_is_discrepancy():
    verdict = evaluate_confirmation(
        item={"Policy Number": "S 2391821", "Account Name": "SAPP",
              "created_date": "2026-08-17"},
        still_on_open_queue=True, documents=[],
        reply_text="Endorsement received for policy 13WECCE5FJ6, all set.",
    )
    assert verdict["verdict"] == STATUS_DISCREPANCY
    assert "do not match" in verdict["detail"]
    assert "1356" in verdict["detail"]  # the offending policy's digits


def test_different_policy_endorsement_cannot_confirm():
    candidates, ignored = find_endorsement_documents(
        [{"id": "7", "name": "Endorsement 13WECCE5FJ6.pdf"}],
        "S 2391821",
    )
    assert candidates == []
    assert [d["id"] for d in ignored] == ["7"]
    # And the verdict path never confirms on it either:
    verdict = evaluate_confirmation(
        item={"Policy Number": "S 2391821", "Account Name": "SAPP",
              "created_date": "2026-08-17"},
        still_on_open_queue=True,
        documents=[{"id": "7", "name": "Endorsement 13WECCE5FJ6.pdf"}],
        reply_text="Endorsement received.",
    )
    assert verdict["verdict"] == STATUS_NEEDS_HUMAN
    assert "Ignored (never confirming on these)" in verdict["detail"]


def test_find_endorsement_documents_same_policy_candidate():
    candidates, ignored = find_endorsement_documents(
        [
            {"id": "1", "name": "Endorsement S2391821 09-2026.pdf"},
            {"id": "2", "name": "Dec page renewal packet.pdf"},
            {"id": "3", "name": "Endorsement 13WECCE5FJ6.pdf"},
        ],
        "S 2391821",
    )
    assert [d["id"] for d in candidates] == ["1"]
    assert [d["id"] for d in ignored] == ["3"]
    # "Dec page renewal packet.pdf" names no policy at all: not a candidate,
    # not ignored — it simply carries no attributable signal.


# -- 14-day escalation ---------------------------------------------------------


def _record(status, escalated=None):
    return {"status": status, "escalated_date": escalated}


@pytest.mark.parametrize("first_nag,expected", [
    (date(2026, 9, 16), False),  # 13 days: no
    (date(2026, 9, 15), False),  # 14 days: no
    (date(2026, 9, 14), True),   # 15 days: yes
])
def test_escalation_boundary(first_nag, expected):
    assert escalation_due(_record(STATUS_IN_PROGRESS), first_nag, TUESDAY) is expected


def test_escalation_skips_confirmed_already_escalated_and_unknown_nag():
    assert escalation_due(_record(STATUS_CONFIRMED), date(2026, 8, 1), TUESDAY) is False
    assert escalation_due(_record(STATUS_IN_PROGRESS, "2026-09-20"),
                          date(2026, 8, 1), TUESDAY) is False
    assert escalation_due(_record(STATUS_IN_PROGRESS), None, TUESDAY) is False
    assert ESCALATION_AFTER_DAYS == 14


def test_escalation_email_content():
    subject, text, html_body = build_escalation_email([{
        "account_name": "SAPP Construction Corp",
        "policy_number": "S 2391821",
        "age_days": 43,
        "csr": "Eimy Ramos",
        "status": STATUS_IN_PROGRESS,
        "last_reply_date": "2026-09-28",
        "missing": "CSR is working on it: carrier said 2-3 days",
    }], TUESDAY)
    assert subject == "Policy changes unconfirmed after 14 days"
    for needle in ("SAPP Construction Corp", "S 2391821", "43 days",
                   "Eimy Ramos", "in progress", "2026-09-28",
                   "carrier said 2-3 days", "-Robie"):
        assert needle in text, needle
    assert "<li>" in html_body and "SAPP Construction Corp" in html_body


# -- full worker: Tuesday run ----------------------------------------------------


def test_tuesday_run_escalates_and_suppresses_nothing_it_shouldnt(tmp_path):
    key = key_for()
    sent_store = sent_store_with_thread(tmp_path, key, date(2026, 9, 14))
    emitted = []
    worker, _ = followup_worker(
        tmp_path,
        rows=[row()],
        threads={"thread-1": [
            {"id": "r1", "from": "robie@streetsmart.insurance",
             "date": "Mon, 14 Sep 2026 08:00:00 -0400", "text": "nag"},
            {"id": "r2", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
             "date": "Mon, 28 Sep 2026 10:00:00 -0400",
             "text": "Waiting on the carrier, they haven't responded."},
        ]},
        escalation_to=emitted,
    )
    worker.sent_store = sent_store
    evidence = worker.perform({"action_type": ACTION, "payload": {}},
                              dry_run=False, today=TUESDAY)
    assert evidence["succeeded"], evidence["error"]
    assert evidence["reply_ingestion"]["replies_ingested"] == 1
    assert evidence["status_counts"] == {STATUS_BLOCKED: 1}
    escalation = evidence["escalation"]
    assert escalation["emitted"] is True
    assert len(emitted) == 1
    assert emitted[0]["destination"] == [ESCALATION_TO]
    assert "SAPP Construction Corp" in emitted[0]["text_body"]
    # Escalation recorded: a second run must not duplicate it.
    evidence2 = worker.perform({"action_type": ACTION, "payload": {}},
                               dry_run=False, today=TUESDAY)
    assert evidence2["escalation"]["emitted"] is False
    assert len(emitted) == 1


def test_monday_run_never_escalates(tmp_path):
    key = key_for()
    sent_store = sent_store_with_thread(tmp_path, key, date(2026, 9, 10))
    emitted = []
    worker, _ = followup_worker(tmp_path, rows=[row()], escalation_to=emitted)
    worker.sent_store = sent_store
    evidence = worker.perform({"action_type": ACTION, "payload": {}},
                              dry_run=False, today=MONDAY)
    assert evidence["succeeded"], evidence["error"]
    assert evidence["escalation"]["emitted"] is False
    assert emitted == []


def test_docs_claimed_dropped_off_queue_confirms(tmp_path):
    key = key_for()
    sent_store = sent_store_with_thread(tmp_path, key, date(2026, 9, 14))
    worker, _ = followup_worker(
        tmp_path,
        rows=[row()],
        threads={"thread-1": [
            {"id": "r1", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
             "date": "Mon, 28 Sep 2026 10:00:00 -0400",
             "text": "Endorsement received, attached below."},
        ]},
    )
    worker.sent_store = sent_store
    evidence = worker.perform({"action_type": ACTION, "payload": {}},
                              dry_run=False, today=TUESDAY)
    assert evidence["status_counts"] == {STATUS_DOCS_CLAIMED: 1}
    assert evidence["confirmations"][0]["verdict"] == STATUS_NEEDS_HUMAN
    # Next day the change is gone from the queue: confirmed, no more nags.
    worker2, _ = followup_worker(tmp_path, rows=[],
                                 followup=worker.followup_store)
    worker2.sent_store = sent_store
    evidence2 = worker2.perform({"action_type": ACTION, "payload": {}},
                               dry_run=False, today=TUESDAY)
    assert evidence2["status_counts"] == {STATUS_CONFIRMED: 1}
    assert evidence2["escalation"]["emitted"] is False


def test_dry_run_sends_nothing_and_mutates_no_state(tmp_path):
    key = key_for()
    sent_path = tmp_path / "sent.json"
    followup_path = tmp_path / "followup.json"
    sent_store = sent_store_with_thread(tmp_path, key, date(2026, 9, 14))
    sent_before = sent_path.read_bytes()
    emitted = []
    worker, _ = followup_worker(
        tmp_path,
        rows=[row()],
        threads={"thread-1": [
            {"id": "r1", "from": "Eimy Ramos <eimy@streetsmart.insurance>",
             "date": "Mon, 28 Sep 2026 10:00:00 -0400",
             "text": "Waiting on the carrier."},
        ]},
        followup=DryRunFollowupStore(followup_path),
        escalation_to=emitted,
    )
    worker.sent_store = sent_store
    evidence = worker.perform({"action_type": ACTION, "payload": {}},
                              dry_run=True, today=TUESDAY)
    assert evidence["succeeded"], evidence["error"]
    assert evidence["mode"] == "dry-run"
    assert emitted == []
    assert evidence["escalation"]["receipt"]["kind"] == "dry-run"
    assert not followup_path.exists()
    assert sent_path.read_bytes() == sent_before


def test_active_keys_only_for_engaged_statuses(tmp_path):
    followup = FollowupStore(tmp_path / "followup.json")
    engaged = {STATUS_IN_PROGRESS: "k1", STATUS_BLOCKED: "k2",
               STATUS_DOCS_CLAIMED: "k3"}
    quiet = {STATUS_NO_SIGNAL: "k4", STATUS_CONFIRMED: "k5",
             STATUS_DISCREPANCY: "k6", STATUS_NEEDS_HUMAN: "k7"}
    for status, key in {**engaged, **quiet}.items():
        followup.get(key)["status"] = status
    assert followup.active_keys() == {"k1", "k2", "k3"}


# -- phase-1 suppression -------------------------------------------------------


def _phase1_worker(tmp_path, followup_store, sent_records):
    def fake_mailer(**kw):
        record = dict(kw)
        record["message_id"] = f"m{len(sent_records)}"
        record["thread_id"] = f"t{len(sent_records)}"
        sent_records.append(record)
        return record

    def fake_search(number):
        return [{"policyNumber": number, "accountId": "41055091",
                 "policyStatus": "Active", "expirationDate": "2026-12-10",
                 "premium": 28936.0}]

    return OverduePolicyChangeReportWorker(
        queue_reader=lambda payload: parse_4359_csv(csv_bytes(row())),
        policy_search=fake_search,
        discussion_lookup=lambda applicant_id: [],
        directory_loader=lambda manifest: build_roster_maps(roster()),
        mailer=fake_mailer,
        sent_store=NotificationStore(tmp_path / "sent.json"),
        followup_store=followup_store,
    )


def test_phase1_suppresses_engaged_change(tmp_path):
    key = key_for()
    followup = FollowupStore(tmp_path / "followup.json")
    followup.set_status(key, STATUS_IN_PROGRESS, TUESDAY, "CSR reply")
    followup.save()
    sent_records = []
    worker = _phase1_worker(tmp_path, FollowupStore(tmp_path / "followup.json"),
                            sent_records)
    result = worker.perform({"action_type": ACTION,
                             "payload": {"manifest_path": "/tmp/m.json"}},
                            idempotency_key="k1")
    assert result.succeeded, result.error
    assert result.destination["suppressed_by_followup"] == 1
    assert result.destination["due_for_nag"] == 0
    assert sent_records == []


def test_phase1_nags_when_no_engagement(tmp_path):
    sent_records = []
    worker = _phase1_worker(tmp_path, None, sent_records)
    result = worker.perform({"action_type": ACTION,
                             "payload": {"manifest_path": "/tmp/m.json"}},
                            idempotency_key="k1")
    assert result.succeeded, result.error
    assert result.destination.get("suppressed_by_followup", 0) == 0
    assert len(sent_records) == 1
    # The thread ID was recorded for phase-2 reply matching.
    store = NotificationStore(tmp_path / "sent.json")
    assert store.thread_to_key() == {"t0": key_for()}
    assert store.first_sent_date(key_for()) == date.today()


def test_notification_store_thread_roundtrip_with_legacy_entry(tmp_path):
    store = NotificationStore(tmp_path / "sent.json")
    # Legacy: plain date string (the 2026-09-27 seed format).
    store._sent["legacy-key"] = "2026-09-27"
    item = {"CSR": "Eimy Ramos", "Policy Number": "S 2391821",
            "created_date": "2026-08-17"}
    store.mark_sent(item, TUESDAY)
    store.record_thread(item, "m1", "thread-9")
    store.save()
    reread = NotificationStore(tmp_path / "sent.json")
    assert reread.thread_to_key() == {"thread-9": key_for()}
    assert reread.first_sent_date("legacy-key") == date(2026, 9, 27)
    assert reread.first_sent_date(key_for()) == TUESDAY
    # Legacy entry still gates the re-nag correctly.
    assert reread.is_due({"CSR": "x", "Policy Number": "y",
                          "created_date": "2020-01-01"}, TUESDAY)
