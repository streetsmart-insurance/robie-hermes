"""Plain-English end-state report for one Robie job, scored by Jev.

Turned on with ``ROBIE_END_STATE_REPORT=1``. Off by default. When the
flag is set, it turns on in that process, including Production.

When the flag is on, Chat and email replies use this report instead of
the "Not verified" / "Worker report (not proof)" wording. Deterministic
readbacks still run and are sent to Jev as evidence. A failed hard
readback (file missing in EZLynx, sent message missing, note missing)
forces verdict ``wrong``. Jev cannot override that. Approval gates for
money, bind, and dangerous commands are not touched here.

There is no second model pass over the worker report. Jev is the only
score. If Jev cannot be reached, the verdict is ``unsure (Jev
unavailable)`` and the job escalates. That is never a silent pass.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from .jev_client import JevUnavailable, build_jev_client
from .secrets import redact_mapping, redact_text
from . import status_format

logger = logging.getLogger("robie.end_state_report")

FLAG = "ROBIE_END_STATE_REPORT"
THRESHOLD_ENV = "ROBIE_END_STATE_CONFIDENCE_THRESHOLD"
_ON = {"1", "true", "yes", "on"}

_HARD_MARKERS = (
    "EZLYNX_API",
    "DOCUMENT",
    "DISCUSSION",
    "APPLICANT",
    "GMAIL_SENT",
    "SENT_READBACK",
    "MESSAGE_ID",
    "DEDUP",
    "DE_DUP",
    "ACTIVIT",
)
_LOGIN_PAGE = re.compile(
    r"(/login\b|/signin\b)"
    r"|(\blogin page\b|\bsign[- ]in page\b|\bsign[- ]in screen\b)"
    r"|(\bstopped at\b.{0,60}\blogin\b)"
    r"|(\bstuck on\b.{0,60}\blogin\b)",
    re.IGNORECASE,
)
_MFA = re.compile(
    r"\b(2fa|two[- ]factor|mfa|one[- ]time code|verification code)\b",
    re.IGNORECASE,
)
_JARGON = (
    (re.compile(r"\bplaywright_exec\b", re.IGNORECASE), "the browser"),
    (re.compile(r"\bplaywright\b", re.IGNORECASE), "the browser"),
    (re.compile(r"\bCDP\b"), "the browser connection"),
    (re.compile(r"\blocators?\b", re.IGNORECASE), "controls"),
    (re.compile(r"\bpage\.goto\([^)]*\)", re.IGNORECASE), "opened the page"),
)
_STUCK = "made no verified destination progress"


@dataclass(frozen=True)
class EndStateDecision:
    verdict: str
    confidence: int
    reason: str
    display_verdict: str
    disagree: bool
    jev_unavailable: bool
    escalate: bool
    raw_response: dict[str, Any]
    request_body: dict[str, Any]


def end_state_report_enabled() -> bool:
    """True only when ``ROBIE_END_STATE_REPORT`` is explicitly on.

    Default is off, in Test and in Production. Setting the flag turns
    the report on in whichever process has it, including Production.
    """
    return os.environ.get(FLAG, "").strip().lower() in _ON


def confidence_threshold() -> int:
    raw = os.environ.get(THRESHOLD_ENV, "70").strip() or "70"
    try:
        value = int(raw)
    except ValueError:
        return 70
    return max(0, min(100, value))


def jev_questions() -> dict[str, Any]:
    """Typed questions: did the end state satisfy the ask, and which outcome.
    
    2026-10-02: Updated to recognize legitimate clarification requests.
    When the worker asks the user for missing/ambiguous info (e.g. which of
    two carriers, missing expiration date), that is CORRECT behavior per
    Carlo's rule: "Anytime something is going to fail, I want you to ask
    the user for that information." Do NOT score clarification as "wrong".
    """
    return {
        "satisfied": {
            "type": "noul",
            "instructions": (
                "Did the observed end state satisfy the original ask? "
                "Use the ask, the end state, the readback results, and the "
                "worker claim in the state. The worker claim is not proof. "
                "IMPORTANT: If the worker asked the user for clarification "
                "because the input was ambiguous or missing required info "
                "(e.g. two carriers with the same name, missing expiration "
                "date), that is CORRECT behavior — score as true. Asking "
                "is better than guessing wrong."
            ),
            "criteria": {
                "true": "The end state shows the requested work is done, OR the worker correctly asked for clarification on ambiguous/missing input.",
                "false": "The end state does not show the requested work is done, and the worker did not ask for needed clarification.",
            },
        },
        "outcome": {
            "type": "choice",
            "instructions": "Which outcome best describes what actually happened at the end?",
            "criteria": {
                "completed": "The requested work finished and the end state matches the ask.",
                "partially_completed": "Some of the requested work is done, but not all of it.",
                "blocked": "The work stopped on something in the way, such as a login page or a missing approval.",
                "failed": "The work did not succeed.",
                "needs_clarification": "The worker correctly asked the user for missing or ambiguous information instead of guessing.",
            },
        },
    }


def answer_quality_questions() -> dict[str, Any]:
    """Jev scores the answer text only. No destination, no policy readback."""
    return {
        "satisfied": {
            "type": "noul",
            "instructions": (
                "Does the answer text reply to the question that was asked? "
                "Judge the answer only. Ignore EZLynx, policy numbers, and "
                "destination readback. This question has no EZLynx destination."
            ),
            "criteria": {
                "true": "The answer replies to the question.",
                "false": "The answer does not reply to the question.",
            },
        },
        "outcome": {
            "type": "choice",
            "instructions": "How complete is the answer text?",
            "criteria": {
                "completed": "The answer replies to the question.",
                "partially_completed": "The answer is only part of a reply.",
                "blocked": "The answer says it could not reply.",
                "failed": "There is no answer.",
            },
        },
    }


def render_answer_only(
    store: Any,
    job: dict[str, Any],
    worker_text: str,
    *,
    channel: str = "chat",
    client: Any = None,
) -> str:
    """Plain reply: the answer only. Audit detail stays on the job checkpoint."""
    from .answer_only import strip_answer_verifier_noise
    from .email_guard import _strip_internal_reasoning
    from .user_reply import format_user_reply

    job_id = str(job.get("id") or "")
    cached = store.get_checkpoint(job_id, "end_state_report") if job_id else None
    if cached and cached.get("answer_only"):
        shown = str(cached.get("user_text") or "").strip()
        if shown:
            return shown if shown.endswith("\n") else shown + "\n"
        if str(cached.get("text") or "").strip():
            cleaned = format_user_reply(str(cached["text"]))
            return cleaned if cleaned.endswith("\n") else cleaned + "\n"
    payload = dict(job.get("payload") or {})
    ask = _ask_text(payload) or _fragment(str(worker_text or ""))
    answer = _strip_internal_reasoning(str(worker_text or ""))
    answer = strip_answer_verifier_noise(str(answer or ""))
    from .answer_only import strip_blank_saved_span

    answer = strip_blank_saved_span(str(answer or ""))
    if end_state_report_enabled():
        scorer = client if client is not None else build_jev_client()
        state = redact_mapping({"ask": ask, "answer": answer[:4000]})
        questions = answer_quality_questions()
        request_body = {"state": state, "model": "jev-latest", "questions": questions}
        try:
            response = scorer.evaluate(state, questions)
        except Exception:
            logger.warning("Jev answer scoring failed closed")
            response = None
        decision = decide(
            response,
            request_body=request_body,
            hard_failure="",
            login_block="",
            readback_passed=False,
        )
        jev_line = (
            f"Jev: {decision.display_verdict}, {int(decision.confidence)}% confidence. "
            f"{plain_customer_text(decision.reason)}"
        )
    else:
        decision = None
        jev_line = "Jev was not asked. The end-state report is off."
    summary = answer or ""
    from .live_turn_guard import format_complete_answer

    user_text = format_complete_answer(summary) or summary
    if not user_text.endswith("\n"):
        user_text += "\n"
    audit = "\n".join(
        (
            summary.strip(),
            "",
            "Details",
            jev_line,
            "No EZLynx destination check. This was a question.",
            "",
            status_format.short_job_ref(job_id),
        )
    ).strip()
    if job_id:
        store.checkpoint(
            job_id,
            "end_state_report",
            {
                "text": audit,
                "user_text": user_text,
                "answer_only": True,
                "verdict": getattr(decision, "verdict", ""),
                "channel": channel,
            },
        )
        logger.info("answer-only audit kept in the ledger job=%s", job_id)
    return user_text


def render_job_end_state(
    store: Any,
    job: dict[str, Any],
    worker_text: str = "",
    *,
    recordings: Any = None,
    channel: str = "chat",
    client: Any = None,
) -> str:
    """Score one terminal job and return the end-state reply.

    Questions use the answer-only path: Jev judges the answer, and EZLynx
    is not re-read.
    """
    from .answer_only import is_answer_only_job

    if is_answer_only_job(job):
        return render_answer_only(
            store, job, worker_text, channel=channel, client=client
        )

    job_id = str(job.get("id") or "")
    cached = store.get_checkpoint(job_id, "end_state_report") if job_id else None
    if cached and str(cached.get("text") or "").strip():
        return str(cached["text"])

    context = collect_context(store, job, worker_text, recordings)
    scorer = client if client is not None else build_jev_client()
    decision = score_end_state(context, scorer)
    if decision.escalate and job_id:
        escalate_end_state(store, job, decision, channel=channel)
    text = compose_report(job_id, context, decision)
    if job_id:
        _persist(store, job_id, decision, text)
    logger.info(
        "Jev score job_id=%s verdict=%s confidence=%s escalate=%s",
        job_id,
        decision.verdict,
        decision.confidence,
        decision.escalate,
    )
    return text


def collect_context(
    store: Any,
    job: dict[str, Any],
    worker_text: str,
    recordings: Any = None,
) -> dict[str, Any]:
    payload = dict(job.get("payload") or {})
    ask = _ask_text(payload)
    claim = _worker_claim(store, job, worker_text)
    evidence = []
    try:
        evidence = list(store.list_evidence(job.get("id")))
    except Exception:
        evidence = []
    pages = _page_observations(store, job.get("id"))
    screenshots = list(pages["screenshots"])
    urls = list(pages["urls"])
    if recordings is not None:
        try:
            recording = recordings.store.latest(job.get("id"))
        except Exception:
            recording = None
        if recording:
            if recording.get("drive_url"):
                urls.append(str(recording["drive_url"]))
            if recording.get("local_path"):
                screenshots.append(str(recording["local_path"]))
    hard_failure = _first_hard_failure(evidence)
    login = observed_login_block("\n".join([claim, pages["page_text"], *urls]))
    end_state = _choose_end_state(ask, evidence, claim, hard_failure, login)
    state = redact_mapping(
        {
            "ask": ask,
            "worker_claim": claim[:2000],
            "end_state": end_state,
            "page_text": pages["page_text"][:4000],
            "readbacks": [_public_readback(row) for row in evidence],
            "urls": urls[:8],
        }
    )
    return {
        "ask": ask,
        "display_ask": _display_ask(payload),
        "claim": claim,
        "end_state": end_state,
        "evidence": evidence,
        "hard_failure": hard_failure,
        "login_block": login,
        "urls": urls,
        "screenshots": screenshots,
        "state": state,
    }


def score_end_state(context: dict[str, Any], client: Any) -> EndStateDecision:
    questions = jev_questions()
    request_body = {
        "state": context["state"],
        "model": "jev-latest",
        "questions": questions,
    }
    try:
        response = client.evaluate(context["state"], questions)
    except JevUnavailable:
        response = None
    except Exception:
        logger.warning("Jev scoring failed closed")
        response = None
    return decide(
        response,
        request_body=request_body,
        hard_failure=context.get("hard_failure") or "",
        login_block=context.get("login_block") or "",
        readback_passed=_hard_readback_passed(context.get("evidence") or []),
    )


def decide(
    response: dict[str, Any] | None,
    *,
    request_body: dict[str, Any],
    hard_failure: str = "",
    login_block: str = "",
    readback_passed: bool = False,
) -> EndStateDecision:
    """Combine Jev's answers with deterministic checks. Checks win."""
    threshold = confidence_threshold()
    if response is None:
        decision = _unavailable(request_body)
    else:
        decision = _from_jev(response, request_body)

    jev_yes = _response_says_yes(response)
    disagree = False
    if hard_failure:
        disagree = bool(jev_yes)
        decision = EndStateDecision(
            verdict="wrong",
            confidence=100,
            reason=(
                f"A required check failed: {hard_failure} "
                "Jev does not override that."
            ),
            display_verdict="wrong",
            disagree=disagree,
            jev_unavailable=False,
            escalate=False,
            raw_response=dict(response or {"error": "Jev unavailable"}),
            request_body=request_body,
        )
    elif login_block and decision.verdict != "wrong":
        disagree = bool(jev_yes) or decision.verdict == "correct"
        if decision.jev_unavailable:
            reason = (
                "Stopped at the login page. Jev could not be reached "
                "and does not override that."
            )
        else:
            reason = "Robie was still on a login page. Jev does not override that."
        decision = EndStateDecision(
            verdict="wrong",
            confidence=100,
            reason=reason,
            display_verdict="wrong",
            disagree=disagree,
            jev_unavailable=False,
            escalate=False,
            raw_response=decision.raw_response,
            request_body=request_body,
        )
    elif (
        readback_passed
        and response is not None
        and not jev_yes
        and decision.verdict == "wrong"
    ):
        disagree = True
        decision = EndStateDecision(
            verdict=decision.verdict,
            confidence=decision.confidence,
            reason=decision.reason,
            display_verdict=decision.display_verdict,
            disagree=True,
            jev_unavailable=decision.jev_unavailable,
            escalate=False,
            raw_response=decision.raw_response,
            request_body=request_body,
        )

    escalate = (
        decision.verdict in {"wrong", "unsure"}
        or decision.confidence < threshold
        or decision.disagree
        or disagree
    )
    if escalate == decision.escalate and disagree == decision.disagree:
        return decision
    return EndStateDecision(
        verdict=decision.verdict,
        confidence=decision.confidence,
        reason=decision.reason,
        display_verdict=decision.display_verdict,
        disagree=decision.disagree or disagree,
        jev_unavailable=decision.jev_unavailable,
        escalate=escalate,
        raw_response=decision.raw_response,
        request_body=decision.request_body,
    )


def _is_clarification_request(end_state: str) -> bool:
    """True when Robie finished by asking the requester for more info.

    This is not a failure -- it is Robie doing its job correctly by
    refusing to guess. The requester just needs a warm nudge, not a QA report.
    """
    blob = str(end_state or "").lower()
    return any(
        marker in blob
        for marker in (
            "need clarification",
            "need the following",
            "missing items",
            "before creating the ascend",
            "could you provide",
            "please provide",
            "i need the following",
            # 2026-10-02: Astra Gold case - carrier not found in Ascend
            "couldn't match",
            "could not match",
            "no results",
            "what is the exact",
            "please reply directly",
            "i haven't created",
            "so i haven't",
        )
    )


def _warm_clarification_email(ask: str, end_state: str) -> str:
    """Rewrite a clarification end-state as a warm human email.

    Keeps every fact from the original (what Robie has, what is missing)
    but sounds like a helpful colleague, not a QA report. No Jev scores,
    no "end state" language, no internal jargon.
    """
    original = str(end_state or "").strip()

    # Pull out the "what I have" lead (e.g. "I received the quote for X from Y")
    have_match = re.search(
        r"(I received the quote for .+?)(?:, but I need clarification.*)",
        original,
        re.IGNORECASE | re.DOTALL,
    )
    have_line = have_match.group(1).strip() + "." if have_match else ""

    # Pull out the numbered missing items (lines starting with digits/bullets)
    missing_lines = []
    for line in original.split("\n"):
        stripped = line.strip()
        # Match "11. Insured Address: ..." or "- Insured Address: ..." etc.
        if re.match(r"^(\d+[.\)]|[-\u2022])\s*\S", stripped):
            # Clean up "11. Insured Address:" -> "Insured Address:"
            cleaned = re.sub(r"^\d+[.\)]\s*", "", stripped)
            missing_lines.append(cleaned)

    parts = ["Hi there,", ""]
    parts.append(
        "Thanks for sending that over -- I'm on it."
    )
    if have_line:
        parts += ["", have_line]
    if missing_lines:
        parts += ["", "Before I can build the financing agreement, I just need a couple of things from you:", ""]
        for item in missing_lines:
            parts.append("- " + item)
        parts += [
            "",
            "Just hit reply with those details and I'll pick it right back up -- "
            "no need to resend anything.",
        ]
    else:
        # Fallback: keep the original ask, just wrapped warmly
        parts += ["", original, "", "Just hit reply and I'll pick it right back up."]
    parts += ["", "Thanks!", "Robie"]
    return "\n".join(parts)


def compose_report(job_id: str, context: dict[str, Any], decision: EndStateDecision) -> str:
    raw_end_state = str(context.get("end_state") or "")
    # Carlo 2026-10-02: when Robie is asking the requester for more info,
    # send a warm human email -- not the internal QA report format.
    if _is_clarification_request(raw_end_state):
        return _warm_clarification_email(
            str(context.get("ask") or ""), raw_end_state
        )
    summary = summary_sentence(
        context.get("display_ask") or context.get("ask") or "",
        context.get("end_state") or "",
        decision.verdict,
    )
    end_state = plain_customer_text(context.get("end_state") or "Robie stopped without a clear ending.")
    jev_line = (
        f"Jev: {decision.display_verdict}, {int(decision.confidence)}% confidence. "
        f"{plain_customer_text(decision.reason)}"
    )
    details = _details_block(context)
    text = status_format.render_end_state_report(
        summary=summary,
        end_state=end_state,
        jev_line=jev_line,
        details=details,
        job_id=job_id,
    )
    return _scrub_old_labels(text)


def escalate_end_state(
    store: Any,
    job: dict[str, Any],
    decision: EndStateDecision,
    *,
    channel: str = "chat",
) -> dict[str, Any]:
    """Enter the existing Gemini-first HITL ladder once, then one Chat ping.

    Gemini rescue itself is unchanged. This only decides to enter that
    ladder. A second render of the same job does not post again.
    """
    job_id = str(job.get("id") or "")
    if not job_id:
        return {"posted": False, "reason": "no job"}
    existing = store.get_checkpoint(job_id, "end_state_hitl") or {}
    if existing.get("attempted"):
        return {"posted": bool(existing.get("posted")), "reason": "already escalated"}
    store.checkpoint(
        job_id,
        "end_state_hitl",
        {"attempted": True, "posted": False, "verdict": decision.verdict},
    )
    posted = False
    error = ""
    try:
        from .hitl_escalation import HitlRequest, escalate

        payload = dict(job.get("payload") or {})
        request = HitlRequest(
            job_id=job_id,
            phase="end_state_report",
            error=decision.reason,
            page_state={"url": "", "title": decision.display_verdict},
            attempted=["end-state report"],
            applicant_id=str(payload.get("applicant_id") or ""),
            original_requester=None,
            notify_carlo=True,
            notify_requester=False,
            channel=str(channel or "chat"),
            script_or_job_stopped=True,
            job_still_running=False,
        )

        def chat_sender(text: str) -> bool:
            from .chat_app_post import post_hitl_to_originating_thread

            return bool(
                post_hitl_to_originating_thread(
                    text,
                    job_id=job_id,
                    store=store,
                    db_path=str(getattr(store, "path", "") or ""),
                )
            )

        deps = {"chat_sender": chat_sender}
        if str(channel or "").strip().casefold() == "email":
            from .hitl_email import carlo_hitl_email_sender

            deps = {"email_sender": carlo_hitl_email_sender()}
            if os.environ.get("ROBIE_CHAT_SA_KEY_FILE", "").strip():
                deps["chat_sender"] = chat_sender
        result = escalate(request, deps=deps)
        posted = bool(getattr(result, "hitl_posted", False))
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("end-state HITL failed job_id=%s error=%s", job_id, error)
    store.checkpoint(
        job_id,
        "end_state_hitl",
        {
            "attempted": True,
            "posted": posted,
            "verdict": decision.verdict,
            "confidence": decision.confidence,
            "channel": channel,
            "error": error,
        },
    )
    return {"posted": posted, "reason": error}


def summary_sentence(ask: str, end_state: str, verdict: str) -> str:
    fragment = _fragment(ask) or "do the requested work"
    ending = str(end_state or "")
    if verdict == "correct" and ending.lower().startswith("quote created"):
        return f"You asked Robie to {fragment}, and Robie created the quote in EZLynx."
    if verdict == "correct":
        return f"You asked Robie to {fragment}, and Robie finished that work."
    if verdict == "wrong":
        return f"You asked Robie to {fragment}, and Robie did not finish it."
    return f"You asked Robie to {fragment}, and Robie could not confirm how it ended."


def plain_customer_text(text: str) -> str:
    cleaned = status_format.plain_reason(text)
    for pattern, replacement in _JARGON:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def observed_login_block(text: str) -> str:
    blob = str(text or "")
    if not _LOGIN_PAGE.search(blob):
        return ""
    if _MFA.search(blob):
        return "Stopped at the login page, 2FA prompt."
    return "Stopped at the login page."


def is_hard_readback(row: dict[str, Any]) -> bool:
    blob = f"{row.get('method') or ''} {row.get('source') or ''}".upper()
    return any(marker in blob for marker in _HARD_MARKERS)


def concrete_readback_failure(row: dict[str, Any]) -> str:
    if not is_hard_readback(row):
        return ""
    observed = dict(row.get("observed") or {})
    expected = dict(row.get("expected") or {})
    missing = observed.get("documents_missing") or []
    if isinstance(missing, str):
        missing = [missing]
    missing = [str(item).strip() for item in missing if str(item).strip()]
    if missing:
        names = ", ".join(missing)
        return f"{names} was not in EZLynx."
    if expected.get("policy_number") and observed.get("policy_found") is False:
        return "The policy was not found in EZLynx."
    if expected.get("discussion_title") and observed.get("discussion_receipt_present") is False:
        return "The note was not found in EZLynx."
    if observed.get("sent_found") is False or observed.get("message_found") is False:
        return "The message was not in the Sent folder."
    if observed.get("duplicate") is True:
        return "A duplicate was already in EZLynx."
    error = str(observed.get("error") or observed.get("documents_error") or "").strip()
    if error:
        return plain_customer_text(error)
    if not row.get("verified") and not _positive_observation(observed):
        return "The destination check did not pass."
    return ""


def _unavailable(request_body: dict[str, Any]) -> EndStateDecision:
    return EndStateDecision(
        verdict="unsure",
        confidence=0,
        reason="Jev could not be reached, so this is not a pass.",
        display_verdict="unsure (Jev unavailable)",
        disagree=False,
        jev_unavailable=True,
        escalate=True,
        raw_response={"error": "Jev unavailable"},
        request_body=request_body,
    )


def _from_jev(response: dict[str, Any], request_body: dict[str, Any]) -> EndStateDecision:
    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        decision = _unavailable(request_body)
        return EndStateDecision(
            verdict="unsure",
            confidence=0,
            reason="Jev did not return a usable score, so this is not a pass.",
            display_verdict="unsure",
            disagree=False,
            jev_unavailable=True,
            escalate=True,
            raw_response=dict(response),
            request_body=request_body,
        )
    satisfied = answers.get("satisfied") if isinstance(answers.get("satisfied"), dict) else {}
    outcome = answers.get("outcome") if isinstance(answers.get("outcome"), dict) else {}
    noul = _float_or_none(satisfied.get("noul"))
    choice = str(outcome.get("choice") or "").strip().lower()
    choice_confidence = _float_or_none(outcome.get("confidence"))
    if noul is None or choice not in {
        "completed",
        "partially_completed",
        "blocked",
        "failed",
        "needs_clarification",
    }:
        return EndStateDecision(
            verdict="unsure",
            confidence=0,
            reason="Jev did not return a usable score, so this is not a pass.",
            display_verdict="unsure",
            disagree=False,
            jev_unavailable=True,
            escalate=True,
            raw_response=dict(response),
            request_body=request_body,
        )
    yes = noul >= 0.5
    yes_no_confidence = noul if yes else (1.0 - noul)
    parts = [yes_no_confidence]
    if choice_confidence is not None:
        parts.append(choice_confidence)
    confidence = _percent(min(parts))
    if choice == "needs_clarification" and yes:
        verdict = "correct"
        reason = "The worker asked for clarification instead of guessing."
    elif choice == "completed" and yes:
        verdict = "correct"
        reason = "The end state matches what was asked."
    elif choice == "partially_completed":
        verdict = "unsure"
        reason = "Only part of the requested work showed up at the end."
    elif choice in {"failed", "blocked"} or not yes:
        verdict = "wrong"
        reason = "The end state does not match what was asked."
    else:
        verdict = "unsure"
        reason = "Jev could not settle the outcome."
    return EndStateDecision(
        verdict=verdict,
        confidence=confidence,
        reason=reason,
        display_verdict=verdict,
        disagree=False,
        jev_unavailable=False,
        escalate=False,
        raw_response=dict(response),
        request_body=request_body,
    )


def _response_says_yes(response: dict[str, Any] | None) -> bool:
    if not isinstance(response, dict):
        return False
    answers = response.get("answers")
    if not isinstance(answers, dict):
        return False
    satisfied = answers.get("satisfied") if isinstance(answers.get("satisfied"), dict) else {}
    outcome = answers.get("outcome") if isinstance(answers.get("outcome"), dict) else {}
    noul = _float_or_none(satisfied.get("noul"))
    choice = str(outcome.get("choice") or "").strip().lower()
    if choice == "completed":
        return True
    return noul is not None and noul >= 0.5


def _persist(store: Any, job_id: str, decision: EndStateDecision, text: str) -> None:
    response = redact_mapping(dict(decision.raw_response or {}))
    request = redact_mapping(dict(decision.request_body or {}))
    try:
        row_id = store.add_jev_evaluation(
            job_id,
            verdict=decision.display_verdict,
            confidence=int(decision.confidence),
            reason=decision.reason,
            escalate=bool(decision.escalate),
            request=request,
            response=response,
        )
    except Exception:
        logger.warning("could not store Jev score job_id=%s", job_id)
        row_id = 0
    try:
        store.checkpoint(
            job_id,
            "jev_score",
            {
                "evaluation_id": row_id,
                "verdict": decision.display_verdict,
                "confidence": int(decision.confidence),
                "reason": decision.reason,
                "escalate": bool(decision.escalate),
                "disagree": bool(decision.disagree),
                "response": response,
            },
        )
        store.checkpoint(job_id, "end_state_report", {"text": text})
    except Exception:
        logger.warning("could not checkpoint Jev score job_id=%s", job_id)


def _choose_end_state(
    ask: str,
    evidence: list[dict[str, Any]],
    claim: str,
    hard_failure: str,
    login: str,
) -> str:
    if hard_failure and "was not in EZLynx" in hard_failure:
        return f"Stopped before the file was in EZLynx. {hard_failure}"
    if hard_failure:
        return hard_failure
    structured = _structured_success(ask, evidence)
    if structured:
        return structured
    if login:
        return login
    cleaned = plain_customer_text(claim)
    if cleaned and _STUCK not in cleaned.casefold() and "UNVERIFIED" not in cleaned:
        sentence = cleaned if cleaned.endswith(".") else cleaned + "."
        return sentence[:400]
    return "Robie stopped without a clear ending."


def _structured_success(ask: str, evidence: list[dict[str, Any]]) -> str:
    quote = "quote" in str(ask or "").casefold()
    for row in reversed(evidence):
        if concrete_readback_failure(row):
            continue
        observed = dict(row.get("observed") or {})
        name = str(observed.get("account_name") or observed.get("applicant_name") or "").strip()
        applicant = str(
            observed.get("applicant_id")
            or (row.get("expected") or {}).get("applicant_id")
            or ""
        ).strip()
        premium = str(observed.get("premium") or "").strip()
        carrier = str(observed.get("carrier") or "").strip()
        if not name or not (premium or applicant):
            continue
        if not (quote or observed.get("quote_created")):
            continue
        bits = [f"Quote created in EZLynx for {name}"]
        if applicant:
            bits.append(f"applicant id {applicant}")
        if premium:
            bits.append(f"premium {premium}")
        if carrier:
            bits.append(f"carrier {carrier}")
        return ", ".join(bits) + "."
    return ""


def _details_block(context: dict[str, Any]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for row in context.get("evidence") or []:
        line = _readback_line(row)
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    for url in context.get("urls") or []:
        line = f"URL: {url}"
        if line not in seen:
            seen.add(line)
            lines.append(line)
    for path in context.get("screenshots") or []:
        line = f"Screenshot: {path}"
        if line not in seen:
            seen.add(line)
            lines.append(line)
    if not lines:
        lines.append("No destination readback was stored for this job.")
    return "\n".join(lines)


def _readback_line(row: dict[str, Any]) -> str:
    label = _readback_label(str(row.get("method") or ""), str(row.get("source") or ""))
    failure = concrete_readback_failure(row)
    if failure:
        return f"Readback: {label} — failed. {failure}"
    if row.get("verified"):
        return f"Readback: {label} — passed."
    return f"Readback: {label} — recorded, not a pass."


def _readback_label(method: str, source: str) -> str:
    blob = f"{method} {source}".upper()
    if "GMAIL_SENT" in blob or "MESSAGE_ID" in blob or "SENT_READBACK" in blob:
        return "Sent-folder / message check"
    if "DEDUP" in blob or "DE_DUP" in blob or "ACTIVIT" in blob:
        return "Documents/activities duplicate check"
    if any(marker in blob for marker in ("EZLYNX", "DOCUMENT", "DISCUSSION", "APPLICANT")):
        return "EZLynx documents/notes/applicant API"
    return method or "Destination check"


def _public_readback(row: dict[str, Any]) -> dict[str, Any]:
    failure = concrete_readback_failure(row)
    return redact_mapping(
        {
            "method": str(row.get("method") or ""),
            "passed": bool(row.get("verified")) and not failure,
            "failure": failure,
            "observed": dict(row.get("observed") or {}),
        }
    )


def _first_hard_failure(evidence: list[dict[str, Any]]) -> str:
    for row in evidence:
        failure = concrete_readback_failure(row)
        if failure:
            return failure
    return ""


def _hard_readback_passed(evidence: list[dict[str, Any]]) -> bool:
    for row in evidence:
        if is_hard_readback(row) and row.get("verified") and not concrete_readback_failure(row):
            return True
    return False


def _positive_observation(observed: dict[str, Any]) -> bool:
    if observed.get("policy_found") is True:
        return True
    if observed.get("discussion_receipt_present") is True:
        return True
    if observed.get("sent_found") is True or observed.get("message_id"):
        return True
    for key in ("documents_found", "documents_present", "found_documents"):
        if observed.get(key):
            return True
    return False


def _page_observations(store: Any, job_id: str) -> dict[str, Any]:
    urls: list[str] = []
    screenshots: list[str] = []
    bits: list[str] = []
    try:
        rows = store.list_playwright_exec(job_id)
    except Exception:
        rows = []
    for row in rows or []:
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        for key in ("url", "page_url", "final_url"):
            if result.get(key):
                urls.append(str(result[key]))
        for key in ("screenshot", "screenshot_path"):
            if result.get(key):
                screenshots.append(str(result[key]))
        title = str(result.get("title") or "").strip()
        text = str(result.get("text") or result.get("page_text") or "").strip()
        if title:
            bits.append(title)
        if text:
            bits.append(text[:1500])
    return {
        "urls": urls,
        "screenshots": screenshots,
        "page_text": plain_customer_text("\n".join(bits))[:4000],
    }


def _ask_text(payload: dict[str, Any]) -> str:
    for key in ("request_text", "text", "message", "task", "prompt"):
        value = str(payload.get(key) or "").strip()
        if value:
            return _fragment(value)
    return ""


def _display_ask(payload: dict[str, Any]) -> str:
    """Short ask for the summary line only. Jev still sees the full ask.

    An email job's request_text is "Subject: <subject>\n\n<body>", which
    rendered as "You asked Robie to Subject: Re: ... Best Regards, ...".
    Use the subject without Re:/Fwd: instead.
    """
    raw = str(payload.get("request_text") or "").strip()
    match = re.match(r"(?is)^subject:\s*([^\n]*)", raw)
    if not match:
        return ""
    subject = match.group(1).strip()
    while True:
        trimmed = re.sub(r"^(?:re|fwd?|fw)\s*:\s*", "", subject, flags=re.IGNORECASE)
        if trimmed == subject:
            break
        subject = trimmed
    return _fragment(f"handle {subject}") if subject else ""


def _worker_claim(store: Any, job: dict[str, Any], worker_text: str) -> str:
    text = str(worker_text or "").strip()
    if text:
        return text
    try:
        action = store.get_checkpoint(job.get("id"), "action") or {}
    except Exception:
        action = {}
    detail = action.get("detail") if isinstance(action, dict) else {}
    if isinstance(detail, dict) and detail.get("response_text"):
        return str(detail["response_text"])
    return str(job.get("last_error") or "")


def _fragment(text: str) -> str:
    cleaned = plain_customer_text(text)
    cleaned = re.sub(r"^(please|can you|could you)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip().strip(".")
    if len(cleaned) > 180:
        cleaned = cleaned[:177].rstrip() + "..."
    return cleaned


def _scrub_old_labels(text: str) -> str:
    text = text.replace("Worker report (not proof):", "")
    text = text.replace("UNVERIFIED", "not confirmed")
    return text


def _percent(value: float) -> int:
    return max(0, min(100, int(round(float(value) * 100))))


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
