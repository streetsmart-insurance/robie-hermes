"""Test-before-Production gate for NEW job types / LOB skills.

Commercial auto and other already-live Production types are grandfathered.
A new job type cannot be treated as Production-ready unless N clean Test
jobs have persisted a passing post-job audit. N is the required gate, not a
suggestion. This module never authorizes Job COMPLETE and does not bind.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, current_robie_env
from .store import JobStore


REQUIRED_CLEAN_TEST_JOBS = 3
REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_DIR = REPO_ROOT / "deploy" / "job_type_gate"
GRANDFATHERED_PATH = GATE_DIR / "grandfathered.json"
PROMOTIONS_DIR = GATE_DIR / "promotions"

# Already live on Production (hermes-poc-01). Do not block these paths.
GRANDFATHERED_JOB_TYPES = frozenset(
    {
        "drive.skill_sync",
        "carrier.proposal",
        "browser.read",
        "ezlynx.reassign",
        "ezlynx.move_document",
        "ezlynx.apply_label",
        "ezlynx.submission_audit",
        "ezlynx.session_refresh",
        "filesystem.skill_update",
        "hermes.google_chat_task",
        "hermes.plain_english",
        "hermes.needs_clarification",
        "ezlynx.commercial_auto",
    }
)
GRANDFATHERED_SKILLS = frozenset(
    {
        "ezlynx-commercial-auto-from-quote",
        "ezlynx-gemini-fallback",
        "robie-playwright-browser",
        "ezlynx-session-login",
    }
)


class JobTypeGateError(RuntimeError):
    """Raised when a new job type is treated as Production-ready without Test audits."""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def grandfathered_job_types() -> set[str]:
    data = _load_json(GRANDFATHERED_PATH)
    listed = {str(item) for item in data.get("job_types", [])}
    return set(GRANDFATHERED_JOB_TYPES) | listed


def grandfathered_skills() -> set[str]:
    data = _load_json(GRANDFATHERED_PATH)
    listed = {str(item) for item in data.get("skills", [])}
    return set(GRANDFATHERED_SKILLS) | listed


def required_clean_test_jobs() -> int:
    data = _load_json(GRANDFATHERED_PATH)
    try:
        value = int(data.get("required_clean_test_jobs") or REQUIRED_CLEAN_TEST_JOBS)
    except (TypeError, ValueError):
        value = REQUIRED_CLEAN_TEST_JOBS
    return max(1, value)


def promotion_record_path(job_type: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in job_type)
    return PROMOTIONS_DIR / f"{safe}.json"


def load_promotion_record(job_type: str) -> dict[str, Any] | None:
    path = promotion_record_path(job_type)
    if not path.is_file():
        return None
    data = _load_json(path)
    return data or None


def passing_test_audit_count(record: dict[str, Any] | None) -> int:
    if not record:
        return 0
    jobs = record.get("jobs") or []
    return sum(
        1
        for item in jobs
        if str(item.get("environment") or "").upper() == TEST_ENV_NAME
        and str(item.get("verdict") or "").upper() == "PASS"
        and item.get("job_id")
    )


def is_job_type_production_ready(job_type: str) -> bool:
    if job_type in grandfathered_job_types():
        return True
    return passing_test_audit_count(load_promotion_record(job_type)) >= required_clean_test_jobs()


def production_hold_reason(job_type: str, *, env: str | None = None) -> str | None:
    """Return a hold reason when Production would treat a NEW type as ready."""
    environment = (env if env is not None else current_robie_env()).upper()
    if environment not in PRODUCTION_ENV_NAMES:
        return None
    if is_job_type_production_ready(job_type):
        return None
    needed = required_clean_test_jobs()
    have = passing_test_audit_count(load_promotion_record(job_type))
    return (
        f"new job type {job_type} is not Production-ready: "
        f"{have}/{needed} clean Test post-job audits recorded. "
        f"Run {needed} passing jobs on hermes-test-01, then promote."
    )


def assert_job_type_production_ready(job_type: str, *, env: str | None = None) -> None:
    reason = production_hold_reason(job_type, env=env)
    if reason:
        raise JobTypeGateError(reason)


def parse_skill_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    raw = parts[1]
    parsed: dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


def iter_skill_files(root: Path | None = None) -> list[Path]:
    base = root or REPO_ROOT
    found: list[Path] = []
    for folder in (base / "skills", base / "deploy" / "hermes" / "skills"):
        if not folder.is_dir():
            continue
        found.extend(sorted(folder.glob("*/SKILL.md")))
    return found


def skill_gate_violations(root: Path | None = None) -> list[str]:
    """CI check: a new skill cannot be Production-ready without Test audits."""
    violations: list[str] = []
    allowed_skills = grandfathered_skills()
    for path in iter_skill_files(root):
        meta = parse_skill_frontmatter(path.read_text(encoding="utf-8"))
        name = str(meta.get("name") or path.parent.name)
        job_type = str(meta.get("job_type") or "").strip()
        production_ready = str(meta.get("production_ready") or "").strip().lower()
        if name in allowed_skills:
            continue
        if production_ready in {"true", "yes", "1"}:
            if not job_type:
                violations.append(
                    f"{path}: production_ready=true requires job_type and "
                    f"{required_clean_test_jobs()} clean Test audits"
                )
                continue
            if not is_job_type_production_ready(job_type):
                violations.append(
                    f"{path}: skill {name} job_type={job_type} is marked "
                    f"production_ready but has no Test-{required_clean_test_jobs()}-clean-jobs record"
                )
    return violations


def record_test_audit(
    job_type: str,
    *,
    job_id: str,
    verdict: str,
    environment: str = TEST_ENV_NAME,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one Test audit to the in-repo promotion record. No SSH required."""
    if str(environment).upper() != TEST_ENV_NAME:
        raise JobTypeGateError("only TEST audits may be recorded for promotion")
    PROMOTIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = promotion_record_path(job_type)
    record = load_promotion_record(job_type) or {
        "job_type": job_type,
        "required_clean_test_jobs": required_clean_test_jobs(),
        "jobs": [],
    }
    jobs = list(record.get("jobs") or [])
    jobs = [item for item in jobs if item.get("job_id") != job_id]
    entry = {
        "job_id": job_id,
        "environment": TEST_ENV_NAME,
        "verdict": str(verdict).upper(),
        **(extra or {}),
    }
    jobs.append(entry)
    record["jobs"] = jobs
    record["required_clean_test_jobs"] = required_clean_test_jobs()
    record["passing_count"] = passing_test_audit_count(record)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def collect_passing_audits_from_db(
    db_path: str | Path,
    job_type: str,
) -> list[dict[str, Any]]:
    store = JobStore(db_path)
    found: list[dict[str, Any]] = []
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, action_type, status FROM jobs WHERE action_type=?",
            (job_type,),
        ).fetchall()
    for row in rows:
        audit = store.get_checkpoint(str(row["id"]), "post_job_audit")
        if not audit:
            continue
        if str(audit.get("verdict") or "").upper() != "PASS":
            continue
        found.append(
            {
                "job_id": row["id"],
                "environment": TEST_ENV_NAME,
                "verdict": "PASS",
                "job_status": row["status"],
            }
        )
    return found


def write_promotions_from_test_db(db_path: str | Path, job_type: str) -> dict[str, Any]:
    env = current_robie_env()
    if env and env != TEST_ENV_NAME:
        raise JobTypeGateError(
            f"refusing to write promotions from ROBIE_ENV={env}; use TEST"
        )
    jobs = collect_passing_audits_from_db(db_path, job_type)
    record: dict[str, Any] = {
        "job_type": job_type,
        "required_clean_test_jobs": required_clean_test_jobs(),
        "jobs": [],
        "passing_count": 0,
    }
    for item in jobs:
        record = record_test_audit(
            job_type,
            job_id=str(item["job_id"]),
            verdict="PASS",
            extra={"job_status": item.get("job_status")},
        )
    return record


def check_repo_gate(root: Path | None = None) -> list[str]:
    return skill_gate_violations(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ROBIE new-job-type Production gate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="CI: fail if a new skill is marked Production-ready without Test audits")
    write = sub.add_parser(
        "record-from-db",
        help="Write a promotion record from a Test jobs.db (no SSH to Production)",
    )
    write.add_argument("--db", required=True)
    write.add_argument("--job-type", required=True)
    args = parser.parse_args(argv)
    if args.command == "check":
        violations = check_repo_gate()
        if violations:
            print("\n".join(violations), file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "required_clean_test_jobs": required_clean_test_jobs(),
                    "grandfathered_job_types": sorted(grandfathered_job_types()),
                },
                sort_keys=True,
            )
        )
        return 0
    record = write_promotions_from_test_db(args.db, args.job_type)
    print(json.dumps(record, indent=2, sort_keys=True))
    ready = is_job_type_production_ready(args.job_type)
    return 0 if ready or passing_test_audit_count(record) else 2


if __name__ == "__main__":
    raise SystemExit(main())
