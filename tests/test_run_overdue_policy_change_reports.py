"""Tests for robie_job_engine/run_overdue_policy_change_reports.py.

Fake clients only: no network, no secrets, no real sends. The full dry run
against live data happens on hermes-poc-01 (see the deploy runbook).
"""

import json
from datetime import date

import pytest

from robie_job_engine.overdue_policy_change_reports import (
    NotificationStore,
    notification_key,
)
from robie_job_engine.run_overdue_policy_change_reports import (
    DryRunNotificationStore,
    build_parser,
    dry_run_mailer_factory,
    seed_sent_store,
)


def test_default_mode_is_dry_run():
    args = build_parser().parse_args(["--manifest", "/tmp/m.json"])
    assert args.mode == "dry-run"


def test_live_mode_is_explicit():
    args = build_parser().parse_args(
        ["--mode", "live", "--manifest", "/tmp/m.json"])
    assert args.mode == "live"


def test_dry_run_mailer_records_without_sending():
    captured = []
    mailer = dry_run_mailer_factory(captured)
    receipt = mailer(to=["a@streetsmart.insurance"], cc=["b@streetsmart.insurance"],
                     subject="s", text_body="t", html_body="<p>t</p>")
    assert len(captured) == 1
    assert captured[0]["kind"] == "dry-run"
    assert captured[0]["destination"] == ["a@streetsmart.insurance"]
    assert receipt["message_id"].startswith("dry-run-")


def test_dry_run_store_never_persists(tmp_path):
    store_path = tmp_path / "sent.json"
    store = DryRunNotificationStore(store_path)
    item = {"CSR": "Eimy Ramos", "Policy Number": "S 2391821",
            "created_date": "2026-08-17"}
    assert store.is_due(item, date(2026, 9, 27))
    store.mark_sent(item, date(2026, 9, 27))
    store.save()
    assert not store_path.exists()
    # ...but the in-memory mark still suppresses a re-nag within the run,
    # so a dry run mirrors what the live run would do.
    assert not store.is_due(item, date(2026, 9, 27))


def test_dry_run_store_reads_real_history(tmp_path):
    store_path = tmp_path / "sent.json"
    real = NotificationStore(store_path)
    item = {"CSR": "Eimy Ramos", "Policy Number": "S 2391821",
            "created_date": "2026-08-17"}
    real.mark_sent(item, date(2026, 9, 27))
    real.save()
    dry = DryRunNotificationStore(store_path)
    assert not dry.is_due(item, date(2026, 9, 27))


def test_seed_sent_store_marks_manual_emails(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([
        {"csr": "Lenin Perdomo", "policy_number": "13WECAT1F8T",
         "created_date": "2026-09-04", "sent_date": "2026-09-27"},
    ]))
    store_path = tmp_path / "sent.json"
    result = seed_sent_store(str(seed), str(store_path))
    assert result["seeded"][0]["csr"] == "Lenin Perdomo"
    store = NotificationStore(store_path)
    item = {"CSR": "Lenin Perdomo", "Policy Number": "13WECAT1F8T",
            "created_date": "2026-09-04"}
    assert notification_key("Lenin Perdomo", "13WECAT1F8T", "2026-09-04") in store._sent
    assert not store.is_due(item, date(2026, 9, 27))
    # Re-nag is still allowed after RENAG_DAYS (7).
    assert store.is_due(item, date(2026, 10, 4))


def test_seed_sent_store_rejects_empty(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text("[]")
    with pytest.raises(Exception):
        seed_sent_store(str(seed), str(tmp_path / "sent.json"))


def test_seed_sent_store_rejects_incomplete_entry(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([{"csr": "Lenin Perdomo"}]))
    with pytest.raises(Exception):
        seed_sent_store(str(seed), str(tmp_path / "sent.json"))


def _fake_live_run(monkeypatch, tmp_path, verifier_factory):
    """Patch the runner's worker so --live runs without network; returns (code, evidence, calls)."""
    import robie_job_engine.run_overdue_policy_change_reports as runner

    class FakeResult:
        succeeded = True
        error = ""
        hold_status = None
        destination = {"delivery_receipts": [], "csr_count": 0}
        detail = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def perform(self, job, idempotency_key=None):
            return FakeResult()

    monkeypatch.setattr(runner, "OverduePolicyChangeReportWorker", FakeWorker)
    args = runner.build_parser().parse_args(
        ["--mode", "live", "--manifest", "/tmp/m.json",
         "--sent-store", str(tmp_path / "sent.json"),
         "--evidence-out", str(tmp_path / "ev.json")])
    return runner.run(args, verifier_factory=verifier_factory)


def test_live_run_calls_verifier_and_records_result(tmp_path, monkeypatch):
    class FakeEvidence:
        expected = 1
        observed = 1
        method = "fake-readback"
        captured_at = "2026-09-27T00:00:00+00:00"

    class FakeVerification:
        verified = True
        error = ""
        evidence = FakeEvidence()

    calls = []

    class FakeVerifier:
        def verify(self, job, action):
            calls.append((job, action))
            return FakeVerification()

    code, evidence = _fake_live_run(monkeypatch, tmp_path, FakeVerifier)
    assert code == 0
    assert len(calls) == 1
    assert evidence["verification"]["verified"] is True
    assert evidence["verification"]["method"] == "fake-readback"


def test_live_run_verifier_failure_is_recorded_not_fatal(tmp_path, monkeypatch):
    class BoomVerifier:
        def verify(self, job, action):
            raise RuntimeError("gmail read-back down")

    code, evidence = _fake_live_run(monkeypatch, tmp_path, BoomVerifier)
    assert code == 0  # the sends happened; verification is evidence, not a retry trigger
    assert evidence["verification"]["verified"] is False
    assert "UNVERIFIED" in evidence["verification"]["error"]


def test_dry_run_skips_verifier(tmp_path, monkeypatch):
    import robie_job_engine.run_overdue_policy_change_reports as runner

    class FakeResult:
        succeeded = True
        error = ""
        hold_status = None
        destination = {"delivery_receipts": [], "csr_count": 0}
        detail = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def perform(self, job, idempotency_key=None):
            return FakeResult()

    def boom():
        raise AssertionError("verifier must not run in dry-run")

    monkeypatch.setattr(runner, "OverduePolicyChangeReportWorker", FakeWorker)
    args = runner.build_parser().parse_args(
        ["--mode", "dry-run", "--manifest", "/tmp/m.json",
         "--sent-store", str(tmp_path / "sent.json"),
         "--evidence-out", str(tmp_path / "ev.json")])
    code, evidence = runner.run(args, verifier_factory=boom)
    assert code == 0
    assert evidence["verification"]["skipped"] is True


def _capture_payload(monkeypatch, tmp_path, extra_args):
    """Run with a fake worker; return the job payload the worker received."""
    import robie_job_engine.run_overdue_policy_change_reports as runner

    captured = {}

    class FakeResult:
        succeeded = True
        error = ""
        hold_status = None
        destination = {"delivery_receipts": [], "csr_count": 0}
        detail = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def perform(self, job, idempotency_key=None):
            captured.update(job["payload"])
            return FakeResult()

    class NoopVerifier:
        def verify(self, job, action):
            raise AssertionError("not used")

    monkeypatch.setattr(runner, "OverduePolicyChangeReportWorker", FakeWorker)
    args = runner.build_parser().parse_args(
        ["--mode", "dry-run", "--manifest", "/tmp/m.json",
         "--sent-store", str(tmp_path / "sent.json"),
         "--evidence-out", str(tmp_path / "ev.json"), *extra_args])
    runner.run(args, verifier_factory=NoopVerifier)
    return captured


def test_report_sender_domains_default_covers_appliedsystems(tmp_path, monkeypatch):
    # The real daily 4359 CSV arrives from DoNotReply@appliedsystems.com
    # (proven on hermes-poc-01 2026-09-27); the default allowlist must accept it.
    payload = _capture_payload(monkeypatch, tmp_path, [])
    assert "appliedsystems.com" in payload["report_allowed_sender_domains"]
    assert "ezlynx.com" in payload["report_allowed_sender_domains"]


def test_report_sender_domains_flag_overrides(tmp_path, monkeypatch):
    payload = _capture_payload(
        monkeypatch, tmp_path, ["--report-allowed-sender-domains", "example.com"])
    assert payload["report_allowed_sender_domains"] == ["example.com"]


def test_report_sender_domains_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_4359_REPORT_ALLOWED_SENDER_DOMAINS", "a.com, b.com")
    payload = _capture_payload(monkeypatch, tmp_path, [])
    assert payload["report_allowed_sender_domains"] == ["a.com", "b.com"]
