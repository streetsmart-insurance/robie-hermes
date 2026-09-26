"""Verified report config registry. IDs and filters only; no secrets.

4372 / look 4601 notes (do not "fix" the Looker look):
- Identity is Policy Number. The export has no Loan Number column.
  Loan numbers come from #504 Additional Interests enrichment, not CSV.
- Look 4601 has no Custom Filter Set named ``ROBIE Intake``. The look
  title itself is the fail-closed scope marker: refuse if that title is
  not visible rather than returning an unfiltered explore.
- Open Looker look 4601 by look id (and that title). Do not search the
  Reports 5.0 hub for a saved-report link named or numbered 4372.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .store import canonical_json, utc_now


FactKind = Literal["observed", "configured", "inferred", "user_supplied"]

# Look 4601 title / Gmail display name. Visible on the scoped 4372 look;
# there is no "ROBIE Intake" Custom Filter Set on that look.
MORTGAGEE_4372_SCOPE_MARKER = "Mortgagee Verification Queue - ROBIE"

# Look 4602 title / Gmail display name. Scope marker for report 4359.
# There is no separate "Open Requests - ROBIE" Custom Filter Set.
POLICY_CHANGE_4359_SCOPE_MARKER = "Policy Change Request Confirmation Queue - ROBIE"

# Shared Looker look ids (agency SharedReports-Streetsmart Insurance-36748).
# These are Looker look ids, not EZLynx saved-report numbers. SSRobie Saved
# Reports has zero ``a[href*=report_id]`` links for these queues.
LOOK_ID_BY_REPORT: dict[str, str] = {
    "4372": "4601",  # Mortgagee Verification Queue - ROBIE
    # 4246 / 4247 live fetch prefers today's robie@ morning email CSV
    # (report_email_source). Do not wire Looker favorites 4603/4604 — SSRobie
    # has zero saved-report links and emails are the system of record.
    # 4246's daily email is the 4360 Active-filtered transaction feed.
    "4359": "4602",  # Policy Change Request Confirmation Queue - ROBIE
}


@dataclass(frozen=True)
class ReportSpec:
    report_id: str
    name: str
    schema_verified: bool
    identity_fields: tuple[str, ...]
    filter_name: str | None = None
    look_id: str | None = None
    metadata_only: bool = False
    alias_of: str | None = None


VERIFIED_REPORTS: dict[str, ReportSpec] = {
    "4247": ReportSpec(
        "4247", "Manual Renewals", True, ("policy_number",)
    ),
    "4372": ReportSpec(
        "4372",
        "Mortgagee",
        True,
        ("policy_number",),
        filter_name=MORTGAGEE_4372_SCOPE_MARKER,
        look_id=LOOK_ID_BY_REPORT["4372"],
    ),
    "4246": ReportSpec(
        "4246", "Audit", True, ("audit_id",)
    ),
    "4359": ReportSpec(
        "4359",
        "Policy Change",
        # Stay False until 3 clean hermes-test-01 post-job audits after
        # Test install. Do not flip this in the same change as
        # POLICY_CHANGE_ENABLED.
        False,
        ("policy_number", "change_request_created_date"),
        filter_name=POLICY_CHANGE_4359_SCOPE_MARKER,
        look_id=LOOK_ID_BY_REPORT["4359"],
    ),
}
METADATA_ONLY_ALIASES: dict[str, ReportSpec] = {
    "4244": ReportSpec(
        "4244", "Policy Change (duplicate metadata)", False, (),
        metadata_only=True, alias_of="4359",
    ),
    "4248": ReportSpec(
        "4248", "Policy Change (duplicate metadata)", False, (),
        metadata_only=True, alias_of="4359",
    ),
}


class ReportRegistryError(RuntimeError):
    pass


def get_report_spec(report_id: str) -> ReportSpec:
    report_id = str(report_id).strip()
    if report_id in METADATA_ONLY_ALIASES:
        raise ReportRegistryError(
            f"report {report_id} is metadata only and is not a work identity"
        )
    spec = VERIFIED_REPORTS.get(report_id)
    if spec is None:
        raise ReportRegistryError(f"unknown report id {report_id}")
    return spec


def config_fingerprint(spec: ReportSpec, filters: dict[str, Any], fields: list[str]) -> str:
    body = canonical_json(
        {
            "report_id": spec.report_id,
            "filter_name": spec.filter_name,
            "look_id": spec.look_id,
            "filters": filters,
            "fields": fields,
            "schema_verified": spec.schema_verified,
        }
    )
    return hashlib.sha256(body.encode()).hexdigest()


class ReportRunRegistry:
    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS report_run_configs (
                    run_id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    filters_json TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    facts_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def start_run(
        self,
        *,
        run_id: str,
        report_id: str,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        facts: dict[str, tuple[Any, FactKind]] | None = None,
        chat_memory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = get_report_spec(report_id)
        if not spec.schema_verified:
            raise ReportRegistryError(
                f"report {spec.report_id} schema is unverified and must block"
            )
        resolved_fields = list(fields or spec.identity_fields)
        missing_identity = [name for name in spec.identity_fields if name not in resolved_fields]
        if missing_identity:
            raise ReportRegistryError(
                f"report {spec.report_id} is missing identity fields: {', '.join(missing_identity)}"
            )
        if not resolved_fields:
            raise ReportRegistryError(f"report {spec.report_id} is missing an output schema")
        merged_facts = _merge_facts(facts or {}, chat_memory or {})
        fingerprint = config_fingerprint(spec, filters or {}, resolved_fields)
        payload = {
            "run_id": run_id,
            "report_id": spec.report_id,
            "name": spec.name,
            "filter_name": spec.filter_name,
            "look_id": spec.look_id,
            "fingerprint": fingerprint,
            "filters": filters or {},
            "fields": resolved_fields,
            "facts": merged_facts,
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO report_run_configs
                   (run_id,report_id,fingerprint,filters_json,fields_json,facts_json,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    run_id,
                    spec.report_id,
                    fingerprint,
                    json.dumps(payload["filters"], sort_keys=True),
                    json.dumps(resolved_fields),
                    json.dumps(merged_facts, sort_keys=True),
                    utc_now(),
                ),
            )
        return payload

    def get(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM report_run_configs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        item = dict(row)
        item["filters"] = json.loads(item.pop("filters_json"))
        item["fields"] = json.loads(item.pop("fields_json"))
        item["facts"] = json.loads(item.pop("facts_json"))
        return item


def _merge_facts(
    facts: dict[str, tuple[Any, FactKind]],
    chat_memory: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    rank = {"configured": 4, "observed": 3, "user_supplied": 2, "inferred": 1}
    merged: dict[str, dict[str, Any]] = {}
    for key, value in chat_memory.items():
        merged[key] = {"value": value, "kind": "inferred", "source": "memory_or_chat"}
    for key, (value, kind) in facts.items():
        existing = merged.get(key)
        if existing and rank[existing["kind"]] > rank[kind]:
            continue
        merged[key] = {"value": value, "kind": kind, "source": "verified_config"}
    return merged
