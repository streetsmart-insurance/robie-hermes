"""Always-on Ascend notice review runner: read-only, propose-only, incremental."""

from __future__ import annotations

import json
import os
import stat
from datetime import date

import pytest

from robie_job_engine import ascend_notice_review_runner as runner
from robie_job_engine.ascend_notice_discovery import ReviewQueue

from test_ascend_notice_discovery import Gmail, msg

MAILBOX = "robie@streetsmart.insurance"


class WriteTrapGmail(Gmail):
    """Like #751's fake, plus every write method the Gmail API has."""

    def batchModify(self, **kw):
        raise AssertionError("no Gmail writes")

    def trash(self, **kw):
        raise AssertionError("no Gmail writes")

    def delete(self, **kw):
        raise AssertionError("no Gmail writes")


def gmail(n=3, token_pages=False):
    messages = {str(i): msg(str(i)) for i in range(n)}
    pages = {"first": {"messages": [{"id": str(i)} for i in range(n)]}}
    return WriteTrapGmail(pages, messages)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBIE_ENV", "TEST")
    monkeypatch.setenv(runner.ENABLE_ENV, "1")
    monkeypatch.setenv(runner.STATE_DIR_ENV, str(tmp_path / "state"))


def run(service, **kw):
    args = dict(service_factory=lambda mailbox: service, mailboxes=[MAILBOX],
                today=date(2026, 10, 4), first_start="2026-10-01")
    args.update(kw)
    return runner.run_once(**args)


def test_first_run_scans_from_explicit_start_and_sets_checkpoint(tmp_path):
    g = gmail()
    report = run(g)
    mb = report["mailboxes"][MAILBOX]
    assert report["status"] == "ok" and mb["complete"]
    assert mb["window"] == ["2026-10-01", "2026-10-05"]
    assert mb["checkpoint"] == "2026-10-04"
    assert mb["review"] == 3 and mb["destination_writes"] == 0 and mb["gmail_label_changes"] == 0
    query = g.calls[0][1]["q"]
    assert "after:2026-10-01" in query and "before:2026-10-05" in query


def test_next_run_overlaps_and_does_not_duplicate_queue_rows():
    g = gmail()
    run(g)
    report = run(g, today=date(2026, 10, 5))
    assert report["mailboxes"][MAILBOX]["window"] == ["2026-10-02", "2026-10-06"]
    assert report["queue_rows"] == 3


def test_incomplete_scan_keeps_old_checkpoint(tmp_path):
    run(gmail())
    broken = WriteTrapGmail({"first": {"messages": [{"id": "m"}]}}, {"m": TimeoutError()})
    report = run(broken, today=date(2026, 10, 6))
    assert report["status"] == "incomplete"
    assert report["mailboxes"][MAILBOX]["checkpoint"] == "2026-10-04"


def test_no_checkpoint_and_no_start_refuses_implicit_backfill():
    with pytest.raises(RuntimeError, match="implicit backfill"):
        run(gmail(), first_start=None)


def test_items_are_propose_only():
    run(gmail())
    q = ReviewQueue(os.path.join(os.environ[runner.STATE_DIR_ENV], "review_queue.db"))
    rows = q.rows()
    q.close()
    for row in rows:
        item = row["item"]
        assert item["proposal"]["propose_only"] is True
        assert item["may_file"] is False and item["may_send_task"] is False
        assert item["source_verified"] is False
    assert "past-due" in rows[0]["item"]["proposal"]["summary"]


def test_read_only_wrapper_refuses_every_non_read_call():
    ro = runner.ReadOnlyGmail(gmail())
    for name in ("modify", "send", "trash", "batchModify", "delete", "insert", "import_"):
        with pytest.raises(runner.RefusedGmailCall):
            getattr(ro.users().messages(), name)
    with pytest.raises(runner.RefusedGmailCall):
        ro.users().labels()
    with pytest.raises(runner.RefusedGmailCall):
        ro.users().messages().get(userId="me", id="0", format="raw")
    assert ro.users().messages().get(userId="me", id="0", format="full").execute()["id"] == "0"


def test_refuses_outside_test_and_respects_disable_flag(monkeypatch):
    monkeypatch.setenv(runner.ENABLE_ENV, "0")
    assert run(gmail())["status"] == "disabled"
    monkeypatch.setenv("ROBIE_ENV", "PRODUCTION")
    with pytest.raises(RuntimeError, match="TEST_ONLY"):
        run(gmail())


def test_second_concurrent_run_is_skipped():
    state = runner._state_dir()
    with runner.single_instance(state / "runner.lock") as first:
        assert first
        assert run(gmail())["status"] == "skipped"


def test_state_files_are_private():
    run(gmail())
    state = runner._state_dir()
    for name in ("checkpoints.json", "status.json", "review_queue.db"):
        assert stat.S_IMODE(os.stat(state / name).st_mode) == 0o600, name
    status = json.loads((state / "status.json").read_text())
    assert status["mode"] == "propose_only"


def test_one_mailbox_failure_does_not_stop_the_others():
    good = gmail()

    def factory(mailbox):
        if mailbox == "bad@streetsmart.insurance":
            raise RuntimeError("delegation missing")
        return good

    report = runner.run_once(service_factory=factory, mailboxes=["bad@streetsmart.insurance", MAILBOX],
                             today=date(2026, 10, 4), first_start="2026-10-01")
    assert report["status"] == "incomplete"
    assert report["mailboxes"][MAILBOX]["complete"] is True
    assert report["mailboxes"]["bad@streetsmart.insurance"]["errors"] == ["run:RuntimeError"]


def test_validation_script_prints_counts_only(capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "ascend_notice_mailbox_validate.py"
    spec = importlib.util.spec_from_file_location("validate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    code = mod.main(["--mailbox", MAILBOX, "--delegation-service-account", "sa@example.com"],
                    service_factory=lambda sa, mb: gmail())
    out = capsys.readouterr().out
    assert code == 0 and "MAILBOX VALIDATION PASSED" in out
    assert "Past due payment for Example LLC" not in out  # no subjects
    assert "notice@useascend.com" not in out  # no senders


def test_units_are_test_host_only_and_read_only():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
    service = (root / "robie-ascend-notice-review-test.service").read_text()
    assert "ConditionHost=hermes-test-01" in service
    assert "ROBIE_ENV=TEST" in service
    assert "ReadWritePaths=/opt/streetsmart-hermes-test/ascend-notice-review" in service
    assert "modify" not in service.lower()
