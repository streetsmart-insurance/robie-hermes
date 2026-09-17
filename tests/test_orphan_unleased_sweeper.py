"""Tests for parking unleased RUNNING/VERIFYING orphans (c282de98 class)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from robie_job_engine.models import JobStatus
from robie_job_engine.orphan_unleased_sweeper import (
    format_orphan_alert,
    park_orphan_unleased_jobs,
    run_orphan_unleased_sweep,
)
from robie_job_engine.store import JobStore


class OrphanUnleasedSweeperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = str(Path(self._td.name) / "jobs.db")
        self.store = JobStore(self.db)
        self.now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _seed(
        self,
        *,
        status: str,
        lease_owner: str | None,
        updated_at: datetime,
        action_type: str = "hermes.email_task",
        key: str,
    ) -> str:
        job = self.store.create_job(action_type, {"text": "x"}, idempotency_key=key)
        with self.store.transaction() as conn:
            conn.execute(
                """UPDATE jobs SET status=?, lease_owner=?, lease_expires_at=?,
                   updated_at=?, attempt_count=? WHERE id=?""",
                (
                    status,
                    lease_owner,
                    None if lease_owner is None else "2099-01-01T00:00:00+00:00",
                    updated_at.isoformat(),
                    1,
                    job["id"],
                ),
            )
        return job["id"]

    def test_parks_stale_unleased_running(self):
        stale = self.now - timedelta(hours=2)
        orphan = self._seed(
            status=JobStatus.RUNNING.value,
            lease_owner=None,
            updated_at=stale,
            action_type="hermes.google_chat_task",
            key="c282de98-orphan",
        )
        parked = park_orphan_unleased_jobs(
            self.store, older_than_seconds=3600, now=self.now
        )
        self.assertEqual([p["id"] for p in parked], [orphan])
        row = self.store.get_job(orphan)
        self.assertEqual(row["status"], JobStatus.FAILED.value)
        self.assertIn("orphan_unleased_sweeper", row["last_error"])
        self.assertIsNone(row["lease_owner"])
        cp = self.store.get_checkpoint(orphan, "orphan_unleased_sweep")
        self.assertEqual(cp["prior_status"], JobStatus.RUNNING.value)

    def test_ignores_live_lease_holder(self):
        stale = self.now - timedelta(hours=2)
        leased = self._seed(
            status=JobStatus.RUNNING.value,
            lease_owner="engine:alive",
            updated_at=stale,
            key="leased-alive",
        )
        parked = park_orphan_unleased_jobs(
            self.store, older_than_seconds=3600, now=self.now
        )
        self.assertEqual(parked, [])
        self.assertEqual(self.store.get_job(leased)["status"], JobStatus.RUNNING.value)
        self.assertEqual(self.store.get_job(leased)["lease_owner"], "engine:alive")

    def test_ignores_fresh_unleased(self):
        fresh = self.now - timedelta(minutes=5)
        job_id = self._seed(
            status=JobStatus.VERIFYING.value,
            lease_owner="",
            updated_at=fresh,
            key="fresh-unleased",
        )
        parked = park_orphan_unleased_jobs(
            self.store, older_than_seconds=3600, now=self.now
        )
        self.assertEqual(parked, [])
        self.assertEqual(self.store.get_job(job_id)["status"], JobStatus.VERIFYING.value)

    def test_run_sweep_posts_ops_alert(self):
        stale = self.now - timedelta(hours=3)
        self._seed(
            status=JobStatus.RUNNING.value,
            lease_owner=None,
            updated_at=stale,
            action_type="hermes.email_task",
            key="email-orphan",
        )
        posts: list[tuple] = []

        def poster(space, text, **_kwargs):
            posts.append((space, text))

        with mock.patch.dict(
            os.environ,
            {"ROBIE_ENV": "TEST", "ROBIE_OPS_CHAT_SPACE": "spaces/OPS"},
            clear=False,
        ):
            result = run_orphan_unleased_sweep(
                self.db, older_than_seconds=3600, now=self.now, poster=poster
            )
        self.assertEqual(result["parked_count"], 1)
        self.assertTrue(result["ops_alert_posted"])
        self.assertEqual(posts[0][0], "spaces/OPS")
        self.assertIn("parked 1", posts[0][1])
        self.assertIn(
            result["parked"][0]["id"],
            format_orphan_alert(result["parked"], environment="TEST"),
        )
        self.assertIn("hermes.email_task", posts[0][1])


if __name__ == "__main__":
    unittest.main()
