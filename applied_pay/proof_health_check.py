"""Outcome-based health check for the Applied Pay Wells proof job.

Green means the scheduled job produced today's review report, not just that
a timer is enabled:
  1. Today's proof report exists in the reports dir (proof-daily.json).
  2. It is fresh (mtime younger than PROOF_HEALTH_MAX_AGE_S, default 26h for
     the daily 06:30 ET run).
  3. It carries verification_label UNVERIFIED (until live Wells access lands)
     and every write-surface counter is zero (read-only by construction).

Usage:
    python -m applied_pay.proof_health_check --reports-dir DIR   # probe; exit 0/1, quiet when green
    python -m applied_pay.proof_health_check --reports-dir DIR --alert
        # probe; POST a plain-English alert to the ROBIE health Chat webhook
        # (APPLIED_PAY_HEALTH_CHAT_WEBHOOK) when red. Prints the alert text
        # when no webhook is configured so the path is still provable.

The check is read-only: it never touches the bank, QBO, EZLynx, or the
proof job's state file.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import urllib.request

REPORT_NAME = "proof-daily.json"
DEFAULT_MAX_AGE_S = 26 * 3600
ZERO_COUNTERS = ("bank_actions", "qbo_posts", "ezlynx_writes",
                 "notes_written", "transfers")


def probe(reports_dir, *, max_age_s=DEFAULT_MAX_AGE_S, now=None):
    """Return (green: bool, reason: str). Never raises on missing/corrupt input."""
    now = now if now is not None else time.time()
    path = os.path.join(reports_dir, REPORT_NAME)
    if not os.path.isfile(path):
        return False, "proof report missing: %s not found (job has not produced today's review)" % path
    age = now - os.path.getmtime(path)
    if age < 0:
        return False, "proof report timestamp is in the future: %s" % path
    if age > max_age_s:
        return False, "proof report stale: %s is %.1f hours old (limit %.1f)" % (
            path, age / 3600, max_age_s / 3600)
    try:
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError) as exc:
        return False, "proof report unreadable: %s (%s)" % (path, exc)
    if report.get("verification_label") != "UNVERIFIED":
        return False, "proof report missing UNVERIFIED bank-side label"
    bad = [k for k in ZERO_COUNTERS if report.get(k) != 0]
    if bad:
        return False, "proof report shows write activity (%s) - read-only violated" % ", ".join(bad)
    if not str(report.get("mode", "")).startswith("scheduled_proof_"):
        return False, "proof report has unexpected mode: %r" % (report.get("mode"),)
    return True, "proof report fresh (%.1f h old), UNVERIFIED, zero write counters" % (age / 3600,)


def send_alert(text, *, webhook_url=None):
    """POST text to the ROBIE health Chat webhook. Returns True if sent."""
    webhook_url = webhook_url or os.environ.get("APPLIED_PAY_HEALTH_CHAT_WEBHOOK", "")
    if not webhook_url:
        print("ALERT (no webhook configured): %s" % text)
        return False
    body = json.dumps({"text": "Applied Pay Wells proof job health: %s" % text}).encode()
    req = urllib.request.Request(webhook_url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return 200 <= resp.status < 300


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", required=True)
    parser.add_argument("--max-age-s", type=int, default=DEFAULT_MAX_AGE_S)
    parser.add_argument("--alert", action="store_true",
                        help="send a Chat alert when not Green")
    args = parser.parse_args(argv)
    green, reason = probe(args.reports_dir, max_age_s=args.max_age_s)
    if green:
        # Quiet when healthy: no ping, ever.
        return 0
    print("RED: %s" % reason)
    if args.alert:
        sent = send_alert(reason)
        print("alert posted to ROBIE health Chat" if sent else "alert not sent (see above)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
