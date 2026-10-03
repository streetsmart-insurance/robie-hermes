"""Regression tests for robie_job_engine.outbound_send_guard.

2026-09-14: the no-blind-resend rule was a chat instruction, not code. These
tests pin the Sent-folder duplicate guard that enforces it.
"""

import time
from unittest.mock import Mock

from robie_job_engine.outbound_send_guard import (
    find_recent_sent,
    normalize_subject,
    should_skip_send,
)


def _sent_message(msg_id, to, subject, age_seconds=3600):
    internal_date = str(int((time.time() - age_seconds) * 1000))
    return {
        "id": msg_id,
        "payload": {
            "headers": [
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 14 Sep 2026 20:00:00 -0400"},
            ]
        },
        "internalDate": internal_date,
    }


def _service_with_sent(*messages):
    service = Mock()
    api = service.users.return_value.messages.return_value
    api.list.return_value.execute.return_value = {
        "messages": [{"id": m["id"]} for m in messages]
    }
    by_id = {m["id"]: m for m in messages}

    def fake_get(userId=None, id=None, format=None, metadataHeaders=None):
        get_mock = Mock()
        get_mock.execute.return_value = by_id[id]
        return get_mock

    api.get.side_effect = fake_get
    return service


def test_normalize_subject_strips_reply_prefixes():
    assert normalize_subject("Re: Renewal offer") == normalize_subject("Renewal offer")
    assert normalize_subject("RE:  Fwd:  Renewal   offer") == "renewal offer"
    assert normalize_subject("  Renewal Offer ") == "renewal offer"


def test_exact_duplicate_is_skipped():
    service = _service_with_sent(
        _sent_message("s1", "tracy.paquette@amwins.com", "Re: Maier Solar renewal")
    )
    skip, reason = should_skip_send(
        service, "tracy.paquette@amwins.com", "Re: Maier Solar renewal"
    )
    assert skip is True
    assert "s1" in reason


def test_subject_without_prefix_still_matches():
    service = _service_with_sent(
        _sent_message("s1", "tracy.paquette@amwins.com", "Re: Maier Solar renewal")
    )
    skip, _ = should_skip_send(
        service, "tracy.paquette@amwins.com", "Maier Solar renewal"
    )
    assert skip is True


def test_different_subject_is_not_skipped():
    service = _service_with_sent(
        _sent_message("s1", "tracy.paquette@amwins.com", "Re: Maier Solar renewal")
    )
    skip, _ = should_skip_send(
        service, "tracy.paquette@amwins.com", "Re: Something else entirely"
    )
    assert skip is False


def test_different_recipient_is_not_skipped():
    service = _service_with_sent(
        _sent_message("s1", "tracy.paquette@amwins.com", "Re: Maier Solar renewal")
    )
    skip, _ = should_skip_send(
        service, "someone.else@example.com", "Re: Maier Solar renewal"
    )
    assert skip is False


def test_old_sent_message_outside_window_is_not_skipped():
    service = _service_with_sent(
        _sent_message(
            "s1", "tracy.paquette@amwins.com", "Re: Maier Solar renewal",
            age_seconds=48 * 3600,
        )
    )
    skip, _ = should_skip_send(
        service, "tracy.paquette@amwins.com", "Re: Maier Solar renewal",
        window_hours=24,
    )
    assert skip is False


def test_no_service_fails_open():
    skip, reason = should_skip_send(None, "a@b.com", "Subject")
    assert skip is False
    assert reason == "no matching sent message"


def test_api_error_fails_open():
    service = Mock()
    service.users.return_value.messages.return_value.list.side_effect = RuntimeError("boom")
    skip, _ = should_skip_send(service, "a@b.com", "Subject")
    assert skip is False
    assert find_recent_sent(service, "a@b.com", "Subject") == []


def test_metadata_scope_403_retries_without_query_and_finds_the_sent_message():
    """gmail.metadata 403s on q=. Listing SENT without q still catches a duplicate."""
    message = _sent_message("s9", "one@streetsmart.insurance", "Action required")
    calls = []

    class Messages:
        def list(self, **kwargs):
            calls.append(dict(kwargs))
            if "labelIds" in kwargs or "q" in kwargs:
                raise RuntimeError(
                    "HttpError 403: Metadata scope does not support the q parameter"
                )
            result = Mock()
            result.execute.return_value = {"messages": [{"id": "s9"}]}
            return result

        def get(self, **kwargs):
            result = Mock()
            result.execute.return_value = message
            return result

    service = Mock()
    service.users.return_value.messages.return_value = Messages()
    skip, reason = should_skip_send(
        service, "one@streetsmart.insurance", "Action required"
    )
    assert skip is True
    assert "s9" in reason
    assert calls[0].get("labelIds") == ["SENT"]
    assert "q" not in calls[0]
    assert "q" not in calls[1]
    assert "labelIds" not in calls[1]


def _plain_part(body):
    import base64

    return {
        "mimeType": "text/plain",
        "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")},
    }


def _thread_service(sent_message, inbound_bodies):
    service = _service_with_sent(sent_message)
    messages = [
        {
            "id": f"in-{index}",
            "labelIds": ["INBOX"],
            "payload": _plain_part(body),
        }
        for index, body in enumerate(inbound_bodies)
    ]
    service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "messages": messages
    }
    return service


def test_new_body_in_the_same_thread_is_a_correction():
    service = _thread_service(
        _sent_message("s1", "jake@streetsmart.insurance", "Re: Astra Gold"),
        [
            "Create the Fortegra agreement.",
            "Use Fortegra Specialty in Jacksonville.",
        ],
    )
    skip, reason = should_skip_send(
        service,
        "jake@streetsmart.insurance",
        "Re: Astra Gold",
        incoming_body="Use Fortegra Specialty in Jacksonville.",
        thread_id="thread-1",
        expected_mailbox="",
    )
    assert skip is False
    assert "correction" in reason


def test_same_body_in_the_thread_still_skips():
    body = "Create the Fortegra agreement."
    service = _thread_service(
        _sent_message("s1", "jake@streetsmart.insurance", "Re: Astra Gold"),
        [body, body],
    )
    skip, reason = should_skip_send(
        service,
        "jake@streetsmart.insurance",
        "Re: Astra Gold",
        incoming_body=body,
        thread_id="thread-1",
    )
    assert skip is True
    assert "s1" in reason


def test_current_message_alone_does_not_count_as_new_content():
    body = "Create the Fortegra agreement."
    service = _thread_service(
        _sent_message("s1", "jake@streetsmart.insurance", "Re: Astra Gold"),
        [body],
    )
    skip, reason = should_skip_send(
        service,
        "jake@streetsmart.insurance",
        "Re: Astra Gold",
        incoming_body=body,
        thread_id="thread-1",
    )
    assert skip is True
    assert "s1" in reason


def test_wrong_mailbox_does_not_count_as_an_empty_sent_folder():
    service = Mock()
    profile = service.users.return_value.getProfile.return_value.execute
    profile.return_value = {"emailAddress": "someone.else@streetsmart.insurance"}
    skip, reason = should_skip_send(
        service,
        "one@streetsmart.insurance",
        "Action required",
        expected_mailbox="robie@streetsmart.insurance",
    )
    assert skip is False
    assert reason == "sent check skipped: wrong mailbox"
    service.users.return_value.messages.return_value.list.assert_not_called()
