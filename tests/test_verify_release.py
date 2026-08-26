"""verify-release must never unpack the suite onto an ephemeral path."""

from __future__ import annotations

import unittest
from pathlib import Path

from durable_temp import durable_temporary_directory

from robie_job_engine.idempotency import IdempotencyError, assert_durable_path
from robie_job_engine.release_verify import (
    DEFAULT_VERIFY_ROOT,
    durable_verify_root,
    durable_verify_workdir,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify-release.sh"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-release.sh"


class VerifyReleasePathTests(unittest.TestCase):
    def test_verify_release_script_uses_durable_helper_not_tmp(self):
        text = VERIFY_SCRIPT.read_text()
        self.assertIn("durable_verify_workdir", text)
        self.assertNotRegex(text, r"mktemp\s+-d(?:\s|$)")
        self.assertNotIn('temp_dir="$(mktemp -d)"', text)
        for ephemeral in ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp"):
            self.assertNotIn(f'"{ephemeral}', text)
        build = BUILD_SCRIPT.read_text()
        self.assertNotRegex(build, r"mktemp\s+-d(?:\s|$)")

    def test_durable_verify_workdir_rejects_ephemeral_roots(self):
        for ephemeral in (
            "/tmp",
            "/tmp/verify-release",
            "/private/tmp/verify-release",
            "/var/tmp/verify-release",
            "/private/var/tmp/verify-release",
        ):
            with self.assertRaises(IdempotencyError, msg=ephemeral):
                durable_verify_root(ephemeral)
            with self.assertRaises(IdempotencyError, msg=f"workdir:{ephemeral}"):
                durable_verify_workdir(explicit_root=ephemeral)

    def test_durable_verify_workdir_is_outside_tmp(self):
        work = durable_verify_workdir()
        try:
            resolved = assert_durable_path(work)
            text = str(resolved)
            self.assertTrue(text.startswith(str(DEFAULT_VERIFY_ROOT.resolve())))
            self.assertFalse(text.startswith("/tmp"))
            self.assertFalse(text.startswith("/private/tmp"))
            self.assertFalse(text.startswith("/var/tmp"))
            self.assertIn("verify-release.", resolved.name)
        finally:
            Path(work).rmdir()

    def test_durable_verify_workdir_accepts_explicit_durable_root(self):
        tmp = durable_temporary_directory()
        try:
            work = durable_verify_workdir(explicit_root=tmp.name)
            try:
                assert_durable_path(work)
                self.assertTrue(str(work).startswith(str(Path(tmp.name).resolve())))
            finally:
                Path(work).rmdir()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
