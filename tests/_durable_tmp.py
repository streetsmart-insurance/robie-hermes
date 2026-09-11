"""Temp directories the engine will accept as a persistent store.

idempotency.assert_durable_path refuses /tmp, /private/tmp and /var/tmp — a
real guard against a production store landing somewhere ephemeral. Any test
that actually runs the engine (rather than only constructing a JobStore) hits
it through DurableWorkLedger, so tempfile.mkdtemp() with its default root
fails. Allocate under the repo's existing .robie-durable-test root, which
regression_battery and release_verify already use and .gitignore already
covers.
"""
import shutil
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DURABLE_ROOT = REPO_ROOT / ".robie-durable-test"


def durable_tmpdir(testcase) -> Path:
    """Make a temp dir the engine accepts, removed when the test finishes."""
    root = DURABLE_ROOT / "unit"
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=root))
    testcase.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path
