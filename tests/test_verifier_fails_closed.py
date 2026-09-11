"""Regression tests: the mortgagee verifier must never verify the worker's
own snapshot. Stdlib unittest so it runs without pytest installed."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from robie_job_engine.mortgagee_verification_worker import (
    DURABLE_NAMESPACE,
    MortgageeVerificationVerifier,
    VerificationReadUnavailable,
    _durable_policy_states,
    _fresh_checkpoint,
)

WORKER_SNAPSHOT = {"detail": {"policy_outcomes": [{"policy_number": "FROM-THE-WORKER"}]}}
ROW_DATA = {"detail": {"policy_outcomes": [{"policy_number": "FROM-THE-DATABASE"}]}}


def _make_db(path, *, job_id=None, checkpoint=None):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE checkpoints (job_id TEXT, kind TEXT, data_json TEXT)")
    conn.execute("CREATE TABLE durable_work_items (namespace TEXT, work_item_key TEXT, outcome TEXT)")
    if job_id and checkpoint is not None:
        conn.execute("INSERT INTO checkpoints VALUES (?,?,?)", (job_id, "action", json.dumps(checkpoint)))
    conn.commit()
    conn.close()


class FreshCheckpointFailsClosed(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("ROBIE_JOB_DB", None)

    def tearDown(self):
        self._env.stop()

    def test_no_db_path_raises_instead_of_returning_the_worker_snapshot(self):
        job = {"id": "j1", "payload": {}}
        with self.assertRaises(VerificationReadUnavailable) as ctx:
            _fresh_checkpoint(job, WORKER_SNAPSHOT)
        self.assertIn("ROBIE_JOB_DB unset", str(ctx.exception))

    def test_read_exception_raises_instead_of_returning_the_worker_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "jobs.db")
            _make_db(db, job_id="j1", checkpoint=ROW_DATA)
            job = {"id": "j1", "payload": {"db_path": db}}
            with mock.patch("sqlite3.connect", side_effect=PermissionError("EPERM")):
                with self.assertRaises(VerificationReadUnavailable) as ctx:
                    _fresh_checkpoint(job, WORKER_SNAPSHOT)
            self.assertIn("PermissionError", str(ctx.exception))
            self.assertIn(db, str(ctx.exception))

    def test_missing_checkpoint_row_raises(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "jobs.db")
            _make_db(db)
            job = {"id": "j1", "payload": {"db_path": db}}
            with self.assertRaises(VerificationReadUnavailable):
                _fresh_checkpoint(job, WORKER_SNAPSHOT)

    def test_returns_the_database_row_and_not_the_action_argument(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "jobs.db")
            _make_db(db, job_id="j1", checkpoint=ROW_DATA)
            job = {"id": "j1", "payload": {"db_path": db}}
            got = _fresh_checkpoint(job, WORKER_SNAPSHOT)
            self.assertEqual(got, ROW_DATA)
            self.assertNotEqual(got, WORKER_SNAPSHOT)

    def test_store_path_is_used_when_payload_and_env_are_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "jobs.db")
            _make_db(db, job_id="j1", checkpoint=ROW_DATA)
            store = mock.Mock(path=db)
            got = _fresh_checkpoint({"id": "j1", "payload": {}}, WORKER_SNAPSHOT, store=store)
            self.assertEqual(got, ROW_DATA)


class DurableStatesFailClosed(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ROBIE_JOB_DB", None)

    def test_no_db_path_raises(self):
        with self.assertRaises(VerificationReadUnavailable):
            _durable_policy_states({"id": "j1", "payload": {}})

    def test_empty_table_returns_empty_dict_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "jobs.db")
            _make_db(db)
            self.assertEqual(_durable_policy_states({"id": "j1", "payload": {"db_path": db}}), {})

    def test_read_exception_raises(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "jobs.db")
            _make_db(db)
            job = {"id": "j1", "payload": {"db_path": db}}
            with mock.patch("sqlite3.connect", side_effect=PermissionError("EPERM")):
                with self.assertRaises(VerificationReadUnavailable):
                    _durable_policy_states(job)

    def test_rows_are_returned(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "jobs.db")
            _make_db(db)
            conn = sqlite3.connect(db)
            conn.execute("INSERT INTO durable_work_items VALUES (?,?,?)",
                         (DURABLE_NAMESPACE, "POL-1", json.dumps({"outcome": "producer_review_complete"})))
            conn.commit(); conn.close()
            states = _durable_policy_states({"id": "j1", "payload": {"db_path": db}})
            self.assertIn("POL-1", states)


class VerifierSurfacesTheFailure(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ROBIE_JOB_DB", None)

    def test_verify_raises_so_the_engine_marks_it_unverified(self):
        # engine._verify wraps verify() in try/except and routes an exception to
        # _verification_retry_or_unverified. Raising is the correct behaviour;
        # silently verifying the worker's snapshot is not.
        verifier = MortgageeVerificationVerifier()
        with self.assertRaises(VerificationReadUnavailable):
            verifier.verify({"id": "j1", "payload": {}}, WORKER_SNAPSHOT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
