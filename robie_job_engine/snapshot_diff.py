"""Visual and DOM accessibility snapshot diffing for carrier & portal pages.

Detects upstream UI changes and drift on Test pages before they cause live failures.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PageSnapshot:
    portal: str
    page_name: str
    url: str
    captured_at: str
    dom_tree: dict[str, Any]
    elements_summary: list[dict[str, str]]
    screenshot_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SnapshotDiffReport:
    portal: str
    page_name: str
    is_drifted: bool
    added_elements: list[dict[str, str]] = field(default_factory=list)
    removed_elements: list[dict[str, str]] = field(default_factory=list)
    text_diff: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SnapshotDiffManager:
    """Manages baseline snapshots and computes structural DOM differences."""

    def __init__(self, baseline_dir: str | Path | None = None) -> None:
        self.baseline_dir = Path(baseline_dir or Path(__file__).resolve().parent / "snapshots")
        self.baseline_dir.mkdir(parents=True, exist_ok=True)

    def _baseline_path(self, portal: str, page_name: str) -> Path:
        return self.baseline_dir / f"{portal}_{page_name}_baseline.json"

    def save_baseline(self, snapshot: PageSnapshot) -> Path:
        path = self._baseline_path(snapshot.portal, snapshot.page_name)
        path.write_text(json.dumps(snapshot.to_dict(), indent=2))
        return path

    def get_baseline(self, portal: str, page_name: str) -> PageSnapshot | None:
        path = self._baseline_path(portal, page_name)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return PageSnapshot(
            portal=data["portal"],
            page_name=data["page_name"],
            url=data["url"],
            captured_at=data["captured_at"],
            dom_tree=data["dom_tree"],
            elements_summary=data["elements_summary"],
            screenshot_sha256=data.get("screenshot_sha256"),
        )

    def diff_snapshot(self, current: PageSnapshot) -> SnapshotDiffReport:
        baseline = self.get_baseline(current.portal, current.page_name)
        if baseline is None:
            return SnapshotDiffReport(
                portal=current.portal,
                page_name=current.page_name,
                is_drifted=False,
                summary="No baseline found; current snapshot is now the baseline.",
            )

        baseline_keys = {f"{e.get('role', '')}:{e.get('name', '')}" for e in baseline.elements_summary}
        current_keys = {f"{e.get('role', '')}:{e.get('name', '')}" for e in current.elements_summary}

        added = [e for e in current.elements_summary if f"{e.get('role', '')}:{e.get('name', '')}" not in baseline_keys]
        removed = [e for e in baseline.elements_summary if f"{e.get('role', '')}:{e.get('name', '')}" not in current_keys]

        base_lines = json.dumps(baseline.elements_summary, indent=2).splitlines()
        curr_lines = json.dumps(current.elements_summary, indent=2).splitlines()
        diff_lines = list(difflib.unified_diff(base_lines, curr_lines, fromfile="baseline", tofile="current"))

        is_drifted = bool(added or removed or (baseline.screenshot_sha256 != current.screenshot_sha256 and current.screenshot_sha256 is not None))
        summary = f"Drift detected: +{len(added)} added, -{len(removed)} removed elements." if is_drifted else "No significant UI drift detected."

        return SnapshotDiffReport(
            portal=current.portal,
            page_name=current.page_name,
            is_drifted=is_drifted,
            added_elements=added,
            removed_elements=removed,
            text_diff="\n".join(diff_lines[:100]),
            summary=summary,
        )
