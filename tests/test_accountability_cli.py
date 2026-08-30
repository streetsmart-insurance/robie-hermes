from pathlib import Path

from robie_job_engine.accountability_cli import main


def test_weekly_cli_without_sources_fails_closed(capsys):
    assert main(["weekly", "--as-of", "2026-08-30T17:00:00+00:00"]) == 0
    output = capsys.readouterr().out
    assert "Inbound Answer Rate: UNVERIFIED" in output
    assert "Department Tracker Exceptions: UNVERIFIED" in output
    assert "51.8%" not in output


def test_weekly_cli_includes_tracker_source_row(tmp_path: Path, capsys):
    bor = tmp_path / "bor.csv"
    bor.write_text(
        "Ezlynx URL/Client Name,Date Submitted,Effective Date,Status,Additinal notes,Owner\n"
        "Example Account,07/01/2026,08/01/2026,Pending Download,Waiting,Example Owner\n",
        encoding="utf-8",
    )
    assert main(["weekly", "--as-of", "2026-08-30T17:00:00+00:00", "--tracker", f"bor={bor}"]) == 0
    output = capsys.readouterr().out
    assert "Department Tracker Exceptions: 1" in output
    assert "Example Account" in output
    assert "source row 2" in output


def test_weekly_cli_includes_inactive_sales_center_opportunity(tmp_path: Path, capsys):
    sales = tmp_path / "sales.csv"
    sales.write_text(
        "Opportunity ID,Account Name,Producer,Stage,Created Date,Last Activity,Last Note\n"
        "O-1,Inactive Account,Example Producer,Quoting,2026-07-01,2026-08-20,Followed up\n",
        encoding="utf-8",
    )
    assert main([
        "weekly",
        "--as-of", "2026-08-30T17:00:00+00:00",
        "--sales", str(sales),
        "--sales-untouched-days", "5",
    ]) == 0
    output = capsys.readouterr().out
    assert "Sales Opportunities With No Recent Touch: 1" in output
    assert "Inactive Account" in output
    assert "Example Producer" in output
    assert "source row 2" in output


def test_weekly_cli_flags_zero_workload_attestation_that_conflicts_with_gmail(tmp_path: Path, capsys):
    tracker = tmp_path / "workload.csv"
    tracker.write_text(
        "Names (39) Members,Email,08/28/2026\n"
        "Jackie Arriola,jackie@streetsmart.insurance,0\n",
        encoding="utf-8",
    )
    email = tmp_path / "email.json"
    email.write_text(
        '{"source_status":"available","stalled_threads":8,"by_employee":{"jackie@streetsmart.insurance":{"stalled_threads":8}}}',
        encoding="utf-8",
    )
    assert main([
        "weekly", "--as-of", "2026-08-30T17:00:00+00:00",
        "--email-json", str(email),
        "--tracker", f"voicemail_email={tracker}",
    ]) == 0
    output = capsys.readouterr().out
    assert "ATTESTATION MISMATCHES" in output
    assert "reported 0 unresolved" in output
    assert "evidence shows 8" in output


def test_daily_cli_reports_unique_queue_offers_and_member_pickups(tmp_path: Path, capsys):
    calls = tmp_path / "calls.csv"
    calls.write_text(
        "Call ID,Direction,From,To,Result,Date/Time,Queue Name,Employee,Answered By,Queue Wait Time\n"
        "q-1,Inbound,5551110001,9006,Missed,08/30/2026 10:00 AM,Commercial,Jackie,,00:00:20\n"
        "q-1,Inbound,5551110001,9006,Call connected,08/30/2026 10:00 AM,Commercial,Erika,Erika,00:00:25\n"
        "q-2,Inbound,5551110002,9006,Refused,08/30/2026 11:00 AM,Commercial,Jackie,,00:03:10\n",
        encoding="utf-8",
    )
    assert main(["daily", "--as-of", "2026-08-30T17:00:00+00:00", "--ringcentral", str(calls)]) == 0
    output = capsys.readouterr().out
    assert "CALL QUEUE PICKUP & MEMBER LEGS" in output
    assert "| Commercial | 2 | 1 | 1 | 50.0% | 190s |" in output
    assert "Commercial pickups — Erika: 1" in output
    assert "Commercial missed/refused member legs — Jackie: 2" in output
