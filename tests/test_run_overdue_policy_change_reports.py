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
