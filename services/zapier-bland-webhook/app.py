"""Bland dispatcher webhook service.

Replaces the stub at https://zapier-bland-webhook-*.run.app/webhook.

Endpoints:
- GET /health            -> {"status": "ok", ...} (Cloud Run health checks)
- GET /health/deep       -> outcome health check: verifies secrets, scripts,
                            EZLynx auth (Classic + OAuth), and runs a full
                            dry-run dispatch against a test applicant.
                            200 {"status": "ok"} / 503 {"status": "degraded"}.
- POST /webhook          -> accepts form-encoded (Zapier) or JSON bodies:
    {applicant_id, label_name, label_id, campaign_id, discussion_id, note_body}
  Responds immediately with {"status": "received", ...}; the dispatch
  (EZLynx lookup -> Bland call w/ double-dial -> EZLynx writeback) runs in
  a background thread so Zapier never waits on a phone call.

  discussion_id is optional but recommended: it is used for the writeback
  (the outcome note is posted to that discussion).
  note_body (alias: note_text) is the primary instruction for the freeform
  "Robie Call" flow: the Zapier "New Note" trigger sees the full note text,
  which the EZLynx API never returns, so the Zap POSTs it here. The
  discussion TITLE is only a fallback instruction (see README).

Safety: live dialing is fail-closed. DRY_RUN=1 (default) logs the exact
Bland payload that WOULD be sent without placing any call. Live calls also
require ROBIE_VOICE_AUTODIAL_LIVE=1 and no kill switch.
"""
import logging
import os
import threading
import time
from typing import Any, Dict, Tuple

from flask import Flask, jsonify, request

from config import Config
from dispatcher import dispatch, route_campaign
from alerts import failures, double_labels, post_chat_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("bland_dispatcher.app")

app = Flask(__name__)

# Idempotency: Zapier retries webhook POSTs it thinks failed. A retry that
# arrives while the first dispatch is still in flight (or recently finished)
# must NOT place a second call. Keyed on (applicant, campaign, discussion).
_IDEMPOTENCY_TTL_S = 1800  # 30 minutes
_seen: Dict[Tuple[str, str, str], float] = {}
_seen_lock = threading.Lock()

# Defensive cap: the note text becomes Eva's prompt; bound it.
NOTE_BODY_MAX_CHARS = 2000

# Applicant used by /health/deep for its dry-run probe (read-only).
TEST_APPLICANT_ID = os.environ.get("HEALTH_TEST_APPLICANT_ID", "25486692")


@app.get("/health")
def health():
    allowed, reason = Config.live_calls_allowed()
    return jsonify({
        "status": "ok",
        "dry_run": Config.DRY_RUN,
        "live_calls_allowed": allowed,
        "live_calls_reason": reason,
        "voice_id": Config.VOICE_ID,
        "from_number": Config.FROM_NUMBER,
    })


@app.get("/health/deep")
def health_deep():
    """Outcome health check (Carlo's standing rule): verify the thing the
    service is FOR still works, not just that the process is up.

    Checks (all read-only; the probe dispatch is forced dry-run):
    1. required secrets present (names only, never values)
    2. all 10 deterministic scripts load with none PENDING
    3. EZLynx Classic auth + phone lookup on the test applicant
    4. EZLynx OAuth token obtainable (Discussion/Document APIs)
    5. full dry-run dispatch end-to-end (no call placed)
    6. Bland API key present

    200 {"status": "ok"} when every check passes, else 503
    {"status": "degraded", "checks": {...}}.
    """
    from ezlynx_client import EZLynxClient
    from prompts import load_scripts, deterministic_campaigns

    checks: Dict[str, Any] = {}
    ok_all = True

    def _record(name: str, ok: bool, detail: str = ""):
        nonlocal ok_all
        checks[name] = {"ok": ok, "detail": detail}
        if not ok:
            ok_all = False

    # 1. secrets / env present
    required = ["BLAND_API_KEY", "EZLYNX_USER", "EZLYNX_PASSWORD",
                "EZLYNX_APP_SECRET", "EZLYNX_OAUTH_CLIENT_ID",
                "EZLYNX_OAUTH_CLIENT_SECRET", "EZLYNX_OAUTH_USERNAME",
                "EZLYNX_INTEGRATION_GROUP_ID"]
    missing = [n for n in required
               if not (os.environ.get(n) or getattr(Config, n, ""))]
    _record("secrets_present", not missing,
            "missing: " + ",".join(missing) if missing else "all 8 present")

    # 2. scripts load
    try:
        scripts = load_scripts()
        cids = deterministic_campaigns()
        pending = [c for c in cids if str(scripts.get(c, {}).get("script", ""))
                   .startswith("PENDING")]
        _record("scripts", len(cids) == 10 and not pending,
                f"{len(cids)} campaigns"
                + (f"; PENDING: {pending}" if pending else ""))
    except Exception as e:
        _record("scripts", False, f"load failed: {e}")

    # 3+4. EZLynx auth
    ez = EZLynxClient()
    try:
        app_res = ez.get_applicant(TEST_APPLICANT_ID)
        if app_res.get("status") == "success":
            contact = EZLynxClient.extract_contact(app_res.get("applicant", {}))
            _record("ezlynx_classic", bool(contact["phone"]),
                    "phone found" if contact["phone"] else "no phone on test applicant")
        else:
            _record("ezlynx_classic", False,
                    str(app_res.get("error"))[:120])
    except Exception as e:
        _record("ezlynx_classic", False, f"exception: {e}")
    try:
        token = ez._oauth_token_get()
        _record("ezlynx_oauth", bool(token),
                "token obtained" if token else "token request failed")
    except Exception as e:
        _record("ezlynx_oauth", False, f"exception: {e}")

    # 5. dry-run dispatch end-to-end (never dials)
    try:
        res = dispatch(TEST_APPLICANT_ID, "robie-unresponsive",
                       "Robie unresponsive", "",
                       discussion_id="", note_body="health check probe",
                       dry_run=True)
        _record("dry_run_dispatch", bool(res.get("ok")),
                res.get("error") or "ok")
    except Exception as e:
        _record("dry_run_dispatch", False, f"exception: {e}")

    # 6. Bland key present (no call placed)
    _record("bland_key", bool(Config.BLAND_API_KEY),
            "present" if Config.BLAND_API_KEY else "missing")

    status = "ok" if ok_all else "degraded"
    code = 200 if ok_all else 503
    logger.info("health/deep: %s %s", status, checks)
    return jsonify({"status": status, "checks": checks}), code


@app.route("/health/scheduled-check", methods=["GET", "POST"])
def health_scheduled_check():
    """Scheduled outcome health check (Carlo's standing rule).

    Called daily by Cloud Scheduler. Runs the deep check; stays silent
    when healthy, posts a plain-English alert to the ROBIE health Chat
    when degraded. Returns 200 either way (the alert is the signal).
    """
    # Run the deep check logic directly.
    with app.test_request_context():
        resp, code = health_deep()
    body = resp.get_json() or {}
    checks = body.get("checks", {})
    healthy = code == 200 and body.get("status") == "ok"

    if healthy:
        logger.info("scheduled health check: healthy, silent")
        return jsonify({"status": "ok", "alerted": False})

    bad = {k: v for k, v in checks.items() if not v.get("ok")}
    detail = "; ".join(
        f"{k}: {v.get('detail', 'failed')}" for k, v in bad.items()
    ) or "unknown failure"
    post_chat_alert(
        "The Bland dispatcher is NOT healthy. "
        "Label-triggered calls may not go out.\n"
        f"Failing checks: {detail}\n"
        "The dispatcher stays fail-closed (no calls) until this is fixed."
    )
    logger.warning("scheduled health check: UNHEALTHY, alert posted: %s", detail)
    return jsonify({"status": "degraded", "alerted": True, "detail": detail})


def _is_duplicate(applicant_id: str, campaign_id: str,
                  discussion_id: str) -> bool:
    """True if this exact trigger was seen within the TTL window."""
    key = (applicant_id, campaign_id, discussion_id or "")
    now = time.time()
    with _seen_lock:
        # Prune expired entries.
        expired = [k for k, ts in _seen.items() if now - ts > _IDEMPOTENCY_TTL_S]
        for k in expired:
            del _seen[k]
        if key in _seen:
            return True
        _seen[key] = now
        return False


def _parse_payload() -> Dict[str, Any]:
    """Accept form-encoded (current Zapier) and JSON POST bodies.

    note_body carries the Zapier "New Note" trigger's note text — the
    primary instruction for the freeform flow. note_text is accepted as an
    alias and normalized to note_body.
    """
    data: Dict[str, Any] = {}
    if request.is_json:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            data.update(body)
    # Form fields (Zapier posts form-encoded); also covers query params.
    for key in ("applicant_id", "label_name", "label_id", "campaign_id",
                "discussion_id", "note_body", "note_text"):
        if key not in data or not data[key]:
            v = request.form.get(key) or request.args.get(key)
            if v:
                data[key] = v
    cleaned = {k: str(v).strip() for k, v in data.items() if v is not None}
    # Alias normalization: note_text -> note_body.
    if not cleaned.get("note_body") and cleaned.get("note_text"):
        cleaned["note_body"] = cleaned.pop("note_text")
    elif "note_text" in cleaned:
        cleaned.pop("note_text")  # note_body wins when both are present
    # Defensive cap: the note text becomes Eva's prompt; bound it.
    if len(cleaned.get("note_body", "")) > NOTE_BODY_MAX_CHARS:
        logger.warning("note_body truncated from %d to %d chars",
                       len(cleaned["note_body"]), NOTE_BODY_MAX_CHARS)
        cleaned["note_body"] = cleaned["note_body"][:NOTE_BODY_MAX_CHARS]
    return cleaned


@app.post("/webhook")
def webhook():
    payload = _parse_payload()
    applicant_id = payload.get("applicant_id", "")
    campaign_id = payload.get("campaign_id", "")
    label_name = payload.get("label_name", "")
    label_id = payload.get("label_id", "")
    discussion_id = payload.get("discussion_id", "")
    note_body = payload.get("note_body", "")

    logger.info(
        "webhook hit: applicant_id=%s campaign_id=%s label_name=%s "
        "discussion_id=%s note_body=%s",
        applicant_id, campaign_id, label_name, discussion_id,
        "(present)" if note_body else "(absent)",
    )

    if not applicant_id or not campaign_id:
        return jsonify({
            "status": "rejected",
            "error": "applicant_id and campaign_id are required",
        }), 400

    # Fail fast on malformed input before anything is queued.
    if not applicant_id.isdigit():
        return jsonify({
            "status": "rejected",
            "error": f"applicant_id must be numeric, got: {applicant_id[:40]}",
        }), 400
    if route_campaign(campaign_id) == "unknown":
        return jsonify({
            "status": "rejected",
            "error": f"unknown campaign_id: {campaign_id[:60]}",
        }), 400
    # Zapier retries POSTs it thinks failed; a retry must not place a
    # second call for the same trigger.
    if _is_duplicate(applicant_id, campaign_id, discussion_id):
        logger.warning("duplicate webhook suppressed: %s %s %s",
                       applicant_id, campaign_id, discussion_id)
        return jsonify({"status": "duplicate_suppressed"}), 202

    thread = threading.Thread(
        target=_run_dispatch,
        args=(applicant_id, campaign_id, label_name, label_id, discussion_id,
              note_body),
        daemon=True,
        name=f"dispatch-{applicant_id}-{campaign_id}",
    )
    thread.start()

    # Back-compat with the stub the Zaps already expect.
    return jsonify({"status": "received"})


def _run_dispatch(applicant_id: str, campaign_id: str, label_name: str,
                  label_id: str, discussion_id: str, note_body: str):
    try:
        # Double-label guard: two DIFFERENT campaigns for one applicant in
        # 5 minutes is worth a heads-up (both calls still go out).
        dbl = double_labels.check(applicant_id, campaign_id)
        if dbl:
            logger.warning("double-label: %s", dbl)
            post_chat_alert(dbl)

        result = dispatch(applicant_id, campaign_id, label_name, label_id,
                          discussion_id=discussion_id, note_body=note_body)
        logger.info("dispatch result: %s", _summarize(result))

        if not result.get("ok"):
            # Single failure: logged + returned; the chat alert fires only
            # on the 3-in-15-min pattern (no spam).
            err = str(result.get("error") or result.get("call_error") or
                      result.get("writeback_error") or "unknown")
            failures.record(applicant_id, campaign_id, err)
        else:
            # Call succeeded but the EZLynx note didn't land: silent data
            # loss. ALWAYS alert — someone may need to write the note manually.
            wb_err = result.get("writeback_error")
            if wb_err:
                call_ids = []
                try:
                    for a in (result.get("call_summary") or {}).get("attempts", []):
                        if a.get("call_id"):
                            call_ids.append(a["call_id"])
                except Exception:
                    pass
                post_chat_alert(
                    "Bland call completed but EZLynx writeback failed. "
                    f"Applicant: {applicant_id}, Campaign: {campaign_id}, "
                    f"Call IDs: {', '.join(call_ids) or 'n/a'}. "
                    "Manual note may be needed."
                )
    except Exception:
        logger.exception("dispatch crashed for applicant %s", applicant_id)
        failures.record(applicant_id, campaign_id, "dispatch crashed")


def _summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "applicant_id": result.get("applicant_id"),
        "campaign_id": result.get("campaign_id"),
        "flow": result.get("flow"),
        "ok": result.get("ok"),
        "error": result.get("error"),
        "dry_run": result.get("dry_run"),
        "mode": (result.get("call_summary") or {}).get("mode"),
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT)
