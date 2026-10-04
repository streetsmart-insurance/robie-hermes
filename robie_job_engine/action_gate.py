"""Action-level Test-before-Production gate.

A job type is not an action. Chat @robie + skill uses
``hermes.google_chat_task``, which is grandfathered at the job-type layer.
Jake / ChatGPT / Claude pastes will skip a written rule. The Job Engine
must REFUSE to start a live Production walk for a gated action unless a
recorded clean Test pass exists for that exact action id.

N=1 recorded clean Test pass unblocks an action (Carlo). N=3 still
applies to flipping ``production_ready`` on a brand-new job type
(``job_type_gate``). HITL after a miss is not the gate. Commercial auto
is the only grandfathered action. Ascend is not grandfathered.

This module never authorizes COMPLETE, never binds, never emails, and
never sends an Ascend API request.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .models import TERMINAL_STATUSES, JobStatus
from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, current_robie_env
from .carrier_browser_policy import hartford_playwright_refusal
from .store import JobStore
from .ezlynx_write_scope import applicant_is_write_allowed, normalize_applicant_id, requested_message_applicant


REQUIRED_CLEAN_TEST_PASSES = 1
CREATE_PROGRAM_ACTION = "ascend.create_program"
COMMERCIAL_AUTO_ACTION = "ezlynx.commercial_auto"
POLICY_SETUP_ACTION = "ezlynx.policy_setup"
REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_DIR = REPO_ROOT / "deploy" / "action_gate"
ACTIONS_PATH = GATE_DIR / "actions.json"
PASSES_DIR = GATE_DIR / "passes"
CHECKPOINT_KIND = "action_gate"
REFUSAL_TOKEN = "ACTION_GATE_REFUSED"

# Tight markers. "producer" / "account manager" alone are EZLynx-common
# and must not classify a commercial-auto Chat job as Ascend.
#
# 264a708f: a Chat message that only *names* the parked Ascend site (e.g.
# to say "don't use Ascend, just do the EZLynx setup") was misclassified as
# ascend.create_program and fail-closed before EZLynx, even though the
# request was ordinary policy-setup work. Root cause: "ascend"/"useascend"
# were treated as sufficient evidence on their own. They are name-only
# mentions and must not, alone, classify a job as an Ascend action — only
# an unambiguous marker (a real Ascend URL, PAWIVA/221398001, premium
# finance wording, or explicit create-program phrasing) may do that by
# itself. A bare name mention still counts, but only *combined with*
# actual create-program intent language (see _looks_like_ascend_action).
STRONG_ASCEND_ACTION_MARKERS = (
    "app.ascend.com",
    "dashboard.useascend.com",
    "premium finance",
    "premium-finance",
    "pawiva",
    "221398001",
    "create a program",
    "create program",
    "new program",
    "create-program",
)
WEAK_ASCEND_NAME_MARKERS = (
    "ascend",
    "useascend",
)
# Back-compat alias: previously this bare list (including "ascend" and
# "useascend") was used directly as sufficient evidence. Keep the name
# for anything still importing it, but classification logic below no
# longer treats a name-only hit as conclusive on its own.
ASCEND_ACTION_MARKERS = STRONG_ASCEND_ACTION_MARKERS + WEAK_ASCEND_NAME_MARKERS
CREATE_PROGRAM_MARKERS = (
    "create a program",
    "create program",
    "new program",
    "create-program",
    "import document",
    "agency fee",
)
SKILL_MARKERS = (
    "ascend-api-create-program",
    "ascend-locator-artifact-audit",
    "ascend.create_program",
    "ascend.locator_artifact_audit",
)
DEFAULT_GATED_ACTIONS = frozenset({CREATE_PROGRAM_ACTION, POLICY_SETUP_ACTION})
DEFAULT_GRANDFATHERED_ACTIONS = frozenset({COMMERCIAL_AUTO_ACTION})
DEFAULT_NO_RETRY_PREFIXES = ("807f8920", "38c0fa79")

CHAT_REFUSE_NOTE = (
    "The system refused because Test has no clean pass for this action. "
    "Production is not the first test. HITL after a miss is not the gate. "
    "Do not send an Ascend API create request until a recorded clean Test "
    "API creation and fresh-readback PASS exists for this exact action."
)
POLICY_SETUP_REFUSE_NOTE = (
    "EZLynx Policy Setup may run only for a compiled-allowlisted applicant. "
    "A missing or different applicant id must refuse before Playwright."
)
LEFTOVER_RETRY_NOTE = (
    "Leftover Production job ids must not RETRY around the action gate. "
    "Carlo will not RETRY 807f8920 or 38c0fa79."
)


class ActionGateError(RuntimeError):
    """Raised when Production would start a gated action without a Test pass."""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _catalog() -> dict[str, Any]:
    return _load_json(ACTIONS_PATH)


def required_clean_test_passes() -> int:
    try:
        value = int(_catalog().get("required_clean_test_passes") or REQUIRED_CLEAN_TEST_PASSES)
    except (TypeError, ValueError):
        value = REQUIRED_CLEAN_TEST_PASSES
    return max(1, value)


def gated_action_ids() -> set[str]:
    listed = {
        str(item.get("id") or "").strip()
        for item in (_catalog().get("gated_actions") or [])
        if isinstance(item, dict)
    }
    return {item for item in listed if item} | set(DEFAULT_GATED_ACTIONS)


def grandfathered_actions() -> set[str]:
    listed = {str(item) for item in (_catalog().get("grandfathered_actions") or [])}
    return set(DEFAULT_GRANDFATHERED_ACTIONS) | listed


def no_retry_job_prefixes() -> tuple[str, ...]:
    listed = tuple(
        str(item).strip().casefold()
        for item in (_catalog().get("no_retry_job_prefixes") or [])
        if str(item).strip()
    )
    return listed or DEFAULT_NO_RETRY_PREFIXES


def pass_record_path(action_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in action_id)
    return PASSES_DIR / f"{safe}.json"


def load_pass_record(action_id: str) -> dict[str, Any] | None:
    path = pass_record_path(action_id)
    if not path.is_file():
        return None
    data = _load_json(path)
    return data or None


def passing_test_count(record: dict[str, Any] | None) -> int:
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


def has_clean_test_pass(action_id: str) -> bool:
    """One recorded clean Test pass is enough to unblock an action."""
    if action_id in grandfathered_actions():
        return True
    return passing_test_count(load_pass_record(action_id)) >= required_clean_test_passes()


def _normalized_blob(*parts: Any) -> str:
    return " ".join(" ".join(str(part or "").casefold().split()) for part in parts)


def _payload_blob(payload: dict[str, Any] | None) -> str:
    blob = dict(payload or {})
    return _normalized_blob(
        blob.get("text"),
        blob.get("action"),
        blob.get("action_id"),
        blob.get("gated_action"),
        blob.get("skill"),
        blob.get("skill_name"),
        blob.get("url"),
        blob.get("programs_url"),
        blob.get("site"),
        blob.get("form"),
        blob.get("scenario"),
        blob.get("account"),
        blob.get("account_name"),
        blob.get("client"),
        blob.get("insured"),
        blob.get("requested_by"),
    )


def classify_action(
    text: str = "",
    *,
    payload: dict[str, Any] | None = None,
    action_type: str | None = None,
    job: dict[str, Any] | None = None,
    code: str | None = None,
) -> str | None:
    """Classify the action the job will perform. Chat job type cannot hide it.

    Returns a gated or grandfathered action id, or None when the request is
    not an action this gate owns.
    """
    job = dict(job or {})
    payload = dict(payload if payload is not None else job.get("payload") or {})
    declared = str(
        payload.get("gated_action")
        or payload.get("action_id")
        or payload.get("action")
        or ""
    ).strip()
    job_type = str(action_type or job.get("action_type") or payload.get("action_type") or "").strip()
    blob = _normalized_blob(text, _payload_blob(payload), job_type, code)
    if (
        declared == POLICY_SETUP_ACTION
        or job_type == POLICY_SETUP_ACTION
        or "ezlynx-policy-setup" in blob
        or "ezlynx.policy_setup" in blob
    ):
        return POLICY_SETUP_ACTION
    if declared in grandfathered_actions() or job_type == COMMERCIAL_AUTO_ACTION:
        if not _looks_like_ascend_action(blob, job_type, declared):
            return COMMERCIAL_AUTO_ACTION
    if declared in gated_action_ids():
        return declared
    if job_type.startswith("ascend."):
        if job_type == "ascend.locator_artifact_audit":
            return CREATE_PROGRAM_ACTION
        if job_type in gated_action_ids():
            return job_type
        return CREATE_PROGRAM_ACTION
    if _looks_like_ascend_action(blob, job_type, declared):
        return CREATE_PROGRAM_ACTION
    if declared == COMMERCIAL_AUTO_ACTION or job_type == COMMERCIAL_AUTO_ACTION:
        return COMMERCIAL_AUTO_ACTION
    return None


def _looks_like_ascend_action(blob: str, job_type: str, declared: str) -> bool:
    if job_type.startswith("ascend.") or declared.startswith("ascend."):
        return True
    if any(marker in blob for marker in SKILL_MARKERS):
        return True
    if any(marker in blob for marker in STRONG_ASCEND_ACTION_MARKERS):
        return True
    if any(marker in blob for marker in CREATE_PROGRAM_MARKERS) and (
        "finance" in blob or "ascend" in blob or "pawiva" in blob
    ):
        return True
    # A bare name-only mention ("ascend"/"useascend") is not, on its own,
    # evidence of an Ascend action — 264a708f. It only counts alongside
    # actual create-program intent language.
    if any(marker in blob for marker in WEAK_ASCEND_NAME_MARKERS) and any(
        marker in blob for marker in CREATE_PROGRAM_MARKERS
    ):
        return True
    return False


def is_no_retry_leftover_job(job_id: str | None) -> bool:
    raw = str(job_id or "").strip().casefold()
    if not raw:
        return False
    for prefix in no_retry_job_prefixes():
        if raw == prefix or raw.startswith(f"{prefix}-") or raw.startswith(prefix):
            return True
    return False


def action_hold_reason(
    action_id: str | None,
    *,
    env: str | None = None,
    job_id: str | None = None,
    applicant_id: str | None = None,
    request_payload: dict[str, Any] | None = None,
) -> str | None:
    """Return a refuse reason when Production must not start this action."""
    environment = (env if env is not None else current_robie_env()).upper()
    leftover = is_no_retry_leftover_job(job_id)
    if leftover and environment in PRODUCTION_ENV_NAMES:
        return (
            f"{REFUSAL_TOKEN}: leftover Production job {job_id} must not RETRY "
            f"around the action gate. {LEFTOVER_RETRY_NOTE}"
        )
    if not action_id:
        return None
    if action_id in grandfathered_actions():
        return None
    if action_id not in gated_action_ids():
        return None
    if environment not in PRODUCTION_ENV_NAMES:
        return None
    if action_id == POLICY_SETUP_ACTION:
        applicant = normalize_applicant_id(applicant_id)
        if applicant_is_write_allowed(applicant) or (
            applicant and requested_message_applicant(request_payload or {}) == applicant
        ):
            return None
        return (
            f"{REFUSAL_TOKEN}: {POLICY_SETUP_REFUSE_NOTE} "
            f"Requested applicant={applicant or '<missing>'}."
        )
    if has_clean_test_pass(action_id):
        return None
    needed = required_clean_test_passes()
    have = passing_test_count(load_pass_record(action_id))
    note = POLICY_SETUP_REFUSE_NOTE if action_id == POLICY_SETUP_ACTION else CHAT_REFUSE_NOTE
    return (
        f"{REFUSAL_TOKEN}: Test has no clean pass for action {action_id} "
        f"({have}/{needed} recorded on hermes-test-01). "
        f"{note}"
    )


def hold_reason_for_job(
    job: dict[str, Any] | None,
    *,
    text: str = "",
    env: str | None = None,
    code: str | None = None,
) -> str | None:
    """The one function the Chat worker / engine / Playwright start must pass."""
    # Permanent carrier rule first: Hartford portal / EBC browser jobs can never
    # run on a hermes-* server (site unreachable from the fleet, proven
    # 2026-09-18). Fail closed with the plain-English reason before any other
    # classification. See robie_job_engine/carrier_browser_policy.py.
    hartford_refusal = hartford_playwright_refusal(
        text=text, job=job, code=str(code or "")
    )
    if hartford_refusal:
        return hartford_refusal
    job = dict(job or {})
    payload = dict(job.get("payload") or {})
    action_id = classify_action(
        text or str(payload.get("text") or ""),
        payload=payload,
        action_type=str(job.get("action_type") or ""),
        job=job,
        code=code,
    )
    return action_hold_reason(
        action_id,
        env=env,
        job_id=str(job.get("id") or ""),
        applicant_id=str(payload.get("applicant_id") or payload.get("account_id") or requested_message_applicant(payload) or ""),
        request_payload=payload,
    )


def is_action_gate_refusal(job: dict[str, Any] | None) -> bool:
    job = dict(job or {})
    error = str(job.get("last_error") or "")
    if REFUSAL_TOKEN in error:
        return True
    checkpoint = job.get("action_gate_checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("refused"):
        return True
    return False


def apply_action_gate(
    store: JobStore,
    job: dict[str, Any] | None,
    *,
    text: str = "",
    env: str | None = None,
) -> dict[str, Any] | None:
    """Refuse a gated Production job before any Ascend API request.

    Returns the updated job when it refused, otherwise None so the caller
    may continue. Dry Chat note is the FAILED reason. Not HITL. Not
    PLAYWRIGHT_BLOCKED.
    """
    job = dict(job or {})
    job_id = str(job.get("id") or "")
    if not job_id:
        return None
    try:
        current = store.get_job(job_id)
    except KeyError:
        return None
    payload = dict(current.get("payload") or {})
    action_id = classify_action(
        text or str(payload.get("text") or ""),
        payload=payload,
        action_type=str(current.get("action_type") or ""),
        job=current,
    )
    reason = action_hold_reason(
        action_id,
        env=env,
        job_id=job_id,
        applicant_id=str(payload.get("applicant_id") or payload.get("account_id") or requested_message_applicant(payload) or ""),
        request_payload=payload,
    )
    store.checkpoint(
        job_id,
        CHECKPOINT_KIND,
        {
            "action_id": action_id,
            "classified_from": "payload/skill/url/site/text",
            "job_type": current.get("action_type"),
            "refused": bool(reason),
            "reason": reason,
            "environment": (env if env is not None else current_robie_env()) or "",
        },
    )
    if not reason:
        return None
    status = JobStatus(current["status"])
    if status in TERMINAL_STATUSES:
        if REFUSAL_TOKEN not in str(current.get("last_error") or ""):
            try:
                return store.transition(
                    job_id,
                    JobStatus.FAILED,
                    expected={status},
                    error=reason,
                    release_lease=True,
                )
            except RuntimeError:
                return store.get_job(job_id)
        return store.get_job(job_id)
    expected = {
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.VERIFYING,
        JobStatus.UNVERIFIED,
        JobStatus.NEEDS_CLARIFICATION,
        JobStatus.WAITING,
        JobStatus.AWAITING_HUMAN_INPUT,
        JobStatus.RETRY_WAIT,
        JobStatus.PAUSED,
        JobStatus.NEEDS_AUTH,
        JobStatus.NEEDS_SKILL,
    }
    if status not in expected:
        expected.add(status)
    return store.transition(
        job_id,
        JobStatus.FAILED,
        expected=expected,
        error=reason,
        release_lease=True,
    )


def refuse_playwright_start(
    code: str = "",
    *,
    job: dict[str, Any] | None = None,
    env: str | None = None,
) -> str | None:
    """Refuse CDP / Playwright before a connection when Production is gated."""
    reason = hold_reason_for_job(job, code=code, env=env)
    if reason:
        return reason
    action_id = classify_action(code=code, job=job)
    return action_hold_reason(action_id, env=env)


def _refusal_note_is_ascend(job: dict[str, Any], error: str) -> bool:
    """True when this refusal is about Ascend, so the Ascend sentence applies."""
    payload = dict(job.get("payload") or {})
    blob = " ".join(
        str(part or "")
        for part in (
            job.get("action_type"),
            payload.get("text"),
            payload.get("action"),
            payload.get("skill"),
            payload.get("skill_name"),
            error,
        )
    ).casefold()
    return any(
        marker in blob
        for marker in (
            "ascend",
            "useascend",
            "premium finance",
            "premium-finance",
            "pawiva",
            "221398001",
        )
    )


def format_action_gate_chat_note(job: dict[str, Any] | None) -> str:
    from . import status_format

    job = dict(job or {})
    job_id = str(job.get("id") or "")
    error = str(job.get("last_error") or CHAT_REFUSE_NOTE)
    if _refusal_note_is_ascend(job, error):
        hold_note = (
            "No Ascend API request was sent. A recorded clean Test API creation "
            "and fresh-readback PASS for this exact action is required."
        )
    else:
        hold_note = (
            "This action was refused before it started. "
            "Nothing was sent and nothing was changed."
        )
    return status_format.render_simple_status(
        headline="Couldn't finish.",
        what_happened=status_format.plain_reason(error),
        anything_needed="Needs a human to review before this action can run.",
        status_line="Blocked \u2014 the action was refused before it started.",
        details=f"{hold_note}\n\nTechnical detail: {error}",
        job_id=job_id,
    )


def record_test_action_pass(
    action_id: str,
    *,
    job_id: str,
    verdict: str,
    environment: str = TEST_ENV_NAME,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one Test pass to the in-repo action record. No SSH required."""
    if str(environment).upper() != TEST_ENV_NAME:
        raise ActionGateError("only TEST passes may be recorded for an action")
    if str(verdict).upper() != "PASS":
        raise ActionGateError("only verdict=PASS may unblock an action")
    if action_id not in gated_action_ids() and action_id not in grandfathered_actions():
        raise ActionGateError(f"unknown action {action_id}")
    PASSES_DIR.mkdir(parents=True, exist_ok=True)
    path = pass_record_path(action_id)
    record = load_pass_record(action_id) or {
        "action_id": action_id,
        "required_clean_test_passes": required_clean_test_passes(),
        "jobs": [],
    }
    jobs = [item for item in list(record.get("jobs") or []) if item.get("job_id") != job_id]
    jobs.append(
        {
            "job_id": job_id,
            "environment": TEST_ENV_NAME,
            "verdict": "PASS",
            **(extra or {}),
        }
    )
    record["jobs"] = jobs
    record["required_clean_test_passes"] = required_clean_test_passes()
    record["passing_count"] = passing_test_count(record)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def collect_passing_action_audits_from_db(
    db_path: str | Path,
    action_id: str,
) -> list[dict[str, Any]]:
    store = JobStore(db_path)
    found: list[dict[str, Any]] = []
    with store.connect() as conn:
        rows = conn.execute("SELECT id, action_type, status, payload_json FROM jobs").fetchall()
    for row in rows:
        payload = {}
        raw = row["payload_json"] if "payload_json" in row.keys() else None
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
        classified = classify_action(
            str((payload or {}).get("text") or ""),
            payload=payload,
            action_type=str(row["action_type"] or ""),
            job={"id": row["id"], "action_type": row["action_type"], "payload": payload},
        )
        if classified != action_id:
            continue
        audit = store.get_checkpoint(str(row["id"]), "post_job_audit") or {}
        punch = store.get_checkpoint(str(row["id"]), "punch_list") or {}
        punch_overall = str(
            punch.get("overall")
            or (punch.get("punch_list") or {}).get("overall")
            or ""
        ).upper()
        verdict = str(audit.get("verdict") or "").upper()
        if verdict != "PASS" and punch_overall != "PASS":
            continue
        found.append(
            {
                "job_id": row["id"],
                "environment": TEST_ENV_NAME,
                "verdict": "PASS",
                "job_status": row["status"],
                "source": "post_job_audit" if verdict == "PASS" else "punch_list",
            }
        )
    return found


def write_passes_from_test_db(db_path: str | Path, action_id: str) -> dict[str, Any]:
    env = current_robie_env()
    if env and env != TEST_ENV_NAME:
        raise ActionGateError(f"refusing to write action passes from ROBIE_ENV={env}; use TEST")
    jobs = collect_passing_action_audits_from_db(db_path, action_id)
    record: dict[str, Any] = {
        "action_id": action_id,
        "required_clean_test_passes": required_clean_test_passes(),
        "jobs": [],
        "passing_count": 0,
    }
    for item in jobs:
        record = record_test_action_pass(
            action_id,
            job_id=str(item["job_id"]),
            verdict="PASS",
            extra={"job_status": item.get("job_status"), "source": item.get("source")},
        )
    return record


def maybe_record_live_test_punch_list(
    *,
    action_id: str,
    job_id: str,
    overall: str,
    live: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record N=1 after a live Test punch-list PASS. Never from CI fixtures."""
    if not live:
        return None
    if current_robie_env() != TEST_ENV_NAME:
        return None
    if str(overall or "").upper() != "PASS":
        return None
    return record_test_action_pass(
        action_id,
        job_id=job_id,
        verdict="PASS",
        extra={"source": "live_punch_list", **(extra or {})},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ROBIE action-level Production gate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Print the registered gated actions (CI sanity)")
    write = sub.add_parser(
        "record-from-db",
        help="Write an action pass record from a Test jobs.db (no SSH to Production)",
    )
    write.add_argument("--db", required=True)
    write.add_argument("--action", required=True)
    args = parser.parse_args(argv)
    if args.command == "check":
        print(
            json.dumps(
                {
                    "ok": True,
                    "required_clean_test_passes": required_clean_test_passes(),
                    "gated_actions": sorted(gated_action_ids()),
                    "grandfathered_actions": sorted(grandfathered_actions()),
                    "no_retry_job_prefixes": list(no_retry_job_prefixes()),
                },
                sort_keys=True,
            )
        )
        return 0
    record = write_passes_from_test_db(args.db, args.action)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if has_clean_test_pass(args.action) or passing_test_count(record) else 2


if __name__ == "__main__":
    raise SystemExit(main())
