from datetime import datetime, timezone

from robie_job_engine.gmail_accountability import mailbox_allowlist, summarize_mailbox_threads


AS_OF = datetime(2026, 8, 30, 17, tzinfo=timezone.utc)


def _thread(thread_id: str, sender: str, when_ms: int, **headers):
    values = [{"name": "From", "value": sender}]
    values.extend({"name": name, "value": value} for name, value in headers.items())
    return {
        "id": thread_id,
        "messages": [{
            "internalDate": str(when_ms),
            "payload": {"headers": values},
        }],
    }


def _millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def test_metadata_summary_classifies_reply_owner_without_bodies_or_subjects():
    summary = summarize_mailbox_threads(
        "jackie@streetsmart.insurance",
        [
            _thread("customer-last", "Client <client@example.com>", _millis(datetime(2026, 8, 28, 17, tzinfo=timezone.utc))),
            _thread("employee-last", "Jackie <jackie@streetsmart.insurance>", _millis(datetime(2026, 8, 30, 12, tzinfo=timezone.utc))),
        ],
        as_of=AS_OF,
    )
    assert summary["awaiting_employee"] == 1
    assert summary["awaiting_customer"] == 1
    assert summary["stalled_threads"] == 1
    assert "subject" not in str(summary).lower()
    assert "body" not in str(summary).lower()


def test_mailbox_allowlist_is_explicit_normalized_and_deduplicated():
    assert mailbox_allowlist(" Jackie@StreetSmart.Insurance, jazmin@streetsmart.insurance, jackie@streetsmart.insurance ") == (
        "jackie@streetsmart.insurance",
        "jazmin@streetsmart.insurance",
    )


def test_summary_excludes_internal_automated_and_ambiguous_threads():
    summary = summarize_mailbox_threads(
        "jackie@streetsmart.insurance",
        [
            _thread("internal", "Jazmin <jazmin@streetsmart.insurance>", _millis(AS_OF)),
            _thread("bulk", "news@example.com", _millis(AS_OF), Precedence="bulk"),
            _thread("auto", "no-reply@example.com", _millis(AS_OF)),
            _thread("missing", "", _millis(AS_OF)),
            _thread("customer", "client@example.com", _millis(AS_OF)),
        ],
        as_of=AS_OF,
    )
    assert summary["threads_reviewed"] == 1
    assert summary["awaiting_employee"] == 1
    assert summary["threads_excluded"] == 4
    assert summary["exclusion_counts"] == {"internal": 1, "automated": 2, "ambiguous": 1}


def test_summary_treats_explicit_mailbox_alias_as_employee_reply():
    summary = summarize_mailbox_threads(
        "jackie@streetsmart.insurance",
        [_thread("alias", "service@streetsmart.insurance", _millis(AS_OF))],
        as_of=AS_OF,
        mailbox_aliases=["service@streetsmart.insurance"],
    )
    assert summary["awaiting_customer"] == 1
    assert summary["threads_excluded"] == 0
