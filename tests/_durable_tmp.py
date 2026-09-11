"""Temp directories the engine will accept as a persistent store.

idempotency.assert_durable_path refuses /tmp, /private/tmp and /var/tmp — a
real guard against a production store landing somewhere ephemeral. Any test
that actually runs the engine (rather than only constructing a JobStore) hits
it through DurableWorkLedger, so tempfile.mkdtemp() with its default root
fails on most machines. Use this instead.
"""
import os
import shutil
import tempfile
from pathlib import Path


def _root() -> Path:
    # GitHub Actions: /home/runner/work/_temp, which is durable by this guard's
    # definition. Otherwise a dot-directory under the user's home.
    root = Path(os.environ.get("RUNNER_TEMP") or (Path.home() / ".robie-test-tmp"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def durable_tmpdir(testcase) -> Path:
    """Make a temp dir the engine accepts, removed when the test finishes."""
    path = Path(tempfile.mkdtemp(dir=_root()))
    testcase.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path
