from pathlib import Path

from robie_job_engine import accountability_delivery


def test_chat_delivery_returns_receipt(monkeypatch, tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text("verified report", encoding="utf-8")
    monkeypatch.setattr(
        accountability_delivery,
        "post_as_chat_app",
        lambda space, text: {"name": f"{space}/messages/123"},
    )
    receipts = accountability_delivery.deliver_report(
        report,
        mode="daily",
        delivery={"chat_spaces": ["spaces/team-leads"]},
        environment={},
    )
    assert receipts == [{
        "kind": "google_chat",
        "destination": "spaces/team-leads",
        "message_name": "spaces/team-leads/messages/123",
        "sequence": 1,
    }]


def test_long_chat_report_is_split_without_losing_text(monkeypatch, tmp_path: Path):
    report = tmp_path / "long.md"
    original = "\n".join(f"row {index}: " + ("x" * 100) for index in range(80))
    report.write_text(original, encoding="utf-8")
    posted = []
    monkeypatch.setattr(
        accountability_delivery,
        "post_as_chat_app",
        lambda space, text: posted.append(text) or {"name": f"{space}/messages/{len(posted)}"},
    )
    receipts = accountability_delivery.deliver_report(
        report, mode="weekly", delivery={"chat_spaces": ["spaces/team-leads"]}, environment={}
    )
    assert len(receipts) > 1
    assert all(len(item) <= 3500 for item in posted)
    assert "\n".join(posted) == original
