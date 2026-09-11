import io
import json
import sqlite3
import sys
from pathlib import Path
import pytest
from scripts.verify_engine_working import main

def test_missing_db_path():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2

def test_production_path_rejected_without_flag(tmp_path, capsys):
    db_file = tmp_path / "prod-jobs.db"
    db_file.touch()
    code = main(["--db-path", str(db_file)])
    assert code == 2
    captured = capsys.readouterr().out
    assert "environment: PRODUCTION" in captured
    assert "FAIL  database path does not contain '-test'" in captured

def test_production_path_allowed_with_flag(tmp_path, capsys):
    db_file = tmp_path / "prod-jobs.db"
    _init_db(db_file)
    baseline_file = tmp_path / "baseline.json"
    code = main(["--db-path", str(db_file), "--allow-production", "--baseline", "--state", str(baseline_file)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "environment: PRODUCTION" in captured
    assert "BASELINE RECORDED" in captured

def test_test_path_allowed_without_flag(tmp_path, capsys):
    db_file = tmp_path / "jobs-test.db"
    _init_db(db_file)
    baseline_file = tmp_path / "baseline.json"
    code = main(["--db-path", str(db_file), "--baseline", "--state", str(baseline_file)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "environment: TEST" in captured
    assert "BASELINE RECORDED" in captured

def test_nonexistent_file_rejected(tmp_path, capsys):
    db_file = tmp_path / "nonexistent-test.db"
    code = main(["--db-path", str(db_file)])
    assert code == 2
    captured = capsys.readouterr().out
    assert "FAIL  not a file:" in captured

def test_no_baseline_file_reports_fail(tmp_path, capsys):
    db_file = tmp_path / "jobs-test.db"
    _init_db(db_file)
    baseline_file = tmp_path / "nonexistent_baseline.json"
    code = main(["--db-path", str(db_file), "--state", str(baseline_file)])
    assert code == 2
    captured = capsys.readouterr().out
    assert "FAIL  no baseline" in captured

def test_verdict_blocked_when_no_new_jobs(tmp_path, capsys):
    db_file = tmp_path / "jobs-test.db"
    _init_db(db_file)
    baseline_file = tmp_path / "baseline.json"
    main(["--db-path", str(db_file), "--baseline", "--state", str(baseline_file)])
    code = main(["--db-path", str(db_file), "--state", str(baseline_file)])
    assert code == 3
    captured = capsys.readouterr().out
    assert "BLOCKED" in captured

def test_verdict_pass_when_completed_and_evidence(tmp_path, capsys):
    db_file = tmp_path / "jobs-test.db"
    _init_db(db_file)
    baseline_file = tmp_path / "baseline.json"
    main(["--db-path", str(db_file), "--baseline", "--state", str(baseline_file)])
    
    # insert completed job and evidence
    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO jobs (id, action_type, completed_at, updated_at) VALUES ('j1', 'hermes.google_chat_task', '2026-09-11', '2026-09-11')")
    conn.execute("INSERT INTO verification_evidence (id) VALUES ('e1')")
    conn.commit()
    conn.close()

    code = main(["--db-path", str(db_file), "--state", str(baseline_file)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "PASS" in captured
    assert "hermes.google_chat_task" in captured
    assert "database:" in captured
    assert "environment: TEST" in captured

def _init_db(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, action_type TEXT, completed_at TEXT, last_error TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE verification_evidence (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
