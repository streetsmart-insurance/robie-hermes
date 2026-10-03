"""Outcome health check for the 4359 policy-change worker's liveness gate.

Runs after the Tuesday worker: confirms the OUTCOME, not just that the
server is up.

Checks:
  1. Alias integrity — every queue-number -> live-number alias in
     ``policy_number_aliases.json`` still resolves to an Active, unexpired,
     corresponding policy in EZLynx **belonging to the recorded applicant**.
     A renewal that changed the number again, a cancelled policy, or a
     number reissued to another account rots the alias; the check says so
     in plain English.
  2. Held-row re-verification — every row the worker held (from
     ``evidence-latest.json``) still classifies HOLD under the current
     logic. If a held row would now resolve LIVE or DEAD, the worker held
     something it shouldn't have.

Usage:
    python -m robie_job_engine.policy_change_liveness_health [--alert]
        [--evidence PATH] [--aliases PATH]

Exit 0: healthy (one-line OK to stdout, no Chat post).
Exit 1: findings printed in plain English; with --alert they are also posted
to the ROBIE health Chat (``ROBIE_HEALTH_CHAT_SPACE``).

All functions take injected search/evidence so the check is unit-testable
with fakes — no network, no secrets in tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any, Callable, Iterable

from .overdue_policy_change_reports import (
    PolicyChangeReportContractError,
    _is_dead_policy,
    _search_candidates,
    classify_policy_liveness,
    default_policy_aliases_path,
    default_policy_search,
    load_policy_alias_applicants,
    load_policy_aliases,
    policy_numbers_correspond,
)


def _is_active_status(row: dict[str, Any]) -> bool:
    return str(row.get("policyStatus") or row.get("status") or "").strip().casefold() == "active"


def _row_number(row: dict[str, Any]) -> str:
    return str(row.get("policyNumber") or row.get("policy_number") or "")


def _row_account(row: dict[str, Any]) -> str:
    return str(row.get("accountId") or row.get("account_id") or "").strip()

DEFAULT_EVIDENCE_PATH = (
    "/opt/streetsmart-hermes/robie-job-engine/data/"
    "overdue_policy_change_reports/evidence-latest.json"
)


def default_evidence_path() -> str:
    return os.environ.get("ROBIE_4359_EVIDENCE_PATH", DEFAULT_EVIDENCE_PATH)


def _find_live_alias_row(policy_search: Callable[[str], list[dict[str, Any]]],
                         target: str, today: date) -> dict[str, Any] | None:
    """The first Active, unexpired, corresponding PolicyApi row, or None."""
    for candidate in _search_candidates(target):
        for row in policy_search(candidate):
            if not policy_numbers_correspond(_row_number(row), target):
                continue
            if _is_active_status(row) and not _is_dead_policy(row, today):
                return row
    return None


def check_alias_targets(policy_search: Callable[[str], list[dict[str, Any]]],
                        aliases: dict[str, str], today: date,
                        alias_applicants: dict[str, str] | None = None) -> list[str]:
    """Plain-English findings for rotted aliases; empty when all are healthy.

    When ``alias_applicants`` records the hand-verified applicant for a queue
    number, the alias target must still belong to that applicant: a live
    policy on a different account means the number was reissued (or the
    alias was recorded against the wrong account) and the alias must not be
    trusted.
    """
    findings: list[str] = []
    anchors = alias_applicants or {}
    for queue_number, target in sorted(aliases.items()):
        try:
            row = _find_live_alias_row(policy_search, target, today)
        except Exception as exc:  # fail-closed: a search error is a finding
            findings.append(
                f"The policy-number alias '{queue_number}' -> '{target}' could not be "
                f"checked because the policy search failed ({exc})."
            )
            continue
        if row is None:
            findings.append(
                f"The policy-number alias '{queue_number}' -> '{target}' no longer "
                f"points at a live policy in EZLynx. The policy may have renewed "
                f"under a new number or been cancelled — please re-verify the "
                f"account and update policy_number_aliases.json."
            )
            continue
        anchor = anchors.get(queue_number, "")
        if anchor and _row_account(row) != anchor:
            findings.append(
                f"The policy-number alias '{queue_number}' -> '{target}' points at a "
                f"live policy, but on account '{_row_account(row)}', not the "
                f"verified account '{anchor}'. The number may have been reissued "
                f"to another account — please re-verify the account and update "
                f"policy_number_aliases.json before trusting this alias."
            )
    return findings


def check_held_rows(policy_search: Callable[[str], list[dict[str, Any]]],
                    held_rows: Iterable[dict[str, Any]],
                    aliases: dict[str, str], today: date,
                    alias_applicants: dict[str, str] | None = None) -> list[str]:
    """Findings for held rows that no longer classify HOLD; empty when all still hold."""
    findings: list[str] = []
    for held in held_rows:
        number = str(held.get("policy_number") or "")
        applicant_id = str(held.get("applicant_id") or "")
        account = str(held.get("account_name") or "?")
        if not number or not applicant_id:
            findings.append(
                f"A held row for '{account}' is missing its policy number or "
                f"applicant ID, so it cannot be re-checked."
            )
            continue
        try:
            verdict = classify_policy_liveness(
                policy_search, number, applicant_id, today, policy_aliases=aliases,
                policy_alias_applicants=alias_applicants)
        except Exception as exc:
            findings.append(
                f"The held row '{account}' ({number}) could not be re-checked "
                f"because the policy search failed ({exc})."
            )
            continue
        if verdict["verdict"] != "HOLD":
            findings.append(
                f"The held row '{account}' ({number}) would now classify "
                f"{verdict['verdict']} ({verdict.get('reason')}) instead of HOLD. "
                f"The worker held something the current rules resolve — please review."
            )
    return findings


def load_held_rows(evidence_path: str) -> list[dict[str, Any]]:
    """Read held rows from the worker's evidence file; fail-closed on problems."""
    if not os.path.exists(evidence_path):
        raise PolicyChangeReportContractError(
            f"evidence file not found: {evidence_path} — the Tuesday 4359 run "
            f"may not have written its evidence"
        )
    with open(evidence_path, encoding="utf-8") as fh:
        evidence = json.load(fh)
    if not isinstance(evidence, dict):
        raise PolicyChangeReportContractError(
            f"evidence file is not a JSON object: {evidence_path}")
    held = evidence.get("held", [])
    if not isinstance(held, list):
        raise PolicyChangeReportContractError(
            f"evidence 'held' is not a list: {evidence_path}")
    return held


def run_health_check(policy_search: Callable[[str], list[dict[str, Any]]],
                      aliases: dict[str, str],
                      held_rows: Iterable[dict[str, Any]],
                      today: date,
                      alias_applicants: dict[str, str] | None = None) -> tuple[bool, list[str]]:
    """Run both checks. Returns (healthy, findings)."""
    findings = check_alias_targets(policy_search, aliases, today, alias_applicants)
    findings.extend(check_held_rows(policy_search, held_rows, aliases, today, alias_applicants))
    return (not findings, findings)


def format_alert(findings: list[str]) -> str:
    lines = [
        "4359 policy-change liveness check found "
        f"{len(findings)} problem{'s' if len(findings) != 1 else ''}:",
        "",
    ]
    lines.extend(f"- {finding}" for finding in findings)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert", action="store_true",
                        help="post findings to the ROBIE health Chat on failure")
    parser.add_argument("--evidence", default=None,
                        help="path to evidence-latest.json")
    parser.add_argument("--aliases", default=None,
                        help="path to policy_number_aliases.json")
    args = parser.parse_args(argv)

    today = date.today()
    try:
        aliases = load_policy_aliases(args.aliases or default_policy_aliases_path())
        alias_applicants = load_policy_alias_applicants(
            args.aliases or default_policy_aliases_path())
        held_rows = load_held_rows(args.evidence or default_evidence_path())
        healthy, findings = run_health_check(
            default_policy_search, aliases, held_rows, today,
            alias_applicants=alias_applicants)
    except PolicyChangeReportContractError as exc:
        healthy, findings = False, [str(exc)]

    if healthy:
        print(f"OK: 4359 liveness check healthy "
              f"({len(aliases)} aliases verified, {len(held_rows)} held rows re-checked).")
        return 0

    message = format_alert(findings)
    print(message)
    if args.alert:
        space = os.environ.get("ROBIE_HEALTH_CHAT_SPACE", "")
        if not space:
            print("ERROR: --alert given but ROBIE_HEALTH_CHAT_SPACE is not set.",
                  file=sys.stderr)
            return 1
        from .chat_app_post import post_as_chat_app
        post_as_chat_app(space, message)
        print(f"Posted alert to {space}.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
