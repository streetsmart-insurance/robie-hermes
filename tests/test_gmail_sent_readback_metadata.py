"""Gmail metadata-scope sent read-back stays an ID existence check."""

from __future__ import annotations

from pathlib import Path

from robie_job_engine.accountability_delivery import verify_delivery_receipts


def test_sent_readback_uses_get_by_id_and_documents_metadata_q_limit():
    source = (
        Path(__file__).resolve().parents[1]
        / "robie_job_engine"
        / "accountability_delivery.py"
    ).read_text()
    block = source.split("def verify_delivery_receipts", 1)[1]
    executable = block.split('"""', 2)[-1]
    assert "messages().get" in executable
    assert "exists_in_sent_mailbox" in executable
    assert "q=" not in executable
    assert "cannot use" in source
    assert "messages.list?q=" in source


def test_verify_delivery_receipts_requires_id_existence(monkeypatch):
    calls = []

    class _Messages:
        def get(self, **kwargs):
            calls.append(kwargs)
            return self

        def execute(self):
            return {"id": calls[-1]["id"]}

    class _Users:
        def messages(self):
            return _Messages()

    class _Gmail:
        def users(self):
            return _Users()

    monkeypatch.setenv("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "sa@test.invalid")
    monkeypatch.setattr(
        "robie_job_engine.accountability_delivery._delegated_gmail_sender",
        lambda account, sender: _Gmail(),
    )
    ok, observed = verify_delivery_receipts(
        [{"kind": "gmail", "sender": "robie@streetsmart.insurance", "message_id": "abc123"}]
    )
    assert ok is True
    assert observed == [{"kind": "gmail", "id": "abc123", "exists_in_sent_mailbox": True}]
    assert calls == [{"userId": "me", "id": "abc123", "format": "minimal"}]
