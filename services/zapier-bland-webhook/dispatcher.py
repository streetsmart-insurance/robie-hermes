"""Dispatch routing: campaign_id -> flow.

Two flows (Carlo, 2026-10-02; note_body primary 2026-10-03):
1. "robie-call" (freeform): the Zapier "New Note" trigger sees the full
   note text — which the EZLynx APIs never return — so the Zap POSTs it as
   `note_body`, and that IS Eva's call instruction. The dispatcher fetches
   the applicant (name, phones) and policies from EZLynx, and builds a
   Bland call where Eva has FULL context and "runs with it" even when the
   instruction is vague. Fallback: the discussion TITLE via the v8 API
   (used only when no note_body arrived). Label detection stays in the
   Zapier Zaps (the only place labels are visible).
2. Deterministic (10 campaigns): look up the applicant's phone, use the
   campaign's approved canned script. A campaign with no approved script is
   a HARD STOP — never dial without one.

Every dispatch runs Jake's double-dial voicemail policy and writes back to
EZLynx (note + MP3) per Carlo's standing rule.
"""
import logging
from typing import Any, Dict, List, Optional

from config import Config
from bland_client import BlandClient
from ezlynx_client import EZLynxClient
from alerts import kill_switch_active
from prompts import (
    build_deterministic_task,
    build_freeform_task,
    deterministic_campaigns,
    first_sentence,
    load_scripts,
    voicemail_message,
)
from writeback import format_call_note, pick_writeback_discussion

logger = logging.getLogger("bland_dispatcher.dispatcher")

FREEFORM_CAMPAIGN = "robie-call"

# Aliases for campaign_ids as sent by the Zapier Zaps (which use extra hyphens).
# Maps Zap variant -> canonical scripts_config.json key.
CAMPAIGN_ALIASES = {
    "robie-lead-follow-up": "robie-lead-followup",
    "robie-e-sign": "robie-esign",
    "robie-renewal-reach-out": "robie-renewal-reachout",
}

# Titles that carry no instruction (user left the default or blank).
GENERIC_TITLES = {"", "untitled", "new discussion", "discussion"}


def canonical_campaign_id(campaign_id: str) -> str:
    """Normalize a campaign_id via aliases to its canonical form."""
    cid = (campaign_id or "").strip().lower()
    return CAMPAIGN_ALIASES.get(cid, cid)


def route_campaign(campaign_id: str) -> str:
    """Map a campaign_id to its flow: 'freeform' | 'deterministic' | 'unknown'."""
    cid = canonical_campaign_id(campaign_id)
    if cid == FREEFORM_CAMPAIGN:
        return "freeform"
    if cid in deterministic_campaigns():
        return "deterministic"
    return "unknown"


def is_generic_title(title: str) -> bool:
    """True when the discussion title carries no usable instruction."""
    return (title or "").strip().lower() in GENERIC_TITLES


# Error fragments meaning "this specific number is bad" (try the next one)
# vs "the calling service is broken" (stop, don't burn other numbers).
_BAD_NUMBER_HINTS = (
    "disconnected", "invalid", "not in service", "no longer in service",
    "unreachable", "bad number", "invalid phone", "number not valid",
)


def _is_bad_number_failure(call_summary: Dict[str, Any]) -> bool:
    """True when the failure is about THIS number, not the service."""
    for a in call_summary.get("attempts") or []:
        if a.get("success"):
            return False
        err = str(a.get("error") or "").lower()
        if any(h in err for h in _BAD_NUMBER_HINTS):
            return True
    return False


def _dial_phones_in_order(
    bland: "BlandClient",
    phones: List[str],
    dial_fn,
    halt_check=None,
) -> Dict[str, Any]:
    """Try each phone in order until one connects or all fail.

    dial_fn(phone) -> call_summary. A "bad number" failure moves to the
    next phone; a service-level failure stops immediately (don't burn the
    client's other numbers on a broken service).

    Returns the winning call_summary, annotated with:
      phones_tried: [{result: 'disconnected'|'failed'|...}] (labels, no digits)
      phone_used_index: index into phones of the successful (or last) attempt
    """
    tried: List[Dict[str, str]] = []
    last_summary: Dict[str, Any] = {"attempts": []}
    for i, phone in enumerate(phones):
        summary = dial_fn(phone)
        last_summary = summary
        if _call_connected(summary):
            tried.append({"result": "connected"})
            summary["phones_tried"] = tried
            summary["phone_used_index"] = i
            return summary
        if _is_bad_number_failure(summary):
            tried.append({"result": _short_failure(summary)})
            continue  # next phone
        # Service-level failure — stop, don't burn other numbers.
        tried.append({"result": "service failure"})
        summary["phones_tried"] = tried
        summary["phone_used_index"] = i
        return summary
    # Every number was bad.
    last_summary["phones_tried"] = tried
    last_summary["phone_used_index"] = len(phones) - 1
    last_summary["all_phones_bad"] = True
    return last_summary


def _call_connected(call_summary: Dict[str, Any]) -> bool:
    """Same verdict as writeback._call_succeeded, without the import cycle."""
    if call_summary.get("mode") == "DRY_RUN":
        return False
    from writeback import _attempt_outcome
    return any(
        _attempt_outcome(a) == "connected"
        for a in (call_summary.get("attempts") or [])
    )


def _short_failure(call_summary: Dict[str, Any]) -> str:
    """One-word-ish label for why a number failed (no digits, for notes)."""
    for a in call_summary.get("attempts") or []:
        if a.get("success"):
            continue
        err = str(a.get("error") or "").lower()
        if "disconnected" in err:
            return "was disconnected"
        if "invalid" in err:
            return "was invalid"
        if "no longer in service" in err or "not in service" in err:
            return "was no longer in service"
        if "unreachable" in err:
            return "was unreachable"
    return "did not work"


def build_freeform_context(
    ez: EZLynxClient,
    applicant_id: str,
    label_name: str = "",
    discussion_id: str = "",
    note_body: str = "",
) -> Dict[str, Any]:
    """Assemble everything Eva needs for a freeform call.

    Instruction priority:
    1. note_body from the webhook (PRIMARY) — the actual note text the
       user typed, POSTed by the Zapier "New Note" trigger. The EZLynx APIs
       never return note bodies, so this is the only way to get the real
       instruction.
    2. Discussion title via the v8 API (FALLBACK) — used only when no
       note_body arrived. discussion_id given: read that discussion's
       title; otherwise the most recently modified discussion's title.
    3. Empty/generic: proceed with applicant context only; the writeback
       notes that no specific instruction was given.

    discussion_id is still recorded for the writeback (the outcome note is
    posted to the triggering discussion).

    Returns dict with: name, first_name, phone, instruction,
    instruction_source, policies, trigger_discussion_id, errors.
    """
    ctx: Dict[str, Any] = {
        "name": "the client", "first_name": "", "phone": "",
        "instruction": "", "instruction_source": "",
        "policies": [], "trigger_discussion_id": None, "errors": [],
    }

    app_res = ez.get_applicant(applicant_id)
    if app_res.get("status") == "success":
        contact = EZLynxClient.extract_contact(app_res.get("applicant", {}))
        ctx.update({k: contact[k] for k in ("name", "first_name", "phone")})
        ctx["phones"] = contact.get("phones", [])
    else:
        ctx["errors"].append(f"applicant lookup failed: {app_res.get('error')}")

    pol_res = ez.get_applicant_policies(applicant_id)
    if pol_res.get("status") == "success":
        ctx["policies"] = pol_res.get("policies", [])
    else:
        ctx["errors"].append(f"policy lookup failed: {pol_res.get('error')}")

    # PRIMARY: the note text POSTed by the Zap. No API title fetch needed.
    body = (note_body or "").strip()
    if body:
        ctx["instruction"] = body
        ctx["instruction_source"] = "webhook note_body"
        # The Zap's discussion is where the note lives; write back there.
        if discussion_id:
            ctx["trigger_discussion_id"] = discussion_id
    else:
        # FALLBACK: discussion title via the v8 API.
        disc = None
        if discussion_id:
            disc = ez.get_discussion_by_id(applicant_id, discussion_id)
            if not disc:
                ctx["errors"].append(
                    f"discussion {discussion_id} not found; fell back to most recent"
                )
        if disc is None:
            disc = ez.most_recent_discussion(applicant_id)

        if disc:
            found_id = EZLynxClient.discussion_id_of(disc)
            if found_id:
                ctx["trigger_discussion_id"] = found_id
            title = str(disc.get("title") or "").strip()
            if is_generic_title(title):
                ctx["errors"].append(
                    "discussion title was empty/generic — no specific instruction; "
                    "proceeding with applicant context only"
                )
            else:
                ctx["instruction"] = title
                # Track where the instruction came from.
                if discussion_id and found_id == discussion_id:
                    ctx["instruction_source"] = f"discussion {discussion_id} title"
                else:
                    ctx["instruction_source"] = "most recent discussion title"
        else:
            ctx["errors"].append("no discussions found for applicant")

    return ctx


def dispatch(
    applicant_id: str,
    campaign_id: str,
    label_name: str = "",
    label_id: str = "",
    discussion_id: str = "",
    note_body: str = "",
    dry_run: Optional[bool] = None,
    ez: Optional[EZLynxClient] = None,
    bland: Optional[BlandClient] = None,
) -> Dict[str, Any]:
    """Route one webhook hit to the right flow and execute it.

    discussion_id (optional): the EZLynx discussion that carried the trigger
    note — the writeback posts the outcome note to this discussion.

    note_body (optional): the trigger note's text, POSTed by the Zapier
    "New Note" trigger. PRIMARY instruction for the freeform "Robie Call"
    flow (the EZLynx API never returns note bodies). Falls back to the
    discussion title via the v8 API when absent.

    Returns a result dict with flow, mode, call_summary, and writeback info.
    Never raises on downstream failures — they are captured in the result.
    """
    dry = Config.DRY_RUN if dry_run is None else dry_run
    live_blocked_reason = ""
    if dry_run is None and not dry:
        # Production default path: enforce the fail-closed live gate
        # (ROBIE_VOICE_AUTODIAL_LIVE=1, no kill switch, Bland key present).
        # An explicit dry_run parameter is a test seam and bypasses the
        # gate; app.py never passes one.
        #
        # Kill switch sources (either one halts):
        #  - env vars ROBIE_HALT / ROBIE_READ_ONLY (existing, needs redeploy)
        #  - Secret Manager secret "bland-dispatcher-kill-switch" (instant,
        #    no redeploy; cached 60s). See README "Kill switch".
        if kill_switch_active():
            logger.warning("kill switch active; failing closed (no call)")
            return {
                "applicant_id": applicant_id,
                "campaign_id": campaign_id,
                "flow": route_campaign(campaign_id),
                "dry_run": dry,
                "ok": False,
                "error": "kill switch active",
            }
        allowed, reason = Config.live_calls_allowed()
        if not allowed:
            logger.warning("live calls blocked (%s); forcing dry-run", reason)
            dry = True
            live_blocked_reason = reason
    ez = ez or EZLynxClient()
    bland = bland or BlandClient()

    result: Dict[str, Any] = {
        "applicant_id": applicant_id,
        "campaign_id": campaign_id,
        "flow": None,  # set below after route_campaign
        "dry_run": dry,
    }
    if live_blocked_reason:
        result["live_blocked_reason"] = live_blocked_reason

    # Fail fast when Bland is known-down: skip the EZLynx lookups entirely
    # and report a clear error instead of a confusing downstream failure.
    # getattr: test doubles may not implement is_available.
    bland_available = getattr(bland, "is_available", None)
    if not dry and callable(bland_available) and not bland_available():
        logger.warning("Bland circuit breaker open; failing fast before lookups")
        result.update({
            "flow": route_campaign(campaign_id),
            "ok": False,
            "error": "Bland calling service temporarily unavailable (circuit breaker open)",
        })
        return result

    result["flow"] = route_campaign(campaign_id)

    if not applicant_id:
        result.update({"ok": False, "error": "missing applicant_id"})
        return result

    if result["flow"] == "unknown":
        result.update({"ok": False, "error": f"unknown campaign_id: {campaign_id}"})
        return result

    scripts = load_scripts()
    ctx: Optional[Dict[str, Any]] = None
    det_name = "the client"

    if result["flow"] == "freeform":
        ctx = build_freeform_context(
            ez, applicant_id, label_name or "Robie Call",
            discussion_id=discussion_id, note_body=note_body,
        )
        result["context_errors"] = ctx["errors"]
        result["instruction_source"] = ctx["instruction_source"]
        phones = [p for p in (ctx.get("phones") or []) if p]
        if not phones and not dry:
            result.update({"ok": False, "error": "no phone number on applicant"})
            return result
        phones = phones or ["+10000000000"]  # dry-run placeholder
        instruction = ctx["instruction"] or "(no specific instruction — proceeding on account context)"
        reason = _infer_reason_sentence(instruction, ctx["policies"])
        vm = voicemail_message(reason)
        fs_template = first_sentence(ctx["first_name"], reason)
        metadata = {"campaign_id": campaign_id, "applicant_id": applicant_id,
                    "flow": "freeform", "label_name": label_name,
                    "discussion_id": discussion_id,
                    "instruction_source": ctx["instruction_source"]}

        def _dial(phone: str) -> Dict[str, Any]:
            task = build_freeform_task(
                ctx["name"], ctx["first_name"], instruction,
                ctx["policies"], phone,
                instruction_source=ctx["instruction_source"],
            )
            return bland.call_with_double_dial(
                phone, task, fs_template, vm, metadata=metadata,
                dry_run=dry, halt_check=kill_switch_active)

        call_summary = _dial_phones_in_order(bland, phones, _dial)
        result["trigger_discussion_id"] = ctx["trigger_discussion_id"]
        if call_summary.get("all_phones_bad"):
            result["phone_failover"] = (
                f"tried {len(phones)} numbers on file; all were bad")
    else:
        script = scripts.get(canonical_campaign_id(campaign_id), {})
        if script.get("script", "").startswith("PENDING"):
            result.update({
                "ok": False,
                "error": f"no approved script for campaign {campaign_id} (hard stop)",
            })
            return result
        app_res = ez.get_applicant(applicant_id)
        contact = EZLynxClient.extract_contact(
            app_res.get("applicant", {}) if app_res.get("status") == "success" else {})
        phones = [p for p in (contact.get("phones") or []) if p]
        if not phones and not dry:
            result.update({"ok": False, "error": "no phone number on applicant"})
            return result
        phones = phones or ["+10000000000"]
        reason = script.get("reason_sentence", "")
        vm = voicemail_message(reason)
        fs_template = first_sentence(contact["first_name"], reason)
        metadata = {"campaign_id": campaign_id, "applicant_id": applicant_id,
                    "flow": "deterministic", "label_name": label_name}

        def _dial(phone: str) -> Dict[str, Any]:
            task = build_deterministic_task(
                contact["name"], contact["first_name"], campaign_id, script)
            return bland.call_with_double_dial(
                phone, task, fs_template, vm, metadata=metadata,
                dry_run=dry, halt_check=kill_switch_active)

        call_summary = _dial_phones_in_order(bland, phones, _dial)
        # The Zap's discussion is where the trigger note lives — write back
        # there (was: None, which silently fell back to most-recent).
        result["trigger_discussion_id"] = discussion_id or None
        det_name = contact["name"]
        if call_summary.get("all_phones_bad"):
            result["phone_failover"] = (
                f"tried {len(phones)} numbers on file; all were bad")

    result["call_summary"] = call_summary

    # A dispatch is only ok if the call itself verifiably CONNECTED
    # (not just POST-accepted). Dry-run skips this: nothing was dialed.
    from writeback import _call_succeeded, _call_outcome_unknown
    call_ok = True
    if not dry:
        call_ok = _call_succeeded(call_summary)
        if not call_ok:
            errs = [str(a.get("error") or "unknown") for a in
                    (call_summary.get("attempts") or []) if not a.get("success")]
            result["call_error"] = "; ".join(errs) or "Bland call failed"
            # POST timeout/network failure: the call may have been accepted
            # by Bland anyway. Check recent calls before declaring failure —
            # a false "NOT successful" note for a call that happened is the
            # September-incident class of bug.
            if _looks_like_timeout(call_summary):
                recent = _check_recent_calls(bland, phones)
                if recent:
                    result["possible_call_placed"] = True
                    result["call_error"] = (
                        "call request timed out; a recent call to this number "
                        "was found — outcome unconfirmed")

    # ---- Writeback (Carlo's standing rule) ----
    if not dry:
        note_name = ctx["name"] if ctx else det_name
        result["writeback"] = _writeback(
            ez, applicant_id, campaign_id, label_name,
            result.get("trigger_discussion_id"),
            note_name,
            call_summary,
            bland,
        )
        # Surface writeback failures: a failed note write means the
        # end-to-end outcome is NOT ok, even if the call itself succeeded.
        wb = result["writeback"] or {}
        note_res = wb.get("note") or {}
        note_ok = note_res.get("status") != "error"
        if not note_ok:
            result["writeback_error"] = note_res.get("error") or note_res
        # Surface a failed MP3 upload distinctly (note may still be ok).
        rec_res = wb.get("recording") or {}
        if rec_res.get("status") in ("error", "failed"):
            result["recording_error"] = rec_res.get("error") or rec_res.get("reason") or rec_res
        result["ok"] = bool(call_ok and note_ok)
    else:
        result["writeback"] = {"skipped": "dry_run"}
        result["ok"] = True
    return result


def _looks_like_timeout(call_summary: Dict[str, Any]) -> bool:
    """True when failures smell like 'request died, call may have gone out'."""
    for a in call_summary.get("attempts") or []:
        if a.get("success"):
            continue
        err = str(a.get("error") or "").lower()
        if any(h in err for h in ("timed out", "timeout", "connection",
                                  "network")):
            return True
    return False


def _check_recent_calls(bland: "BlandClient", phones: List[str]) -> bool:
    """Ask Bland whether a call went to any of these numbers recently.

    Best-effort: any failure returns False (we just don't get the
    disambiguation). Never raises.
    """
    try:
        for phone in phones:
            res = bland.recent_calls(phone, since_seconds=300)
            if res.get("ok") and res.get("calls"):
                logger.warning("recent Bland call found for %s after timeout",
                               phone[-4:])
                return True
    except Exception:
        logger.exception("recent_calls check failed")
    return False


def _infer_reason_sentence(instruction: str, policies: List[Dict[str, Any]]) -> str:
    """Best-effort one-sentence reason for the voicemail/first sentence.

    Uses the freeform instruction (note_body from the Zap, or the
    discussion-title fallback); if it names a policy event
    (renewal, payment, documents), keeps it specific. Falls back to a
    generic account follow-up.
    """
    text = (instruction or "").strip()
    if text and not text.startswith("(no specific instruction"):
        # Keep it to one sentence for the voicemail/first-sentence slot.
        first = text.split(".")[0].strip()
        if first:
            return first[:220]
    return "I'm following up on your StreetSmart Insurance account"


def _writeback(
    ez: EZLynxClient,
    applicant_id: str,
    campaign_id: str,
    label_name: str,
    trigger_discussion_id: Optional[str],
    client_name: str,
    call_summary: Dict[str, Any],
    bland: BlandClient,
) -> Dict[str, Any]:
    """Post the call note and upload the MP3. Returns per-step statuses."""
    out: Dict[str, Any] = {}
    discussions = ez.get_discussions(applicant_id)
    disc_id: Optional[str] = None
    if trigger_discussion_id:
        tid = str(trigger_discussion_id)
        if not discussions:
            # Couldn't list discussions (transient?) — try the trigger ID
            # anyway; the POST result will tell us if it's bad.
            logger.warning("discussion list empty; attempting writeback to "
                           "trigger discussion %s blind", tid)
            disc_id = tid
        elif any(EZLynxClient.discussion_id_of(d) == tid for d in discussions):
            disc_id = tid
        else:
            logger.warning("trigger discussion %s not found; falling back "
                           "to most recent", tid)
    if not disc_id:
        disc_id = pick_writeback_discussion(discussions, None)

    # MP3 first, so the note can say whether the recording is available.
    out["recording"] = {"status": "skipped", "reason": "no completed call"}
    for attempt in call_summary.get("attempts", []):
        call_id = attempt.get("call_id")
        if not call_id or attempt.get("mode") == "DRY_RUN":
            continue
        detail = bland.get_call(call_id)
        rec_url = detail.get("recording_url") or detail.get("recordingUrl")
        if not rec_url:
            # No recording URL yet (call may still be processing).
            out["recording"] = {"status": "pending",
                                "reason": "recording not yet available",
                                "call_id": call_id}
            break
        mp3 = _download_bytes(rec_url)
        if not mp3 or not _looks_like_audio(mp3):
            # URL existed but the audio couldn't be fetched or was empty.
            # Report FAILED, not "pending" — nobody should wait for a
            # recording that will never arrive.
            out["recording"] = {"status": "failed",
                                "reason": "recording download failed or empty",
                                "call_id": call_id}
            break
        up = ez.upload_document(
            applicant_id,
            f"bland-call-{call_id}.mp3",
            mp3,
            content_type="audio/mpeg",
        )
        if up.get("status") == "success":
            out["recording"] = {"status": "ok",
                                "document_id": up.get("document_id"),
                                "call_id": call_id}
        else:
            out["recording"] = {"status": "failed",
                                "error": up.get("error"),
                                "call_id": call_id}
        # Enrich the note result with the call detail for the summary.
        if out["recording"]["status"] == "ok":
            call_summary["recording_url"] = "saved to Documents"
        break

    rec_status = str(out["recording"].get("status") or "")
    note_body = format_call_note(
        client_name, campaign_id, label_name, call_summary,
        recording_status=rec_status)
    if disc_id:
        out["note"] = ez.append_note(disc_id, note_body)
    else:
        out["note"] = {"status": "error", "error": "no discussion found for writeback"}
    return out


def _download_bytes(url: str) -> Optional[bytes]:
    import requests

    try:
        resp = requests.get(url, timeout=60)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception:
        pass
    return None


def _looks_like_audio(data: bytes) -> bool:
    """Sanity check: non-trivial size and a plausible audio header.

    MP3s start with ID3 or an MPEG frame sync (0xFF 0xFx); WAVs with RIFF.
    Anything else (HTML error page, empty body) is not a recording.
    """
    if not data or len(data) < 1024:
        return False
    head = data[:4]
    if head[:3] == b"ID3":
        return True
    if head[:2] == b"\xff\xf3" or head[:2] == b"\xff\xfb":
        return True
    if head == b"RIFF":
        return True
    # Unknown container but substantial — accept (don't lose recordings
    # over a header heuristic), the size floor already rejects error pages.
    return True
