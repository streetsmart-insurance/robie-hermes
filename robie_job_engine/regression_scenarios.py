"""Named deterministic battery scenarios. Previously seen failures only.

Same-day rule: every Production incident that was a NEW failure mode gets
a named scenario here before we call the incident closed. That is how the
simulator grows. Simulator passed still only means known scenarios did not
regress.

No live EZLynx. No Production jobs.db. No @robie.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .chat_guard import (
    guard_chat_response,
    open_chat_job,
    reopen_resumed_generic_chat_job,
    stop_generic_chat_job_heartbeat,
)
from .chat_queue import DurableChatEventQueue
from .models import JobStatus
from .quote_replay import is_live_hermes_path
from .store import JobStore
from .test_runtime import ProductionGuardError
from .worker_contract import claims_unverified_destination_progress


REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_PATH = REPO_ROOT / "deploy" / "regression_battery" / "scenarios.json"

SAME_DAY_RULE = (
    "Every Production incident that was a NEW failure mode gets a named "
    "deterministic scenario in the battery before we call the incident closed. "
    "That is how the simulator grows."
)

HITL_RESUME_CHAT = (
    "HITL resume orphan class returned (09d69760, da53765b, 6cf6f6ae): "
    "resume must re-lease the same job. Do not start a second job. "
    "Do not RETRY leftover failed ids."
)

FALSE_SUCCESS_CHAT = (
    "FALSE-SUCCESS class returned (30777947, c31f9c69): worker wrote "
    "I-did-it / COMPLETE-shaped prose with 0 destination evidence. "
    "Must stay UNVERIFIED or FAILED."
)

ASCEND_AUDIT_CHAT = (
    "ASCEND locator + artifact audit class returned: unique-locator fail is "
    "FAIL (no Gemini, no .first/.nth/.last). Artifact folder must equal the "
    "job id. Missing PDF after save is FAIL. This job ends as a report, not "
    "COMPLETE of a finance agreement."
)

CONCAT_PATH_CHAT = (
    "ARTIFACT PATH class returned (eb96f620): worker looked in "
    "artifacts/{job_id[:-11]+artifact_id}/. Lookup must be exactly "
    "{artifact_root}/{full_job_id}/. Concatenating a sliced job id with "
    "the artifact id is FAIL."
)

HITL_TONE_CHAT = (
    "HITL Chat tone class returned: cowboy slang, blame, or nickname voice "
    "is rewritten to dry PLAYWRIGHT_BLOCKED + path + ask before send."
)

ASCEND_ROLES_CHAT = (
    "ASCEND roles class returned: Producer and Account Manager must be the "
    "Chat sender (requested_by). Jake → Jake Ferrara. Carlo → Carlo Ferrara. "
    "Unknown sender HITL. Never leave Robie AI when requested_by is known."
)

ASCEND_SPINNER_CHAT = (
    "ASCEND + New program class returned: wait out the programs spinner; "
    "click the unique primary + New program only, never the split-menu caret. "
    "Timeout is PLAYWRIGHT_BLOCKED. No Gemini."
)

ASCEND_CUSTOMER_TYPE_CHAT = (
    "ASCEND customer type class returned: Commercial vs Personal is from "
    "line of business, not the form default and not LLC vs person-name. "
    "Missing LOB is HITL. Do not guess."
)

I_DID_IT_PROSE = (
    "I did it. The job is complete. Completed successfully — COMPLETE."
)

HITL_BLOCKER = (
    "ROBIE_BLOCKED: PLAYWRIGHT_BLOCKED: "
    "Could not find PENDING-PROGRESSIVE-CA-220250093 in dropdown"
)

NAMED_SCENARIO_IDS = frozenset(
    {
        "same-day:named-scenario-before-close",
        "hitl-resume:re-lease-after-gateway-restart",
        "hitl-resume:no-second-job",
        "hitl-resume:no-retry-leftover-failed",
        "false-success:complete-prose-zero-evidence",
        "ascend:locator-and-artifact-audit",
        "artifact-path:concat-job-id-eb96f620",
        "hitl-tone:dry-playwright-blocked",
        "ascend-roles:sender-not-robie-ai",
        "ascend-new-program:wait-spinner",
        "ascend-customer-type:lob",
    }
)


class IncidentCloseError(ValueError):
    """NEW failure-mode incidents cannot close without a named scenario."""


def load_scenario_catalog(path: Path | None = None) -> dict[str, Any]:
    target = Path(path or SCENARIOS_PATH)
    return json.loads(target.read_text(encoding="utf-8"))


def close_new_failure_incident(
    incident: dict[str, Any],
    *,
    known_scenario_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Refuse to close a NEW failure mode that has no named battery scenario."""
    allowed = set(known_scenario_ids or NAMED_SCENARIO_IDS)
    if not incident.get("new_failure_mode"):
        return dict(incident, closed=True)
    scenario = str(incident.get("scenario") or "").strip()
    if not scenario:
        raise IncidentCloseError(
            "cannot close NEW failure mode without a named deterministic scenario"
        )
    if scenario not in allowed:
        raise IncidentCloseError(
            f"cannot close NEW failure mode: scenario {scenario!r} is not in the battery"
        )
    return dict(incident, closed=True, scenario=scenario)


def same_day_close_gaps(
    catalog: dict[str, Any] | None = None,
    *,
    known_scenario_ids: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Return close-out violations. Empty means the living catalog is consistent."""
    data = catalog if catalog is not None else load_scenario_catalog()
    allowed = set(known_scenario_ids or NAMED_SCENARIO_IDS)
    gaps: list[str] = []
    for row in data.get("incidents") or []:
        if not row.get("new_failure_mode"):
            continue
        scenario = str(row.get("scenario") or "").strip()
        if row.get("closed") and not scenario:
            gaps.append(f"{row.get('id')}: closed without a named scenario")
            continue
        if row.get("closed") and scenario not in allowed:
            gaps.append(f"{row.get('id')}: scenario {scenario!r} is not in the battery")
    return gaps


def run_same_day_scenario_rule() -> dict[str, Any]:
    catalog = load_scenario_catalog()
    gaps = same_day_close_gaps(catalog)
    return {
        "id": "same-day:named-scenario-before-close",
        "kind": "logic",
        "ok": not gaps,
        "outcome": "PASS" if not gaps else "FAILED",
        "evidence": (
            SAME_DAY_RULE
            if not gaps
            else "same-day close blocked: " + "; ".join(gaps)
        ),
    }


def _all_job_ids(store: JobStore) -> list[str]:
    with store.connect() as conn:
        rows = conn.execute("SELECT id FROM jobs ORDER BY created_at").fetchall()
    return [str(row["id"]) for row in rows]


def _result(
    scenario_id: str,
    *,
    ok: bool,
    outcome: str,
    evidence: str,
) -> dict[str, Any]:
    return {
        "id": scenario_id,
        "kind": "logic",
        "ok": ok,
        "outcome": outcome,
        "evidence": evidence,
    }


def _fail(scenario_id: str, evidence: str, outcome: str = "FAILED") -> dict[str, Any]:
    return _result(scenario_id, ok=False, outcome=outcome, evidence=evidence)


def run_hitl_resume_scenarios(*, work_dir: Path) -> list[dict[str, Any]]:
    """AWAITING_HUMAN_INPUT + lost lease after gateway restart must re-lease.

    Known class: 09d69760, da53765b, 6cf6f6ae.
    Resume the same job. Do not start a second job. Do not RETRY leftover
    failed ids.
    """
    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing HITL resume scenario on live Hermes path: {work_dir}"
        )
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = str(work_dir / "hitl-resume.db")
    space = "spaces/hitl-resume-battery"
    leftover_space = "spaces/hitl-leftover-failed"
    live_id: str | None = None
    leftover_id: str | None = None
    retry_open: str | None = None
    try:
        leftover_id = open_chat_job(
            db,
            f"{leftover_space}/messages/open",
            "finish policy leftover",
            requested_by="Carlo",
            conversation_id=leftover_space,
        )
        if leftover_id is None:
            return [
                _fail("hitl-resume:no-retry-leftover-failed", "leftover job did not open")
            ]
        leftover_store = JobStore(db)
        leftover_queue = DurableChatEventQueue(db)
        guard_chat_response(db, leftover_id, HITL_BLOCKER)
        stop_generic_chat_job_heartbeat(db, leftover_id)
        leftover_store.transition(
            leftover_id,
            JobStatus.FAILED,
            expected={JobStatus.AWAITING_HUMAN_INPUT},
            error="orphan_timeout after gateway restart",
            release_lease=True,
        )

        live_id = open_chat_job(
            db,
            f"{space}/messages/open",
            "finish policy 220250093",
            requested_by="Carlo",
            conversation_id=space,
        )
        if live_id is None:
            return [_fail("hitl-resume:re-lease-after-gateway-restart", "live job did not open")]
        store = JobStore(db)
        queue = DurableChatEventQueue(db)
        guard_chat_response(db, live_id, HITL_BLOCKER)
        stop_generic_chat_job_heartbeat(db, live_id)
        parked = store.get_job(live_id)
        if parked["status"] != JobStatus.AWAITING_HUMAN_INPUT.value:
            return [
                _fail(
                    "hitl-resume:re-lease-after-gateway-restart",
                    f"expected AWAITING_HUMAN_INPUT, got {parked['status']}",
                )
            ]
        stale = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE checkpoints SET created_at=? WHERE job_id=? AND kind='gateway_progress'",
                (stale, live_id),
            )
            conn.execute(
                "UPDATE jobs SET lease_owner=NULL, lease_expires_at=NULL WHERE id=?",
                (live_id,),
            )
        before = _all_job_ids(store)
        resumed = queue.resume_human_input(
            conversation_id=space,
            job_id=live_id,
            reply_message_id=f"{space}/messages/hitl-reply",
            field_name="operator_response",
            value="just create a new shell for now as a test case",
        )
        reopen_resumed_generic_chat_job(db, live_id)
        continued = open_chat_job(
            db,
            f"{space}/messages/hitl-reply",
            "just create a new shell for now as a test case",
            requested_by="Carlo",
            conversation_id=space,
        )
        after = _all_job_ids(store)
        updated = store.get_job(live_id)
        progress = store.get_checkpoint(live_id, "gateway_progress")
        resume_record = store.get_checkpoint_record(live_id, "human_input_resume")
        orphaned = store.fail_orphaned_chat_jobs()
        later = datetime.now(timezone.utc) + timedelta(seconds=300)
        store.heartbeat_generic_chat_job(live_id, now=later)
        orphaned_later = store.fail_orphaned_chat_jobs(now=later)

        release_ok = (
            resumed.get("state") == "DIRECT_RESUME"
            and continued == live_id
            and updated["status"] == JobStatus.RUNNING.value
            and updated["lease_owner"] is None
            and resume_record is not None
            and progress is not None
            and progress.get("last_at") >= resume_record["created_at"]
            and live_id not in orphaned
            and live_id not in orphaned_later
            and store.get_job(live_id)["status"] == JobStatus.RUNNING.value
        )
        second_job = [job_id for job_id in after if job_id not in before]
        no_second = continued == live_id and second_job == []

        leftover = leftover_store.get_job(leftover_id)
        leftover_resume = leftover_store.resume(leftover_id)
        try:
            leftover_queue.resume_human_input(
                conversation_id=leftover_space,
                job_id=leftover_id,
                reply_message_id=f"{leftover_space}/messages/retry",
                field_name="operator_response",
                value="RETRY",
            )
            leftover_resumed = True
        except RuntimeError:
            leftover_resumed = False
        released = leftover_queue.release_stale_human_input_bind(leftover_space)
        retry_open = open_chat_job(
            db,
            f"{leftover_space}/messages/retry",
            "RETRY",
            requested_by="Carlo",
            conversation_id=leftover_space,
        )
        leftover_after = leftover_store.get_job(leftover_id)
        no_retry_leftover = (
            leftover["status"] == JobStatus.FAILED.value
            and leftover_resume["status"] == JobStatus.FAILED.value
            and leftover_after["status"] == JobStatus.FAILED.value
            and leftover_after.get("status") != JobStatus.RETRY_WAIT.value
            and not leftover_resumed
            and released is not None
            and retry_open != leftover_id
        )

        results = [
            _result(
                "hitl-resume:re-lease-after-gateway-restart",
                ok=release_ok,
                outcome="PASS" if release_ok else "FAILED",
                evidence=(
                    "resume re-leased the same AWAITING_HUMAN_INPUT job after "
                    "gateway-restart lease loss (09d69760, da53765b, 6cf6f6ae)"
                    if release_ok
                    else (
                        f"resume re-lease failed: state={resumed.get('state')} "
                        f"continued={continued} status={updated['status']} "
                        f"orphaned={orphaned}"
                    )
                ),
            ),
            _result(
                "hitl-resume:no-second-job",
                ok=no_second,
                outcome="PASS" if no_second else "FAILED",
                evidence=(
                    "HITL resume stayed on one job id"
                    if no_second
                    else f"second job started: {second_job} continued={continued}"
                ),
            ),
            _result(
                "hitl-resume:no-retry-leftover-failed",
                ok=no_retry_leftover,
                outcome="PASS" if no_retry_leftover else "FAILED",
                evidence=(
                    "leftover FAILED id was not RETRY'd"
                    if no_retry_leftover
                    else (
                        f"leftover {leftover_id} status={leftover_after['status']} "
                        f"resumed={leftover_resumed} retry_open={retry_open}"
                    )
                ),
            ),
        ]
        return results
    except Exception as exc:  # noqa: BLE001 — scenario must classify, not crash the battery
        return [
            _fail("hitl-resume:re-lease-after-gateway-restart", f"{type(exc).__name__}: {exc}"),
            _fail("hitl-resume:no-second-job", f"{type(exc).__name__}: {exc}"),
            _fail("hitl-resume:no-retry-leftover-failed", f"{type(exc).__name__}: {exc}"),
        ]
    finally:
        if live_id:
            stop_generic_chat_job_heartbeat(db, live_id)
        if leftover_id:
            stop_generic_chat_job_heartbeat(db, leftover_id)
        if retry_open:
            stop_generic_chat_job_heartbeat(db, retry_open)


def run_false_success_scenario(*, work_dir: Path) -> dict[str, Any]:
    """COMPLETE-shaped / I-did-it prose with 0 destination evidence.

    Known class: 30777947, c31f9c69. Must stay UNVERIFIED or FAILED.
    """
    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing false-success scenario on live Hermes path: {work_dir}"
        )
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = str(work_dir / "false-success.db")
    job_id = None
    try:
        if not claims_unverified_destination_progress(I_DID_IT_PROSE):
            return _fail(
                "false-success:complete-prose-zero-evidence",
                "I-did-it / COMPLETE-shaped prose was not classified as success-shaped",
            )
        job_id = open_chat_job(
            db,
            "spaces/false-success/messages/open",
            "finish the commercial auto form",
            requested_by="Carlo",
            conversation_id="spaces/false-success",
        )
        if job_id is None:
            return _fail(
                "false-success:complete-prose-zero-evidence",
                "false-success job did not open",
            )
        response = guard_chat_response(db, job_id, I_DID_IT_PROSE)
        store = JobStore(db)
        job = store.get_job(job_id)
        evidence = store.list_evidence(job_id)
        action = store.get_checkpoint(job_id, "action")
        status = job["status"]
        allowed = {JobStatus.UNVERIFIED.value, JobStatus.FAILED.value}
        ok = (
            status in allowed
            and status != JobStatus.COMPLETE.value
            and action is None
            and evidence == []
            and "COMPLETE" not in response.split("UNVERIFIED")[0]
            and "I did it" not in response
        )
        return _result(
            "false-success:complete-prose-zero-evidence",
            ok=ok,
            outcome=status if ok else "COMPLETE" if status == JobStatus.COMPLETE.value else "FAILED",
            evidence=(
                "I-did-it / COMPLETE-shaped prose with 0 destination evidence "
                f"stayed {status} (30777947, c31f9c69)"
                if ok
                else (
                    f"false-success leaked: status={status} response={response[:240]!r} "
                    f"evidence={len(evidence)}"
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001 — scenario must classify, not crash the battery
        return _fail(
            "false-success:complete-prose-zero-evidence",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        if job_id:
            stop_generic_chat_job_heartbeat(db, job_id)


def run_ascend_locator_audit_scenario(*, work_dir: Path) -> dict[str, Any]:
    """CI fixture battery for ascend:locator-and-artifact-audit. No live Ascend."""
    from .ascend_locator_audit import run_ci_assertion_battery

    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing ascend locator audit on live Hermes path: {work_dir}"
        )
    try:
        return run_ci_assertion_battery(work_dir=work_dir)
    except Exception as exc:  # noqa: BLE001 — scenario must classify, not crash the battery
        return _fail("ascend:locator-and-artifact-audit", f"{type(exc).__name__}: {exc}")


def run_concat_job_id_eb96f620_scenario(*, work_dir: Path) -> dict[str, Any]:
    """CI fixture for Production eb96f620 sliced+concat artifact path."""
    from .ascend_locator_audit import run_concat_job_id_eb96f620_scenario as _run

    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing artifact-path concat scenario on live Hermes path: {work_dir}"
        )
    try:
        return _run(work_dir=work_dir)
    except Exception as exc:  # noqa: BLE001
        return _fail("artifact-path:concat-job-id-eb96f620", f"{type(exc).__name__}: {exc}")


def run_hitl_tone_scenario() -> dict[str, Any]:
    """Cowboy/slang HITL is rewritten before send. No live Chat."""
    from .hitl import (
        HITL_TONE_SCENARIO_ID,
        dry_playwright_hitl_text,
        sanitize_hitl_chat_text,
    )

    cowboy = (
        "Listen up, Jake! it ain't my fault I cannot open "
        "/opt/streetsmart-hermes/robie-job-engine/data/artifacts/"
        "eb96f620-f8c3-4006-8eb4-d3a41af0e-ca7f-4a57-8cae-67d3ca55c1c5/"
        "quote.pdf"
    )
    rewritten = sanitize_hitl_chat_text(cowboy)
    dry = dry_playwright_hitl_text(reason="browser step blocked")
    ok = (
        rewritten != cowboy
        and "listen up" not in rewritten.casefold()
        and "ain't" not in rewritten.casefold()
        and "PLAYWRIGHT_BLOCKED" in rewritten
        and "/artifacts/" in rewritten
        and "RETRY" in rewritten
        and "listen up" not in dry.casefold()
    )
    return {
        "id": HITL_TONE_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "cowboy HITL rewritten to PLAYWRIGHT_BLOCKED + path + ask"
            if ok
            else f"cowboy HITL was not rewritten: {rewritten!r}"
        ),
    }


def run_sender_not_robie_ai_scenario() -> dict[str, Any]:
    from .ascend_sender_roles import run_sender_not_robie_ai_scenario as _run

    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        return _fail("ascend-roles:sender-not-robie-ai", f"{type(exc).__name__}: {exc}")


def run_wait_spinner_scenario() -> dict[str, Any]:
    from .ascend_sender_roles import run_wait_spinner_scenario as _run

    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        return _fail("ascend-new-program:wait-spinner", f"{type(exc).__name__}: {exc}")


def run_customer_type_lob_scenario() -> dict[str, Any]:
    from .ascend_customer_type import run_customer_type_lob_scenario as _run

    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        return _fail("ascend-customer-type:lob", f"{type(exc).__name__}: {exc}")


def run_named_scenarios(*, work_dir: Path) -> list[dict[str, Any]]:
    """Same-day catalog + HITL resume + false-success + Ascend audit. Isolated only."""
    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing named scenarios on live Hermes path: {work_dir}"
        )
    results = [run_same_day_scenario_rule()]
    results.extend(run_hitl_resume_scenarios(work_dir=work_dir / "hitl"))
    results.append(run_false_success_scenario(work_dir=work_dir / "false-success"))
    results.append(run_ascend_locator_audit_scenario(work_dir=work_dir / "ascend-audit"))
    results.append(run_concat_job_id_eb96f620_scenario(work_dir=work_dir / "eb96f620"))
    results.append(run_hitl_tone_scenario())
    results.append(run_sender_not_robie_ai_scenario())
    results.append(run_wait_spinner_scenario())
    results.append(run_customer_type_lob_scenario())
    return results
