"""Test workspaces that are not under /tmp or /private/tmp."""

from __future__ import annotations

import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / ".robie-durable-test"


def durable_temporary_directory() -> tempfile.TemporaryDirectory:
    ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(ROOT))
