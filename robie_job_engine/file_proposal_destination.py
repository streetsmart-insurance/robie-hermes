"""File-backed ProposalDestination for the scheduler path.

The bounded carrier-proposal worker generates a proposal document dict;
this destination persists it durably as JSON so the verifier can
independently re-read it on every verification. Unlike the in-memory test
double (forbidden outside tests), records survive process restarts and are
always re-read from disk — the verifier never sees the worker's memory.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


def default_proposals_dir(db_path: str | Path | None = None) -> Path:
    """Where proposal records live.

    ``ROBIE_PROPOSAL_DIR`` wins when set; otherwise a ``proposals/``
    directory next to the jobs database, so records are co-located with
    the jobs they belong to.
    """
    override = os.environ.get("ROBIE_PROPOSAL_DIR", "").strip()
    if override:
        return Path(override)
    if db_path:
        return Path(db_path).resolve().parent / "proposals"
    return Path.cwd() / "proposals"


class FileProposalDestination:
    """Durable, file-backed ProposalDestination.

    ``write()`` persists the proposal document as JSON with an atomic
    temp-file + rename, so a crash can never leave a half-written record
    for the verifier to read. ``read_fresh()`` re-reads from disk every
    time and returns ``None`` when the record is missing or corrupt —
    the verifier treats that as failed verification (fail closed), never
    as success.
    """

    def __init__(self, proposals_dir: str | Path | None = None):
        self._dir = Path(proposals_dir) if proposals_dir else default_proposals_dir()
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._dir

    def _path_for(self, proposal_id: str) -> Path:
        safe = _SAFE_ID.sub("_", str(proposal_id or "").strip()).strip("._")
        return self._dir / f"{safe or 'proposal'}.json"

    def write(self, proposal_id: str, document: dict[str, Any]) -> None:
        target = self._path_for(proposal_id)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._dir), prefix=".proposal-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(document, fh, indent=2, sort_keys=True)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def read_fresh(self, proposal_id: str) -> dict[str, Any] | None:
        try:
            with open(self._path_for(proposal_id), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
