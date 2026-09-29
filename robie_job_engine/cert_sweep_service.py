"""Box-native entry point for the certificate sweep systemd timer.

``robie-cert-sweep.service`` runs this module (not ``cert_sweep --once``
directly) so that one place owns the production wiring that used to live
in the sandbox cron definition:

1. Fetch ``CERT_CALLBACK_SECRET`` from Secret Manager via gcloud when it
   is not already set (mirrors the approved production wiring; the value
   is never printed or logged). ``CERT_CALLBACK_SECRET_FETCH=0`` skips
   the fetch.
2. Run one sweep (:func:`cert_sweep.run_sweep`).
3. Record the run in the ``sweep_runs`` table so the buddy
   (:mod:`cert_sweep_health`) can prove Green independently of any
   single run's output.
4. Print the JSON summary to stdout (the unit's journal + log shipper).
5. Hand the summary to :mod:`cert_notify` so filings, UNVERIFIED items,
   and errors reach Carlo without a human watching the timer.

Exit codes mirror ``cert_sweep.main``: 0 clean, 1 runtime failure,
2 usage/config error. Notification failures never change the exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _fetch_callback_secret() -> str:
    """Fetch the Zap callback secret from Secret Manager (transient).

    Returns "" when the fetch is disabled or fails — the sweep still
    runs; the callback-verification path degrades to UNVERIFIED rather
    than failing closed the whole sweep. The value is never logged.
    """
    if _env("CERT_CALLBACK_SECRET"):
        return "already-set"
    if _env("CERT_CALLBACK_SECRET_FETCH", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return "disabled"
    project = _env("CERT_SWEEP_GCP_PROJECT", "streetsmart-hermes-poc")
    secret = _env("CERT_CALLBACK_SECRET_NAME", "cert-callback-secret")
    try:
        out = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "1",
             f"--secret={secret}", f"--project={project}"],
            capture_output=True, timeout=60, check=False,
        )
    except Exception as exc:
        return f"fetch failed: {type(exc).__name__}"
    if out.returncode != 0:
        return "fetch failed: gcloud non-zero exit"
    value = out.stdout.decode().strip()
    if not value:
        return "fetch failed: empty value"
    os.environ["CERT_CALLBACK_SECRET"] = value
    return "ok"


def run_service() -> tuple[dict[str, Any], int]:
    """Run one sweep and notify. Returns (summary, exit_code)."""
    from .cert_notify import notify_summary
    from .cert_sweep import run_sweep
    from .cert_sweep_health import record_sweep_run

    secret_status = _fetch_callback_secret()
    try:
        summary = run_sweep()
    except RuntimeError as exc:
        summary = {"filed": [], "unverified": [],
                   "errors": [f"config error: {exc}"],
                   "sweep_at": "", "stats": {}}
        exit_code = 2
    except Exception as exc:  # never leak tracebacks with secrets
        summary = {"filed": [], "unverified": [],
                   "errors": [f"sweep failed: {type(exc).__name__}: {exc}"],
                   "sweep_at": "", "stats": {}}
        exit_code = 1
    else:
        exit_code = 0 if not summary.get("errors") else 1

    # Buddy feed: record the run so cert_sweep_health can prove Green.
    # Recording must never fail the service (record_sweep_run never raises,
    # belt and suspenders here too).
    try:
        record_sweep_run(
            exit_code=exit_code,
            filed=len(summary.get("filed") or []),
            unverified=len(summary.get("unverified") or []),
            errors=len(summary.get("errors") or []),
            elapsed_s=(summary.get("elapsed_s")
                       if isinstance(summary.get("elapsed_s"),
                                     (int, float)) else None),
        )
    except Exception:
        pass

    summary["callback_secret"] = secret_status
    try:
        notify = notify_summary(summary)
    except Exception as exc:  # belt and suspenders; notify never raises
        notify = {"notified": False, "error": str(exc)}
    summary["notify"] = notify
    return summary, exit_code


def main(argv: list[str] | None = None) -> int:
    summary, exit_code = run_service()
    # Scrub the secret status to a coarse value before printing: the
    # journal must never carry anything derived from the secret.
    summary["callback_secret"] = (
        "ok" if summary.get("callback_secret") in ("ok", "already-set")
        else "unavailable")
    print(json.dumps(summary, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
