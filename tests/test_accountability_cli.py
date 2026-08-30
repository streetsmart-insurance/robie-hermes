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
