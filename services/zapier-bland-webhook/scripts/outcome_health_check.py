#!/usr/bin/env python3
"""Outcome health check for the Bland dispatcher (Carlo's standing rule).

Probes GET /health/deep on the dispatcher. On degraded/unreachable, posts a
plain-English alert to the ROBIE health Chat. Exit code is non-zero on
failure so schedulers can see it too.

Carlo's rule, applied:
- The check verifies the OUTCOME path (secrets, EZLynx auth, dry-run
  dispatch), not just "is the server up".
- --test-alert proves the alert path fires (a check that has never failed
  on purpose isn't trusted).
- --test-quiet proves it stays silent when healthy.

Usage:
    python3 outcome_health_check.py [--base-url URL] [--test-alert] [--test-quiet]

Env:
    DISPATCHER_BASE_URL       default https://zapier-bland-webhook-751771086524.us-east1.run.app
    ROBIE_HEALTH_CHAT_WEBHOOK chat webhook URL for alerts (required for alerting;
                              without it, failures are logged + non-zero exit only)

Wire-up (Cloud Scheduler -> Cloud Run Job, daily ~7:05 AM ET, i.e. after any
nightly work but before the business day):
    # 1. Build a tiny job image (Dockerfile.job in this repo) and push it:
    #      gcloud builds submit --tag us-east1-docker.pkg.dev/streetsmart-hermes-poc/ops/bland-health-check .
    # 2. Create the job with the chat webhook mounted from Secret Manager:
    #      gcloud run jobs create bland-dispatcher-health-check \
    #        --image us-east1-docker.pkg.dev/streetsmart-hermes-poc/ops/bland-health-check \
    #        --region us-east1 --project streetsmart-hermes-poc \
    #        --set-secrets ROBIE_HEALTH_CHAT_WEBHOOK=accountability-robie-health-chat-webhook:latest \
    #        --set-env-vars DISPATCHER_BASE_URL=https://zapier-bland-webhook-751771086524.us-east1.run.app
    # 3. Schedule it:
    #      gcloud scheduler jobs create http bland-dispatcher-health-check \
    #        --schedule="5 7 * * *" --time-zone="America/New_York" \
    #        --uri="https://us-east1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/streetsmart-hermes-poc/jobs/bland-dispatcher-health-check:run" \
    #        --http-method=POST \
    #        --oauth-service-account-email=<scheduler-sa>@streetsmart-hermes-poc.iam.gserviceaccount.com
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = os.environ.get(
    "DISPATCHER_BASE_URL",
    "https://zapier-bland-webhook-751771086524.us-east1.run.app",
).rstrip("/")
CHAT_WEBHOOK = os.environ.get("ROBIE_HEALTH_CHAT_WEBHOOK", "")


def _get(url: str, timeout: int = 120):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        return e.code, body
    except Exception as e:
        return 0, {"transport_error": str(e)}


def _alert(text: str):
    """Post a plain-English alert to the ROBIE health Chat."""
    message = f"*Bland dispatcher health check*\n{text}"
    if not CHAT_WEBHOOK:
        print(f"ALERT (no chat webhook configured): {message}", flush=True)
        return False
    payload = json.dumps({"text": message}).encode("utf-8")
    req = urllib.request.Request(
        CHAT_WEBHOOK, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"alert post failed: {e}", flush=True)
        return False


def _failed_checks(checks):
    return {k: v for k, v in (checks or {}).items() if not v.get("ok")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test alert to prove the alert path works")
    ap.add_argument("--test-quiet", action="store_true",
                    help="verify the check stays silent when healthy")
    args = ap.parse_args()
    base_url = args.base_url.rstrip("/")

    if args.test_alert:
        ok = _alert("TEST ALERT — the Bland dispatcher health-check alert "
                    "path is working. No action needed.")
        print("test alert posted" if ok else "test alert FAILED")
        return 0 if ok else 2

    status, body = _get(base_url + "/health/deep")
    checks = body.get("checks", {}) if isinstance(body, dict) else {}
    bad = _failed_checks(checks)
    healthy = status == 200 and body.get("status") == "ok" and not bad

    if args.test_quiet:
        print("healthy and silent" if healthy
              else f"NOT healthy (would alert): {bad or body}")
        return 0 if healthy else 2

    if healthy:
        print("ok: dispatcher outcome health check passed")
        return 0

    detail = "; ".join(
        f"{k}: {v.get('detail', 'failed')}" for k, v in bad.items()
    ) or f"HTTP {status}: {str(body)[:300]}"
    _alert("The Bland dispatcher is NOT healthy. "
           "Label-triggered calls may not go out.\n"
           f"Failing checks: {detail}\n"
           "The dispatcher stays fail-closed (no calls) until this is fixed.")
    print(f"UNHEALTHY: {detail}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
