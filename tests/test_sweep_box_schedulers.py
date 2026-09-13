"""Regression tests for scripts/sweep_box_schedulers_readonly.py.

Covers the two bugs found in sweep run 34756330819:
1. _execstart_target resolved argv[0] (the python interpreter) instead of
   the script, because a single argv[] token held the whole command line
   and the old `[^\\s;}]+` capture stopped at the first space.
2. _redact missed `Environment=ROBIE_ASCEND_API_KEY=...` because the
   sensitive word was embedded after an underscore (no \\b word boundary),
   leaking a live key into the sweep artifact.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
    path = (Path(__file__).resolve().parent.parent / "scripts"
            / "sweep_box_schedulers_readonly.py")
    spec = importlib.util.spec_from_file_location(
        "sweep_box_schedulers_readonly", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sweep():
    return _load_module()


# Exact ExecStart shape from run 34756330819: one argv[] token holds the
# interpreter AND the script separated by a space.
STRUCTURED = (
    "{ path=/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python ; "
    "argv[]=/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python "
    "/opt/streetsmart-hermes/.hermes/scripts/robie_email_agent.py ; "
    "ignore_errors=no ; start_time=[Sun 2026-09-13 08:09:28 EDT] ; "
    "stop_time=[Sun 2026-09-13 08:09:29 EDT] ; pid=949089 ; "
    "code=exited ; status=0 }"
)


def test_execstart_prefers_script_over_interpreter(sweep):
    assert sweep._execstart_target(STRUCTURED) == (
        "/opt/streetsmart-hermes/.hermes/scripts/robie_email_agent.py")


def test_execstart_plain_form_skips_flags(sweep):
    assert sweep._execstart_target(
        "ExecStart=/usr/bin/python3 -u /opt/x/agent.py --verbose"
    ) == "/opt/x/agent.py"


def test_execstart_takes_last_py_argument(sweep):
    assert sweep._execstart_target(
        "ExecStart=/usr/bin/python3 /opt/x/first.py /opt/x/second.py"
    ) == "/opt/x/second.py"


def test_execstart_binary_falls_back_to_argv0(sweep):
    assert sweep._execstart_target(
        "ExecStart=/usr/bin/mydaemon --daemon") == "/usr/bin/mydaemon"


def test_execstart_empty_returns_empty(sweep):
    assert sweep._execstart_target("") == ""


def test_redact_masks_embedded_api_key(sweep):
    line = ("Environment=ROBIE_ASCEND_API_KEY="
            "Yoo9IziU9MBws0GuzaXww4t1XWqrxrjynaGE0-vltUo")
    out = sweep._redact(line)
    assert "Yoo9IziU9MBws0GuzaXww4t1XWqrxrjynaGE0-vltUo" not in out
    assert out == "Environment=ROBIE_ASCEND_API_KEY=<redacted>"


def test_redact_masks_token_and_password_variants(sweep):
    assert sweep._redact("auth_token=abc123") == "auth_token=<redacted>"
    assert sweep._redact("DB_PASSWORD: s3cret") == "DB_PASSWORD: <redacted>"


def test_redact_leaves_benign_lines_alone(sweep):
    assert (sweep._redact("Environment=ROBIE_ENV=PRODUCTION")
            == "Environment=ROBIE_ENV=PRODUCTION")
    assert sweep._redact("no secrets here") == "no secrets here"


# ROBIE_ZIP_LOAD_PATH launcher indirection (found in sweep run 34758485741):
# the deployed watcher script is a 9-line launcher that exec()s the real
# agent from <root>/releases/current/<load_path>.
LAUNCHER_9_LINES = """\
from pathlib import Path
ROBIE_ZIP_LOAD_PATH = "scripts/robie_email_agent.py"
_root = Path(__file__).resolve().parents[2]
_src = _root / "releases/current" / ROBIE_ZIP_LOAD_PATH


def _main():
    exec(compile(_src.read_text(), str(_src), "exec"))
"""


def test_loader_indirection_resolves_releases_current(sweep):
    assert sweep._resolve_loader_target(
        LAUNCHER_9_LINES,
        "/opt/streetsmart-hermes/.hermes/scripts/robie_email_agent.py"
    ) == ("/opt/streetsmart-hermes/releases/current/"
          "scripts/robie_email_agent.py")


def test_loader_indirection_parents_up_1(sweep):
    assert sweep._resolve_loader_target(
        LAUNCHER_9_LINES,
        "/opt/streetsmart-hermes/.hermes/scripts/robie_email_agent.py",
        parents_up=1,
    ) == ("/opt/streetsmart-hermes/.hermes/releases/current/"
          "scripts/robie_email_agent.py")


def test_loader_indirection_single_quotes(sweep):
    assert sweep._resolve_loader_target(
        "x = 1\nROBIE_ZIP_LOAD_PATH = 'agent/run.py'\n",
        "/a/b/c/launcher.py",
    ) == "/a/releases/current/agent/run.py"


def test_loader_rejects_long_files(sweep):
    padded = LAUNCHER_9_LINES + "".join(f"# pad {i}\n" for i in range(60))
    assert sweep._resolve_loader_target(padded, "/x/y/launcher.py") == ""


def test_loader_rejects_missing_marker(sweep):
    assert sweep._resolve_loader_target(
        "print('hello')\n", "/x/y/launcher.py") == ""


def test_loader_rejects_shallow_path(sweep):
    assert sweep._resolve_loader_target(
        LAUNCHER_9_LINES, "/launcher.py") == ""


# Section 10 helper: extract .json cron definitions from find -printf
# listing lines ("<path>\t<size>\t<mtime>").
FIND_LINES = [
    "/home/streetsmart-hermes/.hermes/cron/daily.json\t123 bytes\t2026-09-13 08:10",
    "/home/streetsmart-hermes/.hermes/cron/notes.txt\t45 bytes\t2026-09-12 07:00",
    "/opt/streetsmart-hermes/.hermes/cron/hourly audit.json\t200 bytes\t2026-09-13 09:00",
]


def test_json_files_in_listing_extracts_json_paths(sweep):
    assert sweep._json_files_in_listing(FIND_LINES) == [
        "/home/streetsmart-hermes/.hermes/cron/daily.json",
        "/opt/streetsmart-hermes/.hermes/cron/hourly audit.json",
    ]


def test_json_files_in_listing_empty_input(sweep):
    assert sweep._json_files_in_listing([]) == []


def test_json_files_in_listing_ignores_non_json(sweep):
    assert sweep._json_files_in_listing(
        ["  <absent or empty>", "/x/cron/job.yaml\t10 bytes\t2026-01-01 00:00"]
    ) == []


# Section 11 helper: pick robie-*/hermes-* units out of
# `systemctl list-unit-files --type=timer,service` output.
UNIT_FILES_SAMPLE = """\
UNIT FILE                              STATE
hermes-email-watcher.service           enabled
hermes-email-watcher.timer             enabled
robie-production-preflight.service     enabled
robie-production-preflight.timer       enabled
robie-ascend-sync.timer                disabled
robie-health-check.service             static
sshd.service                           enabled
cron.service                           enabled
"""


def test_robie_hermes_units_picks_robie_and_hermes(sweep):
    assert sweep._robie_hermes_units(UNIT_FILES_SAMPLE) == [
        "hermes-email-watcher.service",
        "hermes-email-watcher.timer",
        "robie-production-preflight.service",
        "robie-production-preflight.timer",
        "robie-ascend-sync.timer",
        "robie-health-check.service",
    ]


def test_robie_hermes_units_empty_input(sweep):
    assert sweep._robie_hermes_units("") == []


def test_robie_hermes_units_skips_header_and_unrelated(sweep):
    assert sweep._robie_hermes_units(
        "UNIT FILE  STATE\nsshd.service  enabled\n") == []


def test_robie_hermes_units_dedupes_repeated_rows(sweep):
    assert sweep._robie_hermes_units(
        "robie-health-check.timer enabled\n"
        "robie-health-check.timer enabled\n") == [
            "robie-health-check.timer"]


# --- Section 12: ROBIE Job Engine scheduled-jobs DB helpers ---

def _make_job_db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE schedules (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, action_type TEXT NOT NULL,
        payload_json TEXT NOT NULL, interval_minutes INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, next_run_at TEXT NOT NULL,
        last_run_at TEXT, last_job_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE scheduled_jobs (
        id TEXT PRIMARY KEY, task_name TEXT NOT NULL UNIQUE,
        action_type TEXT NOT NULL, parameters_json TEXT NOT NULL,
        cron_spec TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'America/New_York',
        target_ref TEXT, enabled INTEGER NOT NULL DEFAULT 1,
        next_run_at TEXT NOT NULL, last_run_at TEXT, last_job_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    return conn


def test_resolve_job_db_path_uses_env(sweep):
    assert sweep._resolve_job_db_path("/data/jobs.db") == "/data/jobs.db"


def test_resolve_job_db_path_falls_back_to_default(sweep):
    assert sweep._resolve_job_db_path("") == (
        "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db")
    assert sweep._resolve_job_db_path("   ") == (
        "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db")


def test_dump_job_tables_hourly_rows_first(sweep):
    conn = _make_job_db()
    conn.execute(
        "INSERT INTO schedules (id, name, action_type, payload_json,"
        " interval_minutes, enabled, next_run_at, created_at, updated_at)"
        " VALUES ('s1','daily-cleanup','cleanup','{}',1440,1,"
        " '2026-09-14T06:00:00-04:00','2026-09-13','2026-09-13')")
    conn.execute(
        "INSERT INTO scheduled_jobs (id, task_name, action_type,"
        " parameters_json, cron_spec, enabled, next_run_at, created_at,"
        " updated_at) VALUES ('j1','hourly-audit','mailbox_audit','{}',"
        " '0 * * * *',1,'2026-09-13T11:00:00-04:00',"
        " '2026-09-13','2026-09-13')")
    out = sweep._dump_job_tables(conn)
    conn.close()
    assert "tables: scheduled_jobs, schedules" in out
    assert "--- schedules (1 row(s)) ---" in out
    assert "--- scheduled_jobs (1 row(s)) ---" in out
    assert "name=hourly-audit" in out
    assert "schedule=0 * * * *" in out
    assert "enabled=1" in out
    assert "next_run_at=2026-09-13T11:00:00-04:00" in out
    # payload_json/parameters_json must NOT leak into the dump
    assert "payload_json" not in out
    assert "parameters_json" not in out


def test_dump_job_tables_absent_tables_reported(sweep):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    out = sweep._dump_job_tables(conn)
    conn.close()
    assert "--- schedules: <absent>" in out
    assert "--- scheduled_jobs: <absent>" in out


def test_dump_job_tables_never_raises_on_broken_conn(sweep):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.close()
    out = sweep._dump_job_tables(conn)
    assert "<could not list tables:" in out
