"""Unit tests for JE-KILL live preflight blockers."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from robie_job_engine.je_kill_preflight import (
    ezlynx_auth_tab_errors,
    fixture_approval_errors,
    job_inventory_errors,
    job_inventory_report,
    read_job_inventory,
    release_pointer_errors,
)

REMOTE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run-je-kill-test-remote.sh"


def _seed_jobs_db(path: Path, rows: list[tuple[str, str, str | None]]) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        """CREATE TABLE jobs (
               id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
               action_type TEXT NOT NULL, payload_json TEXT NOT NULL,
               status TEXT NOT NULL, lease_owner TEXT, lease_expires_at TEXT,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    for job_id, status, lease_owner in rows:
        con.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, f"key-{job_id}", "hermes.google_chat_task", "{}", status,
             lease_owner, None, "2026-09-13T00:00:00Z", "2026-09-13T00:00:00Z"),
        )
    con.commit()
    con.close()


def _remote_inventory_snippet() -> str:
    """Extract the inline inventory gate the remote runner executes on Test."""
    text = REMOTE_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r'python3 - "\$\{job_db\}" <<\'PY\'\n(.*?)\nPY\n', text, re.DOTALL
    )
    assert match, "inventory heredoc missing from remote runner"
    return match.group(1)


def _run_remote_inventory(db_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-", str(db_path)],
        input=_remote_inventory_snippet(),
        text=True,
        capture_output=True,
        check=False,
    )


class JeKillPreflightTests(unittest.TestCase):
    def _fixture(self, *, approved_at: str) -> dict:
        return {
            "test_only": True,
            "disposable": True,
            "approved_by": "Carlo Ferrara",
            "approval_scope": "JE-KILL-01",
            "approved_at": approved_at,
            "scenarios": {
                "before_action": {"account_id": "220250093", "resource_id": "1"},
                "after_action": {"account_id": "220250093", "resource_id": "2"},
                "during_verification": {"account_id": "220250093", "resource_id": "3"},
            },
        }

    def test_fresh_approval_passes(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        data = self._fixture(approved_at="2026-09-12T12:00:00Z")
        self.assertEqual(fixture_approval_errors(data, now=now), [])

    def test_expired_approval_blocks(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        data = self._fixture(approved_at="2026-08-30T12:00:00Z")
        errors = fixture_approval_errors(data, now=now)
        self.assertTrue(any("expired" in e for e in errors))
        self.assertTrue(any("refresh-je-kill-fixture-approval.sh" in e for e in errors))

    def test_login_tab_blocks(self):
        tabs = [
            {
                "type": "page",
                "url": "https://app.ezlynx.com/auth/account/login?redirectURL=x",
                "title": "Login",
            }
        ]
        errors = ezlynx_auth_tab_errors(tabs)
        self.assertTrue(any("CDP AUTHENTICATED required" in e for e in errors))
        self.assertTrue(any("login page" in e for e in errors))

    def test_about_blank_blocks_with_clear_line(self):
        errors = ezlynx_auth_tab_errors(
            [{"type": "page", "url": "about:blank", "title": ""}]
        )
        self.assertTrue(any("about:blank" in e for e in errors))

    def test_authenticated_tab_passes(self):
        tabs = [
            {
                "type": "page",
                "url": "https://app.ezlynx.com/web/account/220250093/documents",
                "title": "Documents",
            }
        ]
        self.assertEqual(ezlynx_auth_tab_errors(tabs), [])

    def test_release_pointer_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "releases" / "aaaaaaaaaaaa" / "robie-hermes-aaaaaaaaaaaa"
            live.mkdir(parents=True)
            (root / "current").symlink_to(live)
            (root / "releases" / "current").symlink_to(live)
            errors = release_pointer_errors(
                test_root=str(root), expected_sha="bbbbbbbbbbbb"
            )
            self.assertTrue(any("SHA mismatch" in e for e in errors))

    def test_release_pointer_match_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "releases" / "c89f4e6abcde" / "robie-hermes-c89f4e6abcde"
            live.mkdir(parents=True)
            (root / "current").symlink_to(live)
            (root / "releases" / "current").symlink_to(live)
            self.assertEqual(
                release_pointer_errors(
                    test_root=str(root), expected_sha="c89f4e6abcdef0123456789"
                ),
                [],
            )


class JeKillJobInventoryTests(unittest.TestCase):
    """Stage 2 round 3: stuck Chat job c282de98 is RUNNING with no lease."""

    STUCK_CHAT = {"id": "c282de98", "status": "RUNNING", "lease_owner": None}
    LEASED = {"id": "aaaa1111", "status": "RUNNING", "lease_owner": "engine:abc"}

    def test_unleased_running_job_does_not_block(self):
        self.assertEqual(job_inventory_errors([self.STUCK_CHAT]), [])
        report = job_inventory_report([self.STUCK_CHAT])
        self.assertEqual(report["blocking"], [])
        self.assertEqual(report["unleased"], ["c282de98 RUNNING"])

    def test_empty_string_lease_is_unleased(self):
        row = {"id": "c282de98", "status": "VERIFYING", "lease_owner": "  "}
        self.assertEqual(job_inventory_errors([row]), [])
        self.assertEqual(job_inventory_report([row])["unleased"], ["c282de98 VERIFYING"])

    def test_leased_job_blocks_with_identity(self):
        errors = job_inventory_errors([self.STUCK_CHAT, self.LEASED])
        self.assertEqual(len(errors), 1)
        self.assertIn("1 active Test Job lease(s)", errors[0])
        self.assertIn("aaaa1111 RUNNING lease=engine:abc", errors[0])
        self.assertNotIn("c282de98", errors[0])

    def test_lease_on_non_active_status_still_blocks(self):
        row = {"id": "bbbb2222", "status": "PENDING", "lease_owner": "engine:x"}
        self.assertTrue(job_inventory_errors([row]))

    def test_read_job_inventory_is_read_only_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "jobs.db"
            _seed_jobs_db(db, [
                ("c282de98", "RUNNING", None),
                ("done0001", "COMPLETED", None),
                ("lease001", "PENDING", "engine:y"),
            ])
            rows = read_job_inventory(str(db))
            self.assertEqual({r["id"] for r in rows}, {"c282de98", "lease001"})
            self.assertEqual(job_inventory_report(rows)["unleased"], ["c282de98 RUNNING"])
            self.assertEqual(len(job_inventory_report(rows)["blocking"]), 1)

    def test_remote_runner_ignores_unleased_running_chat_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "jobs.db"
            _seed_jobs_db(db, [("c282de98", "RUNNING", None), ("done0001", "COMPLETED", None)])
            proc = _run_remote_inventory(db)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("JE-KILL Test inventory: 0 blocking Jobs/leases", proc.stdout)
            self.assertIn("1 unleased RUNNING/VERIFYING Job(s) ignored", proc.stdout)
            self.assertIn("c282de98 RUNNING", proc.stdout)

    def test_remote_runner_refuses_leased_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "jobs.db"
            _seed_jobs_db(db, [("c282de98", "RUNNING", None), ("lease001", "RUNNING", "engine:z")])
            proc = _run_remote_inventory(db)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("JE-KILL REFUSED: 1 active Test Job lease(s)", proc.stderr)
            self.assertIn("lease001 RUNNING lease=engine:z", proc.stderr)
            self.assertNotIn("0 blocking Jobs/leases", proc.stdout)

    def test_remote_runner_clean_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "jobs.db"
            _seed_jobs_db(db, [("done0001", "COMPLETED", None)])
            proc = _run_remote_inventory(db)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("ignored", proc.stdout)
            self.assertIn("0 blocking Jobs/leases", proc.stdout)


if __name__ == "__main__":
    unittest.main()
