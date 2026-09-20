"""Named deterministic battery scenarios. Previously seen failures only.

Same-day rule: every Production incident that was a NEW failure mode gets
a named scenario here before we call the incident closed. That is how the
simulator grows. Simulator passed still only means known scenarios did not
regress.

No live EZLynx. No Production jobs.db. No @robie.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
    "ASCEND roles class returned: log the Producer and Account Manager "
    "prefills. Jake → Jake Ferrara jake@streetsmart.insurance. "
    "Carlo → Carlo Ferrara carlo@streetsmart.insurance. Name-only "
    "Carlo Ferrara is FAIL (two Carlo rows). FAIL if they stay Robie AI "
    "when requested_by is Carlo or Jake. Unknown sender HITL."
)

ASCEND_SPINNER_CHAT = (
    "ASCEND New program class returned: log seconds until the unique "
    "primary New program is ready; click that primary, never the caret; "
    "wait_for_url /create/new. Timeout is PLAYWRIGHT_BLOCKED. No Gemini."
)

ASCEND_ACCESSIBLE_NAME_CHAT = (
    "ASCEND New program accessible-name class returned (f7653a85): the "
    "unique primary accessible name is exactly New program. The plus is "
    "an icon, not text. get_by_role(button, name='+ New program', "
    "exact=True) never matches. Never the caret. No Gemini."
)

ASCEND_AGENCY_FEE_CHAT = (
    "ASCEND Agency Fee default class returned: log the /create/new default "
    "(expect $0.00 or empty); set 500 in the Test run if the field exists; "
    "then STOP before Save program / Send email / checkout / payment / bind."
)

ASCEND_LISTBOX_CHAT = (
    "ASCEND create/new listbox class returned (38c0fa79): open each "
    "create-form combobox; the intended option needs a unique locator. "
    "Producer / Account Manager unique option is the concatenated "
    "Name+email label, not the display name alone. Two matches is "
    "PLAYWRIGHT_BLOCKED. Log the blocked field. No Save. No PAWIVA. "
    "Carlo will not RETRY 38c0fa79."
)

ASCEND_TOO_SOON_CHAT = (
    "ASCEND create/new too-soon 0-element class returned: after "
    "wait_for_url /create/new the form is not ready. Wait until the unique "
    "exact Import document primary is visible; log seconds. Immediate "
    "get_by_role(button, name='Import document') resolving to 0 elements "
    "is FAIL. No Gemini. No .first/.nth/.last."
)

ASCEND_CUSTOMER_TYPE_CHAT = (
    "ASCEND customer type class returned: Commercial vs Personal is from "
    "line of business, not the form default and not LLC vs person-name. "
    "Missing LOB is HITL. Do not guess."
)

FOLLOW_TAB_CHAT = (
    "FOLLOW-TAB class returned (807f8920, 468d1575, 30777947): "
    "recorder stayed on a stale listing while Playwright drove "
    "Edit/FormEntry/documents. Capture must follow the live Playwright tab. "
    "Not proven on a live job until recorder URL == Playwright URL."
)

ACTION_GATE_CHAT = (
    "ACTION GATE class returned (807f8920, 38c0fa79): Production Chat jobs "
    "that target Ascend / premium finance / PAWIVA / create-program must be "
    "REFUSED before Playwright, CDP, or any Ascend click unless a recorded "
    "clean Test API creation and fresh-readback PASS exists for that exact action. HITL after a "
    "miss is not the gate. The Job Engine refuses; this is not a memory item."
)

PLAYWRIGHT_SILENT_CHAT = (
    "PLAYWRIGHT SILENT-GAP class returned (1df9740b): a Chat/EZLynx "
    "job closed UNVERIFIED from heartbeat + leftover recorder tab with "
    "zero playwright_exec rows. Persist every tool call. Snapshot CDP "
    "tabs. Zero tool rows is FAILED, not UNVERIFIED."
)

PLAYWRIGHT_CDP_CHAT = (
    "PLAYWRIGHT CDP-SNAPSHOT class returned: job start/end must write "
    "Chrome /json/list url+title checkpoints from a fixture payload. "
    "No cookies. No websocket debugger URLs. Leftover Ascend vs EZLynx "
    "must be visible without a live browser."
)

UNVERIFIED_UNMASK_CHAT = (
    "UNVERIFIED-UNMASK class returned (d5fd2307): a Chat job that never "
    "claimed destination success and never emitted a destination action "
    "checkpoint closed UNVERIFIED with 'no structured destination action "
    "checkpoint' after CDP ECONNREFUSED / PLAYWRIGHT_BLOCKED. Infra fail "
    "is FAILED or HITL with the real last error. COMPLETE stays blocked. "
    "Claimed progress without evidence stays UNVERIFIED."
)

ACTION_GATE_SCENARIO_ID = "action-gate:test-pass-required-before-production"
PLAYWRIGHT_SILENT_SCENARIO_ID = "playwright-silent:zero-tool-rows-1df9740b"
PLAYWRIGHT_CDP_SCENARIO_ID = "playwright-cdp:json-list-fixture"
UNVERIFIED_UNMASK_SCENARIO_ID = "unverified-unmask:infra-error-not-checkpoint"
CHAT_SHAPED_ASCEND = (
    "@robie create a program in Ascend for PAWIVA premium finance"
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
        "document-upload:content-and-retry",
        "mortgagee:email-metadata-before-lob",
        "same-day:named-scenario-before-close",
        "message-intake:queue-during-active-work",
        "hitl-resume:re-lease-after-gateway-restart",
        "hitl-resume:no-second-job",
        "hitl-resume:no-retry-leftover-failed",
        "false-success:complete-prose-zero-evidence",
        "ascend:locator-and-artifact-audit",
        "artifact-path:concat-job-id-eb96f620",
        "hitl-tone:dry-playwright-blocked",
        "ascend-roles:sender-not-robie-ai",
        "ascend-new-program:wait-spinner",
        "ascend-new-program:accessible-name",
        "ascend-new-program:spinner-timing",
        "ascend-create:agency-fee-default",
        "ascend-create:too-soon-zero-element",
        "ascend-create:unique-listbox-option",
        "ascend-customer-type:lob",
        "recording:follow-live-playwright-tab",
        "action-gate:test-pass-required-before-production",
        "playwright-silent:zero-tool-rows-1df9740b",
        "playwright-cdp:json-list-fixture",
        "unverified-unmask:infra-error-not-checkpoint",
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
        and "PLAYWRIGHT_BLOCKED" not in rewritten
        and "/artifacts/" in rewritten
        and "RETRY" in rewritten
        and "listen up" not in dry.casefold()
        and "PLAYWRIGHT_BLOCKED" not in dry
    )
    return {
        "id": HITL_TONE_SCENARIO_ID,
        "kind": "logic",
        "ok": ok,
        "outcome": "PASS" if ok else "FAILED",
        "evidence": (
            "cowboy HITL rewritten to plain English + path + ask"
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


def run_accessible_name_scenario() -> dict[str, Any]:
    from .ascend_sender_roles import run_accessible_name_scenario as _run

    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        return _fail("ascend-new-program:accessible-name", f"{type(exc).__name__}: {exc}")


def run_customer_type_lob_scenario() -> dict[str, Any]:
    from .ascend_customer_type import run_customer_type_lob_scenario as _run

    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        return _fail("ascend-customer-type:lob", f"{type(exc).__name__}: {exc}")


def run_spinner_timing_scenario() -> dict[str, Any]:
    from .ascend_create_defaults import run_spinner_timing_scenario as _run

    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        return _fail("ascend-new-program:spinner-timing", f"{type(exc).__name__}: {exc}")


def run_agency_fee_default_scenario() -> dict[str, Any]:
    from .ascend_create_defaults import run_agency_fee_default_scenario as _run

    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        return _fail("ascend-create:agency-fee-default", f"{type(exc).__name__}: {exc}")


def run_too_soon_zero_element_scenario() -> dict[str, Any]:
    from .ascend_create_defaults import run_too_soon_zero_element_scenario as _run

    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "ascend-create:too-soon-zero-element", f"{type(exc).__name__}: {exc}"
        )


def run_unique_listbox_option_scenario() -> dict[str, Any]:
    from .ascend_create_combobox import run_unique_listbox_option_scenario as _run

    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        return _fail("ascend-create:unique-listbox-option", f"{type(exc).__name__}: {exc}")


def run_follow_live_playwright_tab_scenario(*, work_dir: Path) -> dict[str, Any]:
    """Recorder must analyze the Playwright-driven second tab, not the listing.

    Job 807f8920: capture's Playwright connection only saw the first listing
    tab. Chrome already had Edit/FormEntry/documents. first-ezlynx-wins and
    pages-only selection stay on the listing (frozen + MISMATCH). The fix
    merges Chrome /json/list and follows the live tab.
    """
    from .chat_guard import open_chat_job
    from .post_job_audit import frames_show_motion, rgb_frame, run_post_job_audit
    from .recording import RecordingStore
    from .recording_tab import (
        TabCandidate,
        first_ezlynx_wins,
        follow_capture_ticks,
        follow_screencast_frames,
        merge_capture_tabs,
        recorder_tab_mismatch,
        select_recording_tab,
        should_refresh_cdp_connection,
        tab_candidates_from_cdp_payload,
        write_attach_log,
    )

    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing follow-tab scenario on live Hermes path: {work_dir}"
        )
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    policies = "https://app.ezlynx.com/applicantportal/Policies"
    documents = "https://app.ezlynx.com/applicantportal/Documents"
    edit = "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/220250093/83184565"
    formentry = "https://app.ezlynx.com/applicantportal/FormEntry/220250093"
    playwright_text = (
        f"playwright_exec page.goto({edit!r}) then FormEntry {formentry} "
        "and Save and Continue on account 220250093"
    )
    listing = TabCandidate("cdp-listing", policies, 1.0)
    driven = [
        TabCandidate("cdp-driven", documents, 2.0),
        TabCandidate("cdp-driven", edit, 3.0),
        TabCandidate("cdp-driven", formentry, 4.0),
    ]
    colors = {
        policies: (200, 10, 10),
        documents: (10, 200, 10),
        edit: (10, 10, 220),
        formentry: (220, 220, 10),
    }

    def snapshot(tab: TabCandidate) -> bytes:
        return rgb_frame(24, 16, colors.get(tab.url, (0, 0, 0)))

    playwright_ticks = [[listing], *[[listing] for _ in driven]]
    cdp_ticks = [[listing], *[[listing, tab] for tab in driven]]
    old = follow_screencast_frames(
        playwright_ticks, snapshot, selector=first_ezlynx_wins
    )
    pages_only = follow_screencast_frames(
        playwright_ticks, snapshot, selector=select_recording_tab, hint_url=edit
    )
    new = follow_capture_ticks(
        playwright_ticks, cdp_ticks, snapshot, hint_url=edit
    )
    old_motion = frames_show_motion(old["frames"])
    pages_only_motion = frames_show_motion(pages_only["frames"])
    new_motion = frames_show_motion(new["frames"])
    payload = [
        {
            "id": "cdp-listing",
            "type": "page",
            "url": policies,
            "title": "Policies",
        },
        {
            "id": "cdp-driven",
            "type": "page",
            "url": edit,
            "title": "Edit",
        },
    ]
    from_cdp = tab_candidates_from_cdp_payload(payload)
    merged = merge_capture_tabs([listing], from_cdp)
    refresh = should_refresh_cdp_connection([listing], from_cdp, hint_url=edit)
    old_tab = recorder_tab_mismatch(
        {
            "initial_url": old["initial_url"],
            "final_url": old["final_url"],
            "attached_urls": old["attached_urls"],
            "rebinds": old["rebinds"],
            "selection_mode": "first_ezlynx",
        },
        playwright_text,
    )
    new_tab = recorder_tab_mismatch(
        {
            "initial_url": new["initial_url"],
            "final_url": new["final_url"],
            "attached_urls": new["attached_urls"],
            "rebinds": new["rebinds"],
            "selection_mode": new["selection_mode"],
        },
        playwright_text,
    )
    job_id = None
    try:
        db = str(work_dir / "follow-tab.db")
        job_id = open_chat_job(
            db,
            "spaces/follow-tab/messages/open",
            "edit policy 220250093 documents FormEntry",
            requested_by="Carlo",
            conversation_id="spaces/follow-tab",
        )
        store = JobStore(db)
        store.checkpoint(job_id, "worker_response", {"response_text": playwright_text})
        store.transition(
            job_id,
            JobStatus.UNVERIFIED,
            expected={JobStatus.RUNNING},
            error="fixture",
            release_lease=True,
        )
        video = work_dir / "follow-live-playwright-tab.webm"
        video.write_bytes(b"follow-tab-webm")
        write_attach_log(
            video,
            {
                "initial_url": new["initial_url"],
                "final_url": new["final_url"],
                "attached_urls": new["attached_urls"],
                "rebinds": new["rebinds"],
                "selection_mode": new["selection_mode"],
            },
        )
        recordings = RecordingStore(db)
        recording = recordings.create(job_id, video, video.with_suffix(".stop"))
        recordings.update(
            recording["id"],
            status="READY",
            drive_url="https://drive.google.com/file/d/follow-tab/view",
            drive_file_id="follow-tab",
        )
        session = work_dir / "sessions"
        session.mkdir(parents=True, exist_ok=True)
        (session / "job.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "tool": "playwright_exec",
                    "code": f"page.goto({edit!r}); page.click('text=Save and Continue')",
                }
            ),
            encoding="utf-8",
        )
        audit = run_post_job_audit(
            db,
            job_id,
            session_root=session,
            extract_frames=lambda _path: new["frames"],
        )
        stale_stayed = (
            old["final_url"] == policies
            and pages_only["final_url"] == policies
            and old_motion["result"] == "FAIL"
            and pages_only_motion["result"] == "FAIL"
            and old_tab["result"] == "MISMATCH"
        )
        followed = (
            refresh
            and any(tab.url == edit for tab in merged)
            and new["final_url"] == formentry
            and edit in new["attached_urls"]
            and new["rebinds"] >= 1
            and new_motion["result"] == "PASS"
            and new_tab["result"] == "MATCH"
            and audit["recording_motion"]["result"] == "PASS"
            and audit["tool_vs_recording"]["result"] == "MATCH"
            and "wrong-tab" not in str(audit["recording_motion"].get("reason") or "")
        )
        ok = stale_stayed and followed
        return _result(
            "recording:follow-live-playwright-tab",
            ok=ok,
            outcome="PASS" if ok else "FAILED",
            evidence=(
                "Playwright-driven second tab is what the recording analyzes "
                "(807f8920 / 468d1575); pages-only / first-ezlynx stays on listing"
                if ok
                else (
                    f"follow-tab leaked: old={old['final_url']} "
                    f"pages_only={pages_only['final_url']} new={new['final_url']} "
                    f"motion={new_motion['result']} tab={new_tab['result']} "
                    f"audit={audit['tool_vs_recording']['result']}"
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001 — scenario must classify, not crash the battery
        return _fail(
            "recording:follow-live-playwright-tab",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        if job_id:
            stop_generic_chat_job_heartbeat(db, job_id)


def run_action_gate_scenario(*, work_dir: Path) -> dict[str, Any]:
    """Production Chat Ascend/create-program without a Test pass must REFUSE.

    Named class: 807f8920 / 38c0fa79 skipped the written Test gate. The
    Job Engine must refuse before any Ascend API request. Test env may
    run. A recorded clean Test pass unblocks N=1. Leftover Production ids
    must not RETRY around the gate.
    """
    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing action-gate scenario on live Hermes path: {work_dir}"
        )
    from .action_gate import (
        CREATE_PROGRAM_ACTION,
        REFUSAL_TOKEN,
        classify_action,
        hold_reason_for_job,
        record_test_action_pass,
        refuse_playwright_start,
    )
    from .recording import RecordingStore

    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = str(work_dir / "action-gate.db")
    passes = work_dir / "passes"
    passes.mkdir(parents=True, exist_ok=True)
    # The non-Ascend release supersedes the old N=1 enablement path: both Test
    # and Production must now refuse before a worker, recorder, API, or browser
    # can start, even if historical Test-pass evidence exists.
    try:
        from .recording import RecordingStore

        for index, environment in enumerate(("TEST", "PRODUCTION")):
            with patch.dict(os.environ, {"ROBIE_ENV": environment}, clear=False):
                job_id = open_chat_job(
                    db,
                    f"spaces/action-gate/messages/non-ascend-{index}",
                    CHAT_SHAPED_ASCEND,
                    requested_by="Carlo Ferrara",
                    conversation_id=f"spaces/action-gate-{index}",
                )
            job = JobStore(db).get_job(job_id)
            if (
                job["action_type"] != "hermes.unavailable"
                or job["status"] != JobStatus.FAILED.value
                or "ASCEND_UNAVAILABLE" not in str(job.get("last_error") or "")
                or RecordingStore(db).latest(job_id) is not None
            ):
                return _fail(
                    ACTION_GATE_SCENARIO_ID,
                    f"{environment} did not fail closed before execution: {job}",
                )
        return _result(
            ACTION_GATE_SCENARIO_ID,
            ok=True,
            outcome="PASS",
            evidence=(
                "Ascend is unavailable in Test and Production; no worker or "
                "recording started and no historical pass can enable it"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(ACTION_GATE_SCENARIO_ID, f"{type(exc).__name__}: {exc}")

    errors: list[str] = []
    refused_id: str | None = None
    try:
        classified = classify_action(
            CHAT_SHAPED_ASCEND,
            payload={"text": CHAT_SHAPED_ASCEND, "skill": "ascend-api-create-program"},
            action_type="hermes.google_chat_task",
        )
        if classified != CREATE_PROGRAM_ACTION:
            errors.append(f"Chat payload hid the action: {classified!r}")

        with patch("robie_job_engine.action_gate.PASSES_DIR", passes):
            prod_reason = hold_reason_for_job(
                {
                    "id": "new-chat",
                    "action_type": "hermes.google_chat_task",
                    "payload": {"text": CHAT_SHAPED_ASCEND},
                },
                env="PRODUCTION",
            )
            if not prod_reason or REFUSAL_TOKEN not in prod_reason:
                errors.append(f"Production without a record did not refuse: {prod_reason!r}")
            if prod_reason and "PLAYWRIGHT_BLOCKED" in prod_reason:
                errors.append("refuse reason used PLAYWRIGHT_BLOCKED")
            test_reason = hold_reason_for_job(
                {
                    "id": "new-chat",
                    "action_type": "hermes.google_chat_task",
                    "payload": {"text": CHAT_SHAPED_ASCEND},
                },
                env="TEST",
            )
            if test_reason:
                errors.append(f"Test env was refused: {test_reason!r}")

            with patch.dict(os.environ, {"ROBIE_ENV": "PRODUCTION"}, clear=False):
                refused_id = open_chat_job(
                    db,
                    "spaces/action-gate/messages/prod",
                    CHAT_SHAPED_ASCEND,
                    requested_by="Carlo Ferrara",
                    conversation_id="spaces/action-gate-prod",
                )
            if not refused_id:
                errors.append("Production Chat job was not opened so it could be refused")
            else:
                store = JobStore(db)
                refused = store.get_job(refused_id)
                if refused["status"] != JobStatus.FAILED.value:
                    errors.append(f"Production Chat status={refused['status']} not FAILED")
                if REFUSAL_TOKEN not in str(refused.get("last_error") or ""):
                    errors.append(f"missing refuse token: {refused.get('last_error')}")
                if RecordingStore(db).latest(refused_id):
                    errors.append("Production refuse started a recorder")
                if refused["action_type"] != "ascend.create_program":
                    errors.append(
                        "Ascend create request did not route to the bounded API job: "
                        f"{refused['action_type']}"
                    )

            leftover_reason = hold_reason_for_job(
                {
                    "id": "807f8920-leftover",
                    "action_type": "hermes.google_chat_task",
                    "payload": {"text": "RETRY"},
                },
                env="PRODUCTION",
            )
            if not leftover_reason or "807f8920" not in (leftover_reason or ""):
                errors.append(f"leftover 807f8920 was allowed to RETRY: {leftover_reason!r}")

            pw = refuse_playwright_start(
                'page.goto("https://dashboard.useascend.com/create/new")',
                env="PRODUCTION",
            )
            if not pw or REFUSAL_TOKEN not in pw:
                errors.append(f"Playwright start was not refused: {pw!r}")
            if pw and "PLAYWRIGHT_BLOCKED" in pw:
                errors.append("Playwright refuse used PLAYWRIGHT_BLOCKED")

            record_test_action_pass(
                CREATE_PROGRAM_ACTION,
                job_id="test-punch-list-pass",
                verdict="PASS",
                extra={"source": "named-scenario"},
            )
            allowed = hold_reason_for_job(
                {
                    "id": "after-pass",
                    "action_type": "hermes.google_chat_task",
                    "payload": {"text": CHAT_SHAPED_ASCEND},
                },
                env="PRODUCTION",
            )
            if allowed:
                errors.append(f"record present still refused: {allowed!r}")
            leftover_after_pass = hold_reason_for_job(
                {
                    "id": "38c0fa79",
                    "action_type": "hermes.google_chat_task",
                    "payload": {"text": CHAT_SHAPED_ASCEND},
                },
                env="PRODUCTION",
            )
            if not leftover_after_pass:
                errors.append("leftover 38c0fa79 RETRY was allowed after a Test pass")
    except Exception as exc:  # noqa: BLE001 — scenario must classify, not crash
        return _fail(ACTION_GATE_SCENARIO_ID, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            stop_generic_chat_job_heartbeat(db, refused_id)  # type: ignore[name-defined]
        except Exception:
            pass
    ok = not errors
    return _result(
        ACTION_GATE_SCENARIO_ID,
        ok=ok,
        outcome="PASS" if ok else "FAILED",
        evidence=ACTION_GATE_CHAT if ok else "; ".join(errors),
    )


def run_zero_playwright_tool_row_scenario(*, work_dir: Path) -> dict[str, Any]:
    """1df9740b: EZLynx Chat job with zero playwright_exec rows is FAILED."""
    from .playwright_observability import ZERO_PLAYWRIGHT_TOOL_ROWS

    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing playwright-silent scenario on live Hermes path: {work_dir}"
        )
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = str(work_dir / "silent-gap.db")
    job_id = None
    try:
        job_id = open_chat_job(
            db,
            "spaces/1df9740b/messages/open",
            "Finish ROBIE Test LLC 220250093 commercial auto in EZLynx",
            requested_by="Carlo",
            conversation_id="spaces/1df9740b",
        )
        if job_id is None:
            return _fail(PLAYWRIGHT_SILENT_SCENARIO_ID, "silent-gap job did not open")
        response = guard_chat_response(
            db,
            job_id,
            "I entered the vehicles and drivers. The commercial auto is done.",
        )
        store = JobStore(db)
        job = store.get_job(job_id)
        rows = store.list_playwright_exec(job_id)
        ok = (
            job["status"] == JobStatus.FAILED.value
            and job["status"] != JobStatus.UNVERIFIED.value
            and job["status"] != JobStatus.COMPLETE.value
            and rows == []
            and store.list_attempts(job_id) == []
            and "PLAYWRIGHT_SILENT" in str(job.get("last_error") or "")
            and "FAILED" in response
            and "— UNVERIFIED" not in response
            and ZERO_PLAYWRIGHT_TOOL_ROWS[:20] in str(job.get("last_error") or "")
        )
        return _result(
            PLAYWRIGHT_SILENT_SCENARIO_ID,
            ok=ok,
            outcome="PASS" if ok else "FAILED",
            evidence=(
                PLAYWRIGHT_SILENT_CHAT
                if ok
                else (
                    f"silent-gap leaked: status={job['status']} "
                    f"rows={len(rows)} response={response[:240]!r}"
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001 — scenario must classify, not crash
        return _fail(PLAYWRIGHT_SILENT_SCENARIO_ID, f"{type(exc).__name__}: {exc}")
    finally:
        if job_id:
            stop_generic_chat_job_heartbeat(db, job_id)


def run_unverified_unmask_scenario(*, work_dir: Path) -> dict[str, Any]:
    """d5fd2307: infra ECONNREFUSED is FAILED, not UNVERIFIED checkpoint mask."""
    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing unverified-unmask scenario on live Hermes path: {work_dir}"
        )
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = str(work_dir / "unverified-unmask.db")
    infra = (
        "Could not attach to persistent Chrome: "
        "[Errno 111] ECONNREFUSED Connection refused (127.0.0.1:9222)"
    )
    job_id = None
    claimed_id = None
    try:
        job_id = open_chat_job(
            db,
            "spaces/d5fd2307/messages/open",
            "Gold Eagle Bond Chat quote",
            requested_by="Carlo",
            conversation_id="spaces/d5fd2307",
        )
        if job_id is None:
            return _fail(UNVERIFIED_UNMASK_SCENARIO_ID, "unmask job did not open")
        response = guard_chat_response(db, job_id, infra)
        store = JobStore(db)
        job = store.get_job(job_id)
        claimed_id = open_chat_job(
            db,
            "spaces/d5fd2307/messages/claimed",
            "Gold Eagle Bond Chat quote",
            requested_by="Carlo",
            conversation_id="spaces/d5fd2307-claimed",
        )
        claimed_response = guard_chat_response(db, claimed_id, I_DID_IT_PROSE)
        claimed = store.get_job(claimed_id)
        ok = (
            job["status"] == JobStatus.FAILED.value
            and job["status"] != JobStatus.UNVERIFIED.value
            and job["status"] != JobStatus.COMPLETE.value
            and "ECONNREFUSED" in str(job.get("last_error") or "")
            and "no structured destination action checkpoint"
            not in str(job.get("last_error") or "")
            and store.get_checkpoint(job_id, "action") is None
            and store.list_evidence(job_id) == []
            and claimed["status"] == JobStatus.UNVERIFIED.value
            and claimed["status"] != JobStatus.COMPLETE.value
            and "UNVERIFIED" in claimed_response
            and "FAILED" in response
        )
        return _result(
            UNVERIFIED_UNMASK_SCENARIO_ID,
            ok=ok,
            outcome="PASS" if ok else "FAILED",
            evidence=(
                UNVERIFIED_UNMASK_CHAT
                if ok
                else (
                    f"unmask leaked: infra={job['status']} "
                    f"claimed={claimed['status']} response={response[:240]!r}"
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001 — scenario must classify, not crash
        return _fail(UNVERIFIED_UNMASK_SCENARIO_ID, f"{type(exc).__name__}: {exc}")
    finally:
        if job_id:
            stop_generic_chat_job_heartbeat(db, job_id)
        if claimed_id:
            stop_generic_chat_job_heartbeat(db, claimed_id)


def run_cdp_json_list_fixture_scenario(*, work_dir: Path) -> dict[str, Any]:
    """Write CDP url+title checkpoints from fixture /json/list. No live browser."""
    from .playwright_observability import (
        CDP_END_CHECKPOINT,
        CDP_START_CHECKPOINT,
        persist_cdp_snapshot,
    )

    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing cdp-snapshot scenario on live Hermes path: {work_dir}"
        )
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = str(work_dir / "cdp-fixture.db")
    job_id = None
    fixture = [
        {
            "id": "leftover",
            "type": "page",
            "title": "New program",
            "url": "https://dashboard.useascend.com/create/new",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/secret",
            "cookies": [{"name": "sid", "value": "cookie-secret"}],
        },
        {
            "id": "ezlynx",
            "type": "page",
            "title": "EZLynx",
            "url": "https://app.ezlynx.com/web/",
        },
    ]
    try:
        job_id = open_chat_job(
            db,
            "spaces/cdp-fixture/messages/open",
            "open EZLynx for the commercial auto",
            requested_by="Carlo",
            conversation_id="spaces/cdp-fixture",
        )
        if job_id is None:
            return _fail(PLAYWRIGHT_CDP_SCENARIO_ID, "cdp fixture job did not open")
        store = JobStore(db)
        persist_cdp_snapshot(store, job_id, "start", payload=fixture)
        persist_cdp_snapshot(store, job_id, "end", payload=fixture)
        start = store.get_checkpoint(job_id, CDP_START_CHECKPOINT) or {}
        end = store.get_checkpoint(job_id, CDP_END_CHECKPOINT) or {}
        blob = json.dumps(start) + json.dumps(end)
        urls = [tab.get("url") for tab in (start.get("tabs") or [])]
        ok = (
            start.get("phase") == "start"
            and end.get("phase") == "end"
            and any("useascend.com/create/new" in str(url) for url in urls)
            and any("app.ezlynx.com/web" in str(url) for url in urls)
            and "cookie-secret" not in blob
            and "webSocketDebuggerUrl" not in blob
            and "ws://127.0.0.1:9222/devtools/page/secret" not in blob
        )
        return _result(
            PLAYWRIGHT_CDP_SCENARIO_ID,
            ok=ok,
            outcome="PASS" if ok else "FAILED",
            evidence=PLAYWRIGHT_CDP_CHAT if ok else f"cdp snapshot missing: {blob[:240]}",
        )
    except Exception as exc:  # noqa: BLE001 — scenario must classify, not crash
        return _fail(PLAYWRIGHT_CDP_SCENARIO_ID, f"{type(exc).__name__}: {exc}")
    finally:
        if job_id:
            stop_generic_chat_job_heartbeat(db, job_id)


def run_message_intake_scenario(*, work_dir: Path) -> dict[str, Any]:
    from .engine import resolve_worker_name
    from .email_guard import EmailTaskPending, run_guarded_email_task
    from .runs import IsolatedRunStore, RunIsolationError
    scenario = "message-intake:queue-during-active-work"
    work_dir.mkdir(parents=True, exist_ok=True)
    db = work_dir / "jobs.db"
    runs = IsolatedRunStore(db)
    active = None
    try:
        assert resolve_worker_name("audit_verification", {"worker": "audit"}) == "audit-verification"
        active = runs.start(owner="existing-worker", job_id="existing")
        receipt = runs.record_intake(owner="chat-intake", job_id="new-chat", payload={"message_id": "queued"})
        assert receipt["status"] == "INTAKE"
        assert runs.active_run()["id"] == active["id"]
        called = []
        try:
            run_guarded_email_task(db_path=str(db), gmail_message_id="queued-email", prompt="work", run_agent=lambda p: called.append(p) or "done")
        except EmailTaskPending:
            pass
        else:
            raise AssertionError("busy email was acknowledged as terminal")
        assert not called
        try:
            runs.start(owner="second-worker", job_id="new-chat")
        except RunIsolationError:
            pass
        else:
            raise AssertionError("concurrent execution was allowed")
        return _result(scenario, ok=True, outcome="PASS", evidence="Legacy routing resolves; chat receipt and email queue survive active work without parallel execution")
    except Exception as exc:
        return _fail(scenario, f"{type(exc).__name__}: {exc}")
    finally:
        if active:
            runs.terminate(active["id"], "COMPLETE")


def run_mortgagee_email_scope_scenario(*, work_dir: Path) -> dict[str, Any]:
    # Legacy worker test modules install sibling fakes during discovery.
    # A fresh interpreter proves the real shared-module persistence contract.
    import subprocess
    import sys
    scenario = "mortgagee:email-metadata-before-lob"
    code = (
        "import json,sys; from pathlib import Path; "
        "from robie_job_engine.regression_scenarios import _mortgagee_email_scope_isolated; "
        "print(json.dumps(_mortgagee_email_scope_isolated(work_dir=Path(sys.argv[1]))))"
    )
    try:
        result = subprocess.run([sys.executable, "-c", code, str(work_dir.resolve())],
                                capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)
    except Exception as exc:
        return _fail(scenario, f"isolated scenario failed: {type(exc).__name__}")


def _mortgagee_email_scope_isolated(*, work_dir: Path) -> dict[str, Any]:
    """Exercise the real worker and durable outcome path with offline reads."""
    from . import mortgagee_verification_worker as worker
    from .report_email_source import project_email_rows

    scenario = "mortgagee:email-metadata-before-lob"
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        store = JobStore(work_dir / "jobs.db")
        job = store.create_job("mortgagee_verification", {
            "as_of": "2026-09-20", "voice_enabled": False,
            "authorized_actions": [], "jobs_db_path": store.path,
        }, idempotency_key="mortgagee-scope-regression")
        raw = {"Policy Number": "TEST-SCOPE", "Applicant ID": "TEST-A",
               "Policy Master ID": "TEST-M", "Task Due Date": "01/01/2025"}
        rows = project_email_rows("4372", [
            {**raw, "Task Status": "Closed", "Task ID": "CLOSED"},
            {**raw, "Task Status": "Open", "Task ID": "OPEN"},
        ])

        class Lookup:
            def search_policy_by_number(self, number):
                assert number == "TEST-SCOPE"
                return {"data": [{"policyNumber": number, "applicantId": "TEST-A",
                                  "policyMasterId": "TEST-M", "lineOfBusiness": "Flood",
                                  "expirationDate": "2026-10-30"}]}

        with patch.object(worker, "fetch_report_rows", return_value=rows) as fetch:
            result = worker.MortgageeVerificationWorker(store, policy_lookup=Lookup()).perform(
                job, idempotency_key="mortgagee-scope-regression")
        assert fetch.call_args.kwargs["source"] == "email"
        assert result.destination["skipped_closed"] == 1
        assert result.destination["in_scope_count"] == 1
        assert result.destination["skipped_lob"] == 0
        outcome, = result.detail["policy_outcomes"]
        assert outcome["status"] == "pending"
        assert outcome["evidence"]["policy_scope"]["lob"] == "Flood"
        assert "retrieval_intent_recorded" in outcome["actions_taken"]
        saved = store.get_checkpoint(job["id"], "action")
        assert saved["detail"]["policies"][0]["policy_number"] == "TEST-SCOPE"
        return _result(scenario, ok=True, outcome="PASS",
                       evidence="Closed task excluded; open Flood resolved by identity; true expiration used; outcome persisted; no outreach")
    except Exception as exc:
        return _fail(scenario, f"{type(exc).__name__}: {exc}")


def run_document_upload_scenario(*, work_dir: Path) -> dict[str, Any]:
    """Exercise document-only verification and saved-ID retry without network."""
    from .document_upload_reliability import upload_with_receipt, verify_document_request
    from .store import JobStore
    from .models import JobStatus

    class Destination:
        posts = 0
        body = b'synthetic-document-regression'

        def upload_applicant_document(self, *args, **kwargs):
            self.posts += 1
            return '12345'

        def search_applicant_documents(self, applicant):
            return {'results': self.documents_for_applicant(applicant)}

        def documents_for_applicant(self, applicant):
            return [{'id': '12345', 'name': 'synthetic.txt'}]

        def download_document(self, doc_id):
            return self.body

    work_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(work_dir / 'jobs.db')
    job = store.create_job('hermes.email_task', {
        'request_text': 'Upload document "synthetic.txt" to applicant 220250093.',
        'applicant_id': '220250093',
    })
    store.transition(job['id'], JobStatus.RUNNING)
    destination = Destination()
    env = {'ROBIE_JOB_ID': job['id'], 'ROBIE_JOB_DB': store.path}
    try:
        for _ in range(2):
            upload_with_receipt(destination, '220250093', 'synthetic.txt',
                                b'synthetic-document-regression', env=env)
        job = store.get_job(job['id'])
        action = store.get_checkpoint(job['id'], 'action')
        positive = verify_document_request(destination, job, action)
        destination.body = b'wrong-but-nonempty'
        negative = verify_document_request(destination, job, action)
        ok = positive.verified and not negative.verified and destination.posts == 1
    except Exception:
        ok = False
    return {'id': 'document-upload:content-and-retry', 'kind': 'logic', 'ok': ok,
            'outcome': 'PASS' if ok else 'FAILED',
            'evidence': 'Synthetic document-only read-back, wrong-content refusal, and repeat-call single POST.'}


def run_named_scenarios(*, work_dir: Path) -> list[dict[str, Any]]:
    """Same-day catalog + HITL resume + false-success + Ascend audit. Isolated only."""
    if is_live_hermes_path(work_dir):
        raise ProductionGuardError(
            f"refusing named scenarios on live Hermes path: {work_dir}"
        )
    results = [run_same_day_scenario_rule()]
    results.append(run_document_upload_scenario(work_dir=work_dir / 'document-upload'))
    results.append(run_mortgagee_email_scope_scenario(work_dir=work_dir / "mortgagee-scope"))
    results.append(run_message_intake_scenario(work_dir=work_dir / "message-intake"))
    results.extend(run_hitl_resume_scenarios(work_dir=work_dir / "hitl"))
    results.append(run_false_success_scenario(work_dir=work_dir / "false-success"))
    ascend_runtime_present = (Path(__file__).with_name("ascend_api.py").is_file())
    if ascend_runtime_present:
        results.append(run_ascend_locator_audit_scenario(work_dir=work_dir / "ascend-audit"))
        results.append(run_concat_job_id_eb96f620_scenario(work_dir=work_dir / "eb96f620"))
    results.append(run_hitl_tone_scenario())
    if ascend_runtime_present:
        results.append(run_sender_not_robie_ai_scenario())
        results.append(run_wait_spinner_scenario())
        results.append(run_accessible_name_scenario())
        results.append(run_spinner_timing_scenario())
        results.append(run_agency_fee_default_scenario())
        results.append(run_too_soon_zero_element_scenario())
        results.append(run_unique_listbox_option_scenario())
        results.append(run_customer_type_lob_scenario())
    results.append(
        run_follow_live_playwright_tab_scenario(work_dir=work_dir / "follow-tab")
    )
    results.append(run_action_gate_scenario(work_dir=work_dir / "action-gate"))
    results.append(
        run_zero_playwright_tool_row_scenario(work_dir=work_dir / "playwright-silent")
    )
    results.append(
        run_cdp_json_list_fixture_scenario(work_dir=work_dir / "playwright-cdp")
    )
    results.append(
        run_unverified_unmask_scenario(work_dir=work_dir / "unverified-unmask")
    )
    return results
