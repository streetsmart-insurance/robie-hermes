"""Durable workspace for release verification. Never /tmp."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .idempotency import assert_durable_path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VERIFY_ROOT = REPO_ROOT / ".robie-durable-test"


def durable_verify_root(explicit: str | Path | None = None) -> Path:
    """Return a durable directory that is not /tmp, /private/tmp, or /var/tmp."""
    root = Path(explicit) if explicit else DEFAULT_VERIFY_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return assert_durable_path(root)


def durable_verify_workdir(*, explicit_root: str | Path | None = None) -> Path:
    """Create a unique verify-release workdir under a durable root."""
    root = durable_verify_root(explicit_root)
    path = Path(tempfile.mkdtemp(prefix="verify-release.", dir=str(root)))
    return assert_durable_path(path)
