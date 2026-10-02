"""Plan, then EZLynx API readback, then Jev, for Chat and email write jobs.

The model states the plan before any write: the write, the target, and the
values. After the work, a fresh EZLynx API read compares each planned value.
That comparison is deterministic. Jev scores the end state afterwards and
does not replace the readback. The user reply quotes the readback.

Questions are not write jobs. They do not take this path.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

PLAN_CHECKPOINT = "write_plan"
READBACK_CHECKPOINT = "write_readback"
JEV_CHECKPOINT = "write_jev_score"

WRITE_ACTIONS = frozenset(
    {
        "ezlynx.policy_change",
        "ezlynx.certificate",
        "ezlynx.quote",
        "ezlynx.commercial_auto",
        "ezlynx.policy_setup",
        "ezlynx.reassign",
        "ezlynx.move_document",
        "ezlynx.apply_label",
        "ezlynx.discussion_note",
    }
)

_TARGET_KEYS = ("applicant_id", "policy_number", "discussion", "discussion_title")
_FENCE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$", re.MULTILINE)

PLAN_MAX_OUTPUT_TOKENS = 4096
REFUSAL_CHECKPOINT = "write_plan_refusal"
MAX_SAME_REFUSAL = 2

_PLAN_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "write": {"type": "STRING"},
        "target": {
            "type": "OBJECT",
            "properties": {
                "applicant_id": {"type": "STRING"},
                "policy_number": {"type": "STRING"},
                "discussion": {"type": "STRING"},
                "discussion_title": {"type": "STRING"},
            },
        },
        "values": {
            "type": "OBJECT",
            "properties": {
                "note_text": {"type": "STRING"},
            },
        },
    },
    "required": ["write", "target", "values"],
}


NO_ACTIVE_JOB_WRITE = "The write was not sent. There is no active job."
TERMINAL_JOB_WRITE = "The write was not sent. This job is already finished."


def plan_refusal(field: str) -> str:
    """Name the field that blocked the write. The write is not sent."""
    name = str(field or "plan").strip() or "plan"
    return (
        f"The plan field {name} is wrong. "
        "Pass plan with write, target, and values. The write was not sent."
    )


def plan_stop_refusal(field: str) -> str:
    """After two refusals the tool tells the model to stop."""
    name = str(field or "plan").strip() or "plan"
    return (
        "Stop. Do not call this tool again. "
        f"Nothing was changed or noted. The plan field {name} is wrong."
    )


def is_plan_refusal_text(text: str) -> bool:
    """True when this outbound text is the plan validator talking to the model.

    That sentence goes back to the model so it can re-plan. It is not a
    user reply, and posting it must not end the job.
    """
    body = " ".join(str(text or "").split())
    if body.startswith("The plan field ") and " is wrong" in body:
        return True
    if body.startswith("Stop. Do not call this tool again.") and "plan field" in body:
        return True
    return False


PLAN_REQUIRED = plan_refusal("plan")


def is_ezlynx_write_job(job: dict[str, Any] | None) -> bool:
    """True for an EZLynx mutation. A question is not a write."""
    if not job:
        return False
    from .chat_job_controls import job_is_hard_blocked

    if job_is_hard_blocked(None, job):
        return False
    from .answer_only import is_answer_only_job

    if is_answer_only_job(job):
        return False
    action = str(job.get("action_type") or "")
    if action in WRITE_ACTIONS:
        return True
    if action not in {"hermes.email_task", "hermes.google_chat_task"}:
        return False
    from .request_routing import classify_request

    classified = classify_request(_job_text(job))
    return classified.action_type in WRITE_ACTIONS


def _job_text(job: dict[str, Any]) -> str:
    payload = dict(job.get("payload") or {})
    return " ".join(
        str(payload.get(key) or "")
        for key in ("text", "request_text", "prompt")
        if str(payload.get(key) or "").strip()
    )


def plan_prompt_for_job(job: dict[str, Any]) -> str:
    """Ask the model for the plan only. No tools and no write."""
    return (
        "State the plan before any write. Reply with one JSON object and nothing else. "
        "Do not call a tool. Do not write.\n"
        '{"write":"<what will be written>","target":{"applicant_id":"","policy_number":"",'
        '"discussion":""},"values":{"<field>":"<exact value>"}}\n'
        "Include only values the request states. Do not invent a value. "
        "write is a string, not an object. "
        "values is an object, not a string. "
        "For a discussion note, write is \"discussion note\", "
        "target.discussion is the existing title, and values.note_text is the exact note. "
        "target must name the account, policy, or discussion. "
        "values must list each field that will change.\n"
        "Request:\n"
        + _job_text(job)
    )


def locked_plan_instructions(plan: Mapping[str, Any]) -> str:
    """The locked plan the worker is allowed to carry out."""
    body = {
        "write": plan.get("write"),
        "target": plan.get("target"),
        "values": plan.get("values"),
    }
    return (
        "Locked plan. Do only this. Do not change any other field.\n"
        + json.dumps(body, sort_keys=True)
    )


def _strip_fence(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = _FENCE.sub("", text).strip()
    return text


def repair_truncated_json(raw: str) -> str:
    """Close a cut-off JSON object. Does not add a field or a value."""
    text = _strip_fence(raw)
    start = text.find("{")
    if start < 0:
        return text
    body = text[start:]
    in_string = False
    escape = False
    depth = 0
    for char in body:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
    if escape:
        body += "\\"
    if in_string:
        body += '"'
    if depth:
        body += "}" * depth
    return body


def _loads_object(text: str) -> dict[str, Any] | None:
    body = str(text or "").strip()
    if not body.startswith("{"):
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _numeric_applicant_id(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    if text.isdigit():
        return text
    return ""


def bound_applicant_id(job: Mapping[str, Any] | None) -> str:
    """The account id this job's name search already bound. A name is not an id."""
    payload = dict((job or {}).get("payload") or {})
    for key in ("applicant_id", "ezlynx_applicant_id", "account_id"):
        found = _numeric_applicant_id(payload.get(key))
        if found:
            return found
    return ""


def _normalize_target(target_raw: Any) -> dict[str, str]:
    """A string target is an account id, a policy number, or a discussion.

    ``applicant_id`` is digits only. A person's name is not an account id.
    """
    if isinstance(target_raw, str):
        text = " ".join(target_raw.split()).strip()
        if not text:
            return {}
        if text.isdigit():
            return {"applicant_id": text}
        # A policy number is one token. "follw up 1" is a discussion title.
        if " " not in text and re.search(r"[A-Za-z]", text) and re.search(r"\d", text):
            return {"policy_number": text}
        return {"discussion": text}
    if not isinstance(target_raw, Mapping):
        return {}
    target: dict[str, str] = {}
    for key in _TARGET_KEYS:
        text = " ".join(str(target_raw.get(key) or "").split()).strip()
        if not text:
            continue
        if key == "applicant_id" and not text.isdigit():
            continue
        target[key] = text
    return target


def plan_with_bound_applicant(
    raw: str | Mapping[str, Any] | None,
    job: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """A locked-ready plan whose applicant id came from the name search."""
    data = raw if isinstance(raw, Mapping) else _plan_object(raw, repair=True)
    bound = bind_plan_applicant(data, job)
    if bound is None:
        return None
    try:
        return _validated_statement(bound)
    except ValueError:
        return None


def bind_plan_applicant(statement: Mapping[str, Any] | None, job: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Use the id from the name search. Never keep a name in ``applicant_id``."""
    if not isinstance(statement, Mapping):
        return None
    data = dict(statement)
    target = _normalize_target(data.get("target"))
    if not _numeric_applicant_id(target.get("applicant_id")):
        target.pop("applicant_id", None)
        bound = bound_applicant_id(job)
        if bound:
            target["applicant_id"] = bound
    data["target"] = target
    return data


def _clean_values(values_raw: Any) -> dict[str, Any]:
    if not isinstance(values_raw, Mapping):
        return {}
    values: dict[str, Any] = {}
    for raw_name, raw_value in values_raw.items():
        name = str(raw_name or "").strip()
        if not name or name.startswith("_"):
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, str) and not raw_value.strip():
            continue
        values[name] = raw_value
    return values


def parse_model_plan(raw: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The model's plan, or None when it did not state one.

    Truncated JSON is closed once. A string target is normalized. No
    value is invented.
    """
    data = _plan_object(raw, repair=True)
    if data is None:
        return None
    try:
        return _validated_statement(data)
    except ValueError:
        return None


def _plan_object(raw: str | Mapping[str, Any] | None, *, repair: bool) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        return dict(raw)
    text = _strip_fence(str(raw or ""))
    data = _loads_object(text)
    if data is None and repair:
        data = _loads_object(repair_truncated_json(text))
    return data


def _statement_if_complete(raw: str | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A plan whose JSON already closed. Truncation does not count."""
    data = _plan_object(raw, repair=False)
    if data is None:
        return None
    try:
        return _validated_statement(data)
    except ValueError:
        return None


def _coerce_write(write: Any) -> str:
    """A string write. An object write contributes its kind, not a dump."""
    if isinstance(write, str):
        return " ".join(write.split()).strip()
    if isinstance(write, Mapping):
        for key in ("write", "kind", "action", "type", "name"):
            picked = " ".join(str(write.get(key) or "").split()).strip()
            if picked:
                return picked
        return ""
    if write is None:
        return ""
    return " ".join(str(write).split()).strip()


def _coerce_values_field(values_raw: Any, args: Mapping[str, Any]) -> Any:
    """values is an object. A string is the note the model already stated."""
    if isinstance(values_raw, str):
        text = " ".join(values_raw.split()).strip()
        if text:
            return {"note_text": text}
        values_raw = {}
    if isinstance(values_raw, Mapping):
        cleaned = _clean_values(values_raw)
        if cleaned:
            return cleaned
    note = " ".join(str(args.get("note_text") or "").split()).strip()
    if note and (values_raw is None or isinstance(values_raw, (str, Mapping))):
        return {"note_text": note}
    return values_raw


def coerce_tool_plan(statement: Any, args: Mapping[str, Any] | None = None) -> Any:
    """Make a stated note plan match the validator. Do not invent a note.

    The tool schema used to leave ``plan`` as an untyped object, so the
    model sent a blank ``write`` or a string ``values``. A plain note
    ("add a note to Buster Brown on follw up 1 saying X") states the
    discussion on the target or title_hint and the sentence on values
    or note_text. Those are the same facts, not new ones. A missing
    plan is still missing.
    """
    if not isinstance(statement, Mapping):
        return statement
    tool_args = dict(args or {})
    data = dict(statement)
    data["write"] = _coerce_write(data.get("write"))
    if "values" in data or tool_args.get("note_text"):
        data["values"] = _coerce_values_field(data.get("values"), tool_args)
    if not _normalize_target(data.get("target")):
        title = " ".join(str(tool_args.get("title_hint") or "").split()).strip()
        if title:
            target = {"discussion": title}
            applicant = _numeric_applicant_id(tool_args.get("applicant_id"))
            if applicant:
                target["applicant_id"] = applicant
            data["target"] = target
    if not str(data.get("write") or "").strip():
        if _normalize_target(data.get("target")) and _clean_values(data.get("values")):
            data["write"] = "discussion note"
    return data


def plan_field_problem(statement: Any) -> str | None:
    """The first wrong field, or None when this plan can lock."""
    data = statement
    if isinstance(statement, str):
        data = _plan_object(statement, repair=True)
    if not isinstance(data, Mapping):
        return "plan"
    if not " ".join(str(data.get("write") or "").split()).strip():
        return "write"
    if not _normalize_target(data.get("target")):
        return "target"
    if not _clean_values(data.get("values")):
        return "values"
    return None


def _validated_statement(data: Mapping[str, Any]) -> dict[str, Any]:
    write = " ".join(str(data.get("write") or "").split()).strip()
    if not write:
        raise ValueError("plan requires write")
    target = _normalize_target(data.get("target"))
    if not target:
        raise ValueError("plan target must name an account, policy, or discussion")
    values = _clean_values(data.get("values"))
    if not values:
        raise ValueError("plan requires at least one value")
    return {"write": write, "target": target, "values": values}


def plan_is_locked(store: Any, job_id: str) -> bool:
    note = store.get_checkpoint(job_id, PLAN_CHECKPOINT) or {}
    return bool(note.get("locked"))


def get_locked_plan(store: Any, job_id: str) -> dict[str, Any] | None:
    note = store.get_checkpoint(job_id, PLAN_CHECKPOINT) or {}
    if not note.get("locked"):
        return None
    return dict(note)


def lock_stated_plan(store: Any, job: Mapping[str, Any], statement: Mapping[str, Any]) -> dict[str, Any]:
    """Lock the model's plan. Does not add a value the model left out."""
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        raise ValueError("plan requires a job")
    existing = get_locked_plan(store, job_id)
    if existing is not None:
        return existing
    clean = _validated_statement(bind_plan_applicant(statement, job) or statement)
    record = {
        "locked": True,
        "source": "model",
        "write": clean["write"],
        "target": clean["target"],
        "values": clean["values"],
    }
    store.checkpoint(job_id, PLAN_CHECKPOINT, record)
    return record


def remember_unlocked_plan(store: Any, job: Mapping[str, Any], raw: str) -> dict[str, Any]:
    job_id = str(job.get("id") or "").strip()
    record = {
        "locked": False,
        "reason": "model did not state a plan",
        "raw_excerpt": str(raw or "")[:400],
    }
    if job_id:
        store.checkpoint(job_id, PLAN_CHECKPOINT, record)
    return record


def prepare_write_plan(
    store: Any,
    job: dict[str, Any],
    model_fn: Callable[[str], str],
) -> dict[str, Any]:
    """Ask the model for the plan and lock it before any write."""
    from .chat_job_controls import job_is_hard_blocked

    if job_is_hard_blocked(store, job):
        return {"skipped": True, "reason": "hard blocked"}
    if not is_ezlynx_write_job(job):
        return {"skipped": True, "reason": "not a write job"}
    existing = get_locked_plan(store, str(job.get("id") or ""))
    if existing is not None:
        return existing
    prompt = plan_prompt_for_job(job)
    raw = _call_plan_model(model_fn, prompt)
    statement = _statement_if_complete(raw)
    if statement is None:
        # One retry. The first reply is often cut off at the token cap.
        retried = _call_plan_model(model_fn, prompt)
        if retried:
            raw = retried
        statement = parse_model_plan(raw)
    statement = plan_with_bound_applicant(statement if statement is not None else raw, job)
    if statement is None:
        return remember_unlocked_plan(store, job, str(raw or ""))
    return lock_stated_plan(store, job, statement)


def _call_plan_model(model_fn: Callable[[str], str], prompt: str) -> str:
    try:
        return str(model_fn(prompt) or "")
    except Exception:
        return ""


def default_plan_model(prompt: str) -> str:
    """Gemini states the plan when ``ROBIE_PLAN_MODEL=1``.

    The flag is off unless set, so a test run does not read Secret Manager
    or call Gemini. The key is ``gemini-api-key``. It is not logged. An
    empty key returns "" and does not open a connection. This is only the
    plan. It is not the readback. Write tools still refuse a job that has
    no locked plan.
    """
    flag = os.environ.get("ROBIE_PLAN_MODEL", "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return ""
    from .gemini_ui_rescue import load_gemini_api_key

    if not load_gemini_api_key():
        return ""
    return _gemini_plan_text(prompt)


def _gemini_plan_text(prompt: str) -> str:
    import json as _json
    import urllib.request

    from .gemini_ui_rescue import GEMINI_MODEL, load_gemini_api_key

    api_key = load_gemini_api_key()
    if not api_key:
        return ""
    model = (GEMINI_MODEL or "gemini-2.5-flash").strip()
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = _json.dumps(
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": PLAN_MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
                "responseSchema": _PLAN_RESPONSE_SCHEMA,
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = _json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    parts = (
        ((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts")
        or []
    )
    return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))


def prepare_chat_write_plan(
    db_path: str,
    job_id: str,
    model_fn: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Lock a Chat write plan before Hermes starts. Failures do not invent one."""
    from .store import JobStore

    try:
        store = JobStore(db_path)
        job = store.get_job(job_id)
    except Exception:
        return {"skipped": True, "reason": "job not available"}
    try:
        return prepare_write_plan(store, job, model_fn or default_plan_model)
    except Exception:
        return remember_unlocked_plan(store, job, "")


def _same_value(expected: Any, observed: Any) -> bool:
    if observed is None:
        return False
    if isinstance(expected, bool) or isinstance(observed, bool):
        return bool(expected) is bool(observed)
    if isinstance(expected, (int, float)) or isinstance(observed, (int, float)):
        try:
            return float(expected) == float(observed)
        except (TypeError, ValueError):
            return False
    left = " ".join(str(expected).split()).casefold()
    right = " ".join(str(observed).split()).casefold()
    return bool(left) and left == right


def _observed_value(observed: Mapping[str, Any], field: str) -> Any:
    if field in observed:
        return observed.get(field)
    folded = field.casefold()
    for key, value in observed.items():
        if str(key).casefold() == folded:
            return value
    return None


def compare_plan_to_api(
    plan: Mapping[str, Any],
    observed: Mapping[str, Any] | None,
    *,
    error: str = "",
) -> dict[str, Any]:
    """Match each planned value to the API payload. No model."""
    values = dict(plan.get("values") or {})
    payload = dict(observed or {})
    fetch_error = str(error or payload.get("_error") or "").strip()
    items: list[dict[str, Any]] = []
    for field, expected in values.items():
        actual = None if fetch_error else _observed_value(payload, field)
        items.append(
            {
                "field": field,
                "expected": expected,
                "observed": actual,
                "matched": _same_value(expected, actual),
            }
        )
    passed = bool(items) and not fetch_error and all(item["matched"] for item in items)
    if fetch_error:
        failure = fetch_error
    elif not items:
        failure = "the plan has no values to read back"
    elif passed:
        failure = ""
    else:
        missing = [item["field"] for item in items if not item["matched"]]
        failure = "planned values not on the EZLynx record: " + ", ".join(missing)
    return {
        "method": "EZLYNX_API",
        "source": "EZLynx API readback",
        "verified": passed,
        "passed": passed,
        "items": items,
        "failure": failure,
    }


def _observed_from_discussion(
    discussion_id: str, plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Map planned values off a re-read discussion. No policy number."""
    try:
        from .ezlynx_api_read_port import EzlynxApiClientReadPort
        from .ezlynx_discussions import _note_body, find_identical_note
    except Exception as exc:
        return {"_error": f"EZLynx API readback was not run: {type(exc).__name__}"}
    try:
        record = EzlynxApiClientReadPort().get_discussion(discussion_id)
    except Exception as exc:
        return {"_error": f"EZLynx API readback failed: {type(exc).__name__}"}
    if not isinstance(record, dict) or not record:
        return {"_error": f"discussion {discussion_id} is not on the EZLynx record"}
    observed: dict[str, Any] = {}
    for name, expected in dict(plan.get("values") or {}).items():
        key = str(name or "")
        if key.casefold() in {"note_text", "note", "body", "text", "note_body"}:
            matched = find_identical_note(record, str(expected or ""))
            observed[name] = _note_body(matched) if matched else None
        elif key in record:
            observed[name] = record.get(key)
        else:
            observed[name] = None
    return observed


def readback_is_live() -> bool:
    """The API read runs on Test, where the discussion API is live.

    ``ROBIE_EZLYNX_API_READBACK=1`` turns it on anywhere. ``0`` turns it off.
    Otherwise it follows ``ROBIE_EZLYNX_DISCUSSION_API=live``.
    """
    flag = os.environ.get("ROBIE_EZLYNX_API_READBACK", "").strip().lower()
    if flag in {"0", "false", "off", "no"}:
        return False
    if flag in {"1", "true", "on", "yes"}:
        return True
    return os.environ.get("ROBIE_EZLYNX_DISCUSSION_API", "").strip().lower() == "live"


def default_ezlynx_observed(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Fresh EZLynx API values for the planned fields. Never a guessed pass."""
    if not readback_is_live():
        return {"_error": "EZLynx API readback was not run"}
    target = dict(plan.get("target") or {})
    policy_number = str(target.get("policy_number") or "").strip()
    try:
        from .ezlynx_api_read_port import EzlynxApiClientReadPort
        from .ezlynx_writers import find_policy_record
        from .evidence_fetchers import _POLICY_FIELD_KEYS, _first_present
    except Exception as exc:
        return {"_error": f"EZLynx API readback was not run: {type(exc).__name__}"}
    discussion_id = str(target.get("discussion_id") or "").strip()
    if not policy_number and discussion_id:
        return _observed_from_discussion(discussion_id, plan)
    if not policy_number:
        return {"_error": "EZLynx API readback needs a policy number on the plan"}
    try:
        payload = EzlynxApiClientReadPort().policy_by_number(policy_number)
        record = find_policy_record(payload, policy_number) or {}
    except Exception as exc:
        return {"_error": f"EZLynx API readback failed: {type(exc).__name__}"}
    if not record:
        return {"_error": f"policy {policy_number} is not on the EZLynx record"}
    observed: dict[str, Any] = {}
    for name in dict(plan.get("values") or {}):
        keys = _POLICY_FIELD_KEYS.get(name, (name,))
        observed[name] = _first_present(record, keys)
    return observed


def _score_with_jev(
    *,
    ask: str,
    plan: Mapping[str, Any],
    readback: Mapping[str, Any],
    worker_text: str,
    client: Any,
) -> Any:
    from .end_state_report import decide, jev_questions
    from .jev_client import JevUnavailable
    from .secrets import redact_mapping

    state = redact_mapping(
        {
            "ask": ask,
            "plan": {
                "write": plan.get("write"),
                "target": dict(plan.get("target") or {}),
                "values": dict(plan.get("values") or {}),
            },
            "readback": list(readback.get("items") or []),
            "readback_passed": bool(readback.get("passed")),
            "worker_claim": str(worker_text or "")[:500],
            "worker_claim_is_proof": False,
        }
    )
    questions = jev_questions()
    request_body = {"state": state, "model": "jev-latest", "questions": questions}
    try:
        response = client.evaluate(state, questions)
    except JevUnavailable:
        response = None
    except Exception:
        response = None
    hard = "" if readback.get("passed") else str(readback.get("failure") or "EZLynx API readback failed")
    return decide(
        response,
        request_body=request_body,
        hard_failure=hard,
        readback_passed=bool(readback.get("passed")),
    )


def _reply_text(job_id: str, readback: Mapping[str, Any], decision: Any) -> str:
    from . import status_format

    lines = ["EZLynx readback:"]
    items = list(readback.get("items") or [])
    if not items:
        lines.append("- No planned value was checked.")
    for item in items:
        observed = item.get("observed")
        shown = "(not on the record)" if observed is None else str(observed)
        mark = "matches" if item.get("matched") else "does not match"
        lines.append(
            f"- {item.get('field')}: planned {item.get('expected')}; "
            f"API shows {shown}; {mark}."
        )
    if readback.get("failure"):
        lines.append(str(readback["failure"]))
    lines.append(
        f"Jev: {decision.display_verdict}, {int(decision.confidence)}% confidence. "
        f"{decision.reason}"
    )
    ref = status_format.short_job_ref(job_id)
    if ref:
        lines.append(ref)
    return "\n".join(lines).strip() + "\n"


def write_reply_if_planned(
    store: Any,
    job: dict[str, Any],
    worker_text: str = "",
    *,
    fetch_fn: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    client: Any = None,
) -> str:
    """Read the plan back and score it. Empty when this job has no locked plan.

    The returned text is the readback and Jev's score. It does not repeat
    the worker's claim.
    """
    job_id = str(job.get("id") or "")
    plan = get_locked_plan(store, job_id) if job_id else None
    if plan is None:
        return ""
    plan = dict(plan)
    target = dict(plan.get("target") or {})
    if (
        not str(target.get("policy_number") or "").strip()
        and not str(target.get("discussion_id") or "").strip()
        and job_id
    ):
        note = store.get_checkpoint(job_id, "discussion_note") or {}
        discussion_id = str(note.get("discussion_id") or "").strip()
        if discussion_id:
            target["discussion_id"] = discussion_id
            plan["target"] = target
    fetcher = fetch_fn or default_ezlynx_observed
    try:
        observed = dict(fetcher(plan) or {})
        error = ""
    except Exception as exc:
        observed = {}
        error = f"EZLynx API readback failed: {type(exc).__name__}"
    readback = compare_plan_to_api(plan, observed, error=error)
    store.checkpoint(job_id, READBACK_CHECKPOINT, readback)
    _store_readback_evidence(store, job_id, plan, readback)
    from .jev_client import build_jev_client

    scorer = client if client is not None else build_jev_client()
    payload = dict(job.get("payload") or {})
    ask = _job_text(job) or str(payload.get("text") or "")
    decision = _score_with_jev(
        ask=ask,
        plan=plan,
        readback=readback,
        worker_text=worker_text,
        client=scorer,
    )
    store.checkpoint(
        job_id,
        JEV_CHECKPOINT,
        {
            "verdict": decision.verdict,
            "confidence": int(decision.confidence),
            "reason": decision.reason,
            "readback_passed": bool(readback.get("passed")),
        },
    )
    return _reply_text(job_id, readback, decision)


def _store_readback_evidence(store: Any, job_id: str, plan: Mapping[str, Any], readback: Mapping[str, Any]) -> None:
    try:
        from .models import VerificationEvidence

        store.add_evidence(
            job_id,
            bool(readback.get("passed")),
            VerificationEvidence(
                method="EZLYNX_API",
                source="EZLynx API readback",
                expected={"values": dict(plan.get("values") or {})},
                observed={
                    "items": list(readback.get("items") or []),
                    "failure": readback.get("failure") or "",
                },
                authoritative=True,
                captured_at=datetime.now(timezone.utc).isoformat(),
                locator=str((plan.get("target") or {}).get("policy_number") or ""),
            ),
        )
    except Exception:
        return


def score_confirmed_discussion_note(store: Any, job_id: str, note: Mapping[str, Any]) -> None:
    """Record the count read-back and ask Jev. A missing key does not unfile the note."""

    if not job_id or not note.get("read_back"):
        return
    readback = {
        "method": "EZLYNX_API",
        "source": "EZLynx API readback",
        "verified": True,
        "passed": True,
        "items": [
            {
                "field": "note_text",
                "expected": note.get("note_text"),
                "observed": note.get("note_text"),
                "matched": True,
            }
        ],
        "failure": "",
    }
    store.checkpoint(
        job_id,
        "discussion_note_readback",
        {
            "matched": True,
            "note_id": note.get("note_id"),
            "verified_by": note.get("verified_by"),
        },
    )
    store.checkpoint(job_id, READBACK_CHECKPOINT, readback)
    try:
        job = store.get_job(job_id)
    except Exception:
        return
    plan = get_locked_plan(store, job_id) or {
        "write": "discussion note",
        "target": {"discussion_id": note.get("discussion_id")},
        "values": {"note_text": note.get("note_text")},
    }
    from .jev_client import build_jev_client

    decision = _score_with_jev(
        ask=_job_text(job),
        plan=plan,
        readback=readback,
        worker_text=str(note.get("note_text") or ""),
        client=build_jev_client(),
    )
    store.checkpoint(
        job_id,
        JEV_CHECKPOINT,
        {
            "verdict": getattr(decision, "verdict", ""),
            "confidence": int(getattr(decision, "confidence", 0) or 0),
            "reason": getattr(decision, "reason", ""),
            "readback_passed": True,
        },
    )


def write_landed(store: Any, job: Mapping[str, Any]) -> bool:
    """True when a note, a document, or a passed readback is on the job."""
    job_id = str(job.get("id") or "")
    if not job_id:
        return False
    note = store.get_checkpoint(job_id, "discussion_note") or {}
    status = str(note.get("status") or "")
    if status == "filed" and (note.get("read_back") or note.get("note_id")):
        return True
    for kind in ("document_upload", "uploaded_document", "ezlynx_document"):
        document = store.get_checkpoint(job_id, kind) or {}
        if document.get("document_id") or document.get("read_back"):
            return True
    readback = store.get_checkpoint(job_id, READBACK_CHECKPOINT) or {}
    return bool(readback.get("passed"))


def nothing_written_line(reason: str) -> str:
    """One line when a write job changed nothing."""
    clean = " ".join(str(reason or "").split()).strip().rstrip(".")
    if not clean:
        clean = "the write did not land"
    return f"Nothing was changed or noted. {clean}."


def unwritten_write_reason(store: Any, job: Mapping[str, Any]) -> str:
    """Why the write did not land, for the one-line user reply."""
    job_id = str(job.get("id") or "")
    refusal = store.get_checkpoint(job_id, REFUSAL_CHECKPOINT) or {} if job_id else {}
    if int(refusal.get("count") or 0) >= MAX_SAME_REFUSAL:
        field = str(refusal.get("field") or "plan")
        return f"The plan field {field} is wrong"
    readback = store.get_checkpoint(job_id, READBACK_CHECKPOINT) or {} if job_id else {}
    failure = str(readback.get("failure") or "").strip()
    if failure:
        return failure
    if job_id and not plan_is_locked(store, job_id):
        return "the plan was not locked"
    return "the write did not land"


def refuse_tool_write(args: Mapping[str, Any] | None, kwargs: Mapping[str, Any] | None) -> str | None:
    """Block an EZLynx write until the model has locked a plan.

    A missing job, or a job that is already finished, is refused before
    any EZLynx call. A wrong plan is returned to the model. It does not
    end the job.
    """
    import os as _os

    kwargs = dict(kwargs or {})
    args = dict(args or {})
    from .live_turn_guard import acting_job_id

    job_id = acting_job_id(dict(kwargs))
    db_path = str(kwargs.get("db_path") or _os.environ.get("ROBIE_JOB_DB") or "").strip()
    if not job_id or not db_path:
        return NO_ACTIVE_JOB_WRITE
    from .store import JobStore

    try:
        store = JobStore(db_path)
        job = store.get_job(job_id)
    except Exception:
        return NO_ACTIVE_JOB_WRITE
    from .models import JobStatus

    status = str((job or {}).get("status") or "")
    if status in {
        JobStatus.COMPLETE.value,
        JobStatus.UNVERIFIED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    }:
        from .chat_turn_control import request_agent_stop

        request_agent_stop(job_id)
        return TERMINAL_JOB_WRITE
    from .chat_job_controls import hard_block_reply

    blocked = hard_block_reply(_job_text(job))
    if blocked:
        return blocked
    if not is_ezlynx_write_job(job):
        return None
    from .live_turn_guard import person_named_in_job, refuse_untrusted_applicant

    if person_named_in_job(job):
        from .client_name_lookup import prepare_named_write_client

        held = prepare_named_write_client(store, job_id)
        if held:
            return held
        job = store.get_job(job_id)
    if plan_is_locked(store, job_id):
        untrusted = refuse_untrusted_applicant(
            store, job, str(args.get("applicant_id") or "")
        )
        if untrusted:
            return untrusted
        return None
    statement = coerce_tool_plan(args.get("plan"), args)
    problem = plan_field_problem(statement)
    if problem is None:
        parsed = parse_model_plan(statement if isinstance(statement, (str, Mapping)) else None)
        if parsed is not None:
            lock_stated_plan(store, job, parsed)
            return None
        problem = "plan"
    prior = store.get_checkpoint(job_id, REFUSAL_CHECKPOINT) or {}
    counts = dict(prior.get("counts") or {})
    count = int(counts.get(problem) or 0) + 1
    counts[problem] = count
    store.checkpoint(
        job_id,
        REFUSAL_CHECKPOINT,
        {"count": count, "field": problem, "counts": counts},
    )
    if count >= MAX_SAME_REFUSAL:
        return plan_stop_refusal(problem)
    from .live_turn_guard import name_placed_in_applicant_id, person_named_in_job

    named = person_named_in_job(job) or name_placed_in_applicant_id(
        args.get("applicant_id")
    )
    if named and problem in {"target", "plan"}:
        return (
            "The plan field target is wrong. "
            f"The request names {named}. Search EZLynx for that name. "
            "Do not ask the user for an applicant id. "
            "Do not put the name in applicant_id. The write was not sent."
        )
    return plan_refusal(problem)
