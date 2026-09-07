import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.run_accountability_job import occurrence_key, run


def test_daily_occurrence_key_uses_prior_business_day():
    assert occurrence_key(
        "daily",
        now=datetime(2026, 9, 8, 10, 25, tzinfo=timezone.utc),
        holiday_calendar="US-FEDERAL",
    ) == "accountability:daily:2026-09-04"


def test_runner_refuses_non_production_manifest(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"safety": {"environment": "Test"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="environment=Production"):
        run(mode="daily", manifest_path=str(manifest), db_path=str(tmp_path / "jobs.db"))


def test_runner_creates_one_durable_verified_occurrence(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "safety": {"environment": "Production"},
        "rules": {"holiday_calendar": "US-FEDERAL"},
    }), encoding="utf-8")

    class Engine:
        def run(self, job_id):
            return {"id": job_id, "status": "COMPLETE", "error": None}

    at = datetime(2026, 9, 8, 10, 25, tzinfo=timezone.utc)
    with patch("scripts.run_accountability_job.build_runtime_engine", return_value=Engine()):
        first = run(
            mode="daily", manifest_path=str(manifest), db_path=str(tmp_path / "jobs.db"), now=at
        )
        second = run(
            mode="daily", manifest_path=str(manifest), db_path=str(tmp_path / "jobs.db"), now=at
        )

    assert first["verified"] is True
    assert first["job_id"] == second["job_id"]
    assert first["reporting_period"] == "2026-09-04"
