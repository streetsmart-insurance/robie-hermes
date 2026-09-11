"""Unit tests for the file-backed proposal destination."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from robie_job_engine.file_proposal_destination import (
    FileProposalDestination,
    default_proposals_dir,
)


class FileProposalDestinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="proposal-dest-")
        self.addCleanup(self.tmp.cleanup)
        self.dest = FileProposalDestination(os.path.join(self.tmp.name, "p"))

    def test_round_trip(self):
        doc = {"proposal_id": "P-1", "page_count": 12, "body": "x"}
        self.dest.write("P-1", doc)
        self.assertEqual(self.dest.read_fresh("P-1"), doc)

    def test_missing_returns_none(self):
        self.assertIsNone(self.dest.read_fresh("no-such-proposal"))

    def test_corrupt_returns_none(self):
        with open(self.dest._path_for("bad"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(self.dest.read_fresh("bad"))

    def test_overwrite_replaces(self):
        self.dest.write("P-2", {"v": 1})
        self.dest.write("P-2", {"v": 2})
        self.assertEqual(self.dest.read_fresh("P-2"), {"v": 2})

    def test_proposal_id_cannot_escape_directory(self):
        self.dest.write("../../evil", {"v": 1})
        self.assertTrue(
            str(self.dest._path_for("../../evil")).startswith(
                str(self.dest.directory)
            )
        )

    def test_no_temp_files_left_behind(self):
        self.dest.write("P-3", {"v": 1})
        leftovers = [
            f for f in os.listdir(self.dest.directory) if f.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_default_dir_env_override(self):
        os.environ["ROBIE_PROPOSAL_DIR"] = os.path.join(self.tmp.name, "custom")
        try:
            self.assertEqual(
                default_proposals_dir("/x/jobs.db"),
                __import__("pathlib").Path(self.tmp.name) / "custom",
            )
        finally:
            del os.environ["ROBIE_PROPOSAL_DIR"]

    def test_default_dir_next_to_db(self):
        self.assertEqual(
            default_proposals_dir("/var/hermes/jobs.db").name, "proposals"
        )
        self.assertEqual(
            str(default_proposals_dir("/var/hermes/jobs.db").parent),
            "/var/hermes",
        )


if __name__ == "__main__":
    unittest.main()
