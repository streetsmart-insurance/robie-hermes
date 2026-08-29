"""Playwright tracing manager for ROBIE durable job execution.

Captures full replayable Playwright traces (DOM snapshots, network activity,
console logs, timeline, and screenshots) during job execution.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


logger = logging.getLogger("robie.playwright_tracing")


class PlaywrightTraceManager:
    """Manages start and stop of Playwright tracing per job execution."""

    def __init__(
        self,
        artifact_root: str | Path | None = None,
        *,
        screenshots: bool = True,
        snapshots: bool = True,
        sources: bool = True,
    ) -> None:
        self.artifact_root = Path(
            artifact_root
            or os.environ.get("ROBIE_ARTIFACT_ROOT")
            or "/opt/streetsmart-hermes/robie-job-engine/data/artifacts"
        )
        self.screenshots = screenshots
        self.snapshots = snapshots
        self.sources = sources

    def trace_path_for_job(self, job_id: str) -> Path:
        folder = self.artifact_root / job_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "playwright-trace.zip"

    def start_tracing(self, context: Any) -> bool:
        """Start tracing on a Playwright browser context."""
        tracing = getattr(context, "tracing", None)
        if tracing is None:
            return False
        start = getattr(tracing, "start", None)
        if not callable(start):
            return False
        try:
            start(
                screenshots=self.screenshots,
                snapshots=self.snapshots,
                sources=self.sources,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to start Playwright tracing: %s", exc)
            return False

    def stop_tracing(self, context: Any, job_id: str, *, save: bool = True) -> Path | None:
        """Stop tracing and optionally write trace.zip to the job artifact folder."""
        tracing = getattr(context, "tracing", None)
        if tracing is None:
            return None
        stop = getattr(tracing, "stop", None)
        if not callable(stop):
            return None
        out_path = self.trace_path_for_job(job_id) if save else None
        try:
            if out_path:
                stop(path=str(out_path))
                return out_path
            stop()
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to stop Playwright tracing for job %s: %s", job_id, exc)
            return None

    @contextmanager
    def trace_job(self, context: Any, job_id: str, *, save_on_failure_only: bool = False) -> Iterator[Path]:
        """Context manager to trace an execution block."""
        started = self.start_tracing(context)
        trace_path = self.trace_path_for_job(job_id)
        failed = False
        try:
            yield trace_path
        except Exception:
            failed = True
            raise
        finally:
            if started:
                should_save = True if not save_on_failure_only else failed
                self.stop_tracing(context, job_id, save=should_save)
