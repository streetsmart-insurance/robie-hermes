"""Miss-watch for the certificate sweep: the EPHE-class health check.

On 2026-09-29 a Highway renewal email ("COI for EPHE LLC", policy
9300216995) held as NO_MATCH even though the policy number was right
there in the applicant index — the matcher never used policy numbers
as a key. The fix (cert_applicant_index.by_policy + "COI for <name>"
extraction) routes such requests. This probe is the tripwire for that
miss class coming back:

  - It reads the ``cert_index_misses`` ledger (read-only) for
    unresolved misses seen in the last 24 hours.
  - Any miss whose extracted policy number exists in the applicant
    index is a recurrence of the EPHE miss: the request held even
    though the book could have routed it.
  - Exit 0 = clean (nothing to report). Exit 1 = findings. Exit 2 =
    the probe itself could not run (DB or index unreadable).

Carlo's rule, tested both ways: the unit tests prove it stays quiet
on a clean ledger AND fires on a seeded miss whose policy is in the
index. A check that has never failed on purpose isn't trusted.

Usage::

    python -m robie_job_engine.cert_miss_watch          # probe; exit 0/1/2
    python -m robie_job_engine.cert_miss_watch --alert   # probe; Chat-alert when red

``--alert`` posts to the ROBIE health Chat in plain English via the
same Chat identity as :mod:`cert_notify`. Alerts are deduped: one per
red episode, a repeat at most every
``CERT_MISS_WATCH_ALERT_EVERY_S`` (default 4h) while still red, and a
recovery notice when the ledger goes clean after red. Alert state lives
in ``miss_watch_alert_state.json`` inside the sweep data dir.

The probe never touches Gmail, EZLynx, or Zapier.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

DB_FILENAME = "cert-sweep.db"
ALERT_STATE_FILENAME = "miss_watch_alert_state.json"

#: Only misses seen this recently count — the class must surface
#: within a day of the held request.
WINDOW_S = 24 * 3600


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def data_dir() -> str:
    """Sweep data dir (same resolution as cert_sweep.data_dir)."""
    path = os.path.expanduser(_env("CERT_SWEEP_DATA_DIR", "~/.cert-sweep"))
    os.makedirs(path, exist_ok=True)
    return path


def db_path_for(directory: str = "") -> str:
    return os.path.join(directory or data_dir(), DB_FILENAME)


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_misses(*, directory: str = "", csv_path: str = "") -> dict[str, Any]:
    """Run the probe. Never raises: probe errors are reported, not thrown.

    Returns a report dict with ``clean`` (bool), ``findings`` (list),
    ``misses_checked`` (int), and either ``error`` or ``note`` when the
    probe could not fully run.
    """
    report: dict[str, Any] = {
        "at": _utcnow().isoformat(timespec="seconds"),
        "clean": True,
        "findings": [],
        "misses_checked": 0,
    }

    # Applicant index: the source of truth for "policy exists in the book".
    try:
        from .cert_sweep import load_applicant_index
        index = load_applicant_index(csv_path)
        index_policies: dict[str, list[int]] = dict(index.by_policy)
    except Exception as exc:
        report["clean"] = False
        report["error"] = f"cannot load applicant index: {exc}"
        return report

    # Miss ledger, strictly read-only: an unreadable or pre-fix DB is a
    # probe error, not a clean bill of health.
    try:
        conn = sqlite3.connect(f"file:{db_path_for(directory)}?mode=ro",
                               uri=True, timeout=10)
    except Exception as exc:
        report["clean"] = False
        report["error"] = f"cannot open sweep DB read-only: {exc}"
        return report
    try:
        tables = {r[0] for r in
                  conn.execute("SELECT name FROM sqlite_master"
                               " WHERE type='table'").fetchall()}
        if "cert_index_misses" not in tables:
            report["clean"] = False
            report["error"] = "cert_index_misses table missing"
            return report
        cols = {r[1] for r in
                conn.execute("PRAGMA table_info(cert_index_misses)").fetchall()}
        if "policy_numbers" not in cols:
            # Sweep predates the fix: nothing recorded the policy signal,
            # so the class cannot be detected. Say so, loudly.
            report["note"] = ("miss ledger predates the policy_numbers "
                              "column — the EPHE class cannot be detected "
                              "until the sweep records it")
            return report
        rows = conn.execute(
            "SELECT raw_name, gmail_id, last_seen_at, occurrences,"
            " policy_numbers FROM cert_index_misses"
            " WHERE resolved_applicant_id IS NULL"
        ).fetchall()
    except Exception as exc:
        report["clean"] = False
        report["error"] = f"cannot read miss ledger: {exc}"
        return report
    finally:
        conn.close()

    cutoff = _utcnow() - timedelta(seconds=WINDOW_S)
    for raw_name, gmail_id, last_seen_at, occurrences, policy_numbers in rows:
        seen = _parse_iso(last_seen_at or "")
        if seen is None or seen < cutoff:
            continue
        report["misses_checked"] += 1
        for pol_key in (policy_numbers or "").split("|"):
            pol_key = pol_key.strip()
            if not pol_key:
                continue
            applicants = index_policies.get(pol_key, [])
            if applicants:
                report["clean"] = False
                report["findings"].append({
                    "raw_name": raw_name,
                    "gmail_id": gmail_id,
                    "last_seen_at": last_seen_at,
                    "occurrences": occurrences,
                    "policy_number": pol_key,
                    "indexed_applicants": sorted(applicants),
                })
                break  # one routing per miss is enough to prove the class
    return report


# ---------------------------------------------------------------------------
# Alert (deduped Chat alert when red)
# ---------------------------------------------------------------------------

def _alert_state_path(directory: str) -> str:
    return os.path.join(directory or data_dir(), ALERT_STATE_FILENAME)


def _read_alert_state(directory: str) -> dict[str, Any]:
    try:
        with open(_alert_state_path(directory), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_alert_state(directory: str, state: dict[str, Any]) -> None:
    try:
        with open(_alert_state_path(directory), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception:
        pass  # alert bookkeeping must never break the probe


def render_report(report: dict[str, Any]) -> str:
    """Plain-English one-paragraph-or-list summary for Chat or stdout."""
    if report.get("error"):
        return (f"Certificate miss-watch: PROBE ERROR (checked "
                f"{report['at']}). {report['error']}. The EPHE miss class "
                f"cannot be verified right now — investigate the sweep DB "
                f"and applicant index.")
    if report.get("note"):
        return (f"Certificate miss-watch: CANNOT DETECT "
                f"(checked {report['at']}). {report['note']}.")
    if report["clean"]:
        return (f"Certificate miss-watch: clean (checked {report['at']}). "
                f"{report['misses_checked']} unresolved miss(es) in the "
                f"last 24h, none with a policy number found in the "
                f"applicant index.")
    lines = [
        f"Certificate miss-watch: {len(report['findings'])} held request(s) "
        f"whose policy number IS in the applicant index "
        f"(checked {report['at']}) — the EPHE miss class is back:",
    ]
    for f in report["findings"]:
        applicants = ", ".join(str(a) for a in f["indexed_applicants"])
        lines.append(
            f"\u2022 {f['raw_name']!r} (held {f['occurrences']}x, last seen "
            f"{f['last_seen_at']}): policy {f['policy_number']} belongs to "
            f"applicant {applicants} — the request should have matched, "
            f"not held.")
    return "\n".join(lines)


def render_recovery(report: dict[str, Any]) -> str:
    return (f"Certificate miss-watch: clean again (checked {report['at']}). "
            f"{report['misses_checked']} unresolved miss(es) in the last "
            f"24h, none with a policy number in the applicant index.")


def _post(poster: Any, text: str) -> dict[str, Any]:
    if poster is None:
        try:
            from .cert_notify import _default_chat_poster
            poster = _default_chat_poster()
        except Exception as exc:
            return {"skipped": f"chat identity unavailable: {exc}"}
    try:
        posted = poster(text)
        return {"posted": posted}
    except Exception as exc:
        return {"failed": f"{type(exc).__name__}: {exc}"}


def maybe_alert(report: dict[str, Any], *, directory: str = "",
                poster: Any = None,
                alert_every_s: int = 4 * 3600) -> dict[str, Any]:
    """Send a Chat alert when red (deduped), a recovery note when clean
    returns after red. ``poster`` is a callable(text) -> Any; when None,
    the cert_notify Chat identity is used. Never raises."""
    directory = directory or data_dir()
    state = _read_alert_state(directory)
    now_iso = _utcnow().isoformat(timespec="seconds")
    result: dict[str, Any] = {"alerted": False}

    if report.get("error"):
        # A broken probe is reported, not deduped: every run says so.
        result["post"] = _post(poster, render_report(report))
        result["alerted"] = result["post"].get("posted", False)
        result["state"] = "probe-error"
        return result

    if report["clean"]:
        if state.get("red_since"):
            posted = _post(poster, render_recovery(report))
            result["recovery"] = posted
            result["alerted"] = posted.get("posted", False)
        _write_alert_state(directory, {})
        result["state"] = "green"
        return result

    reason_key = "|".join(sorted(
        f"{f['raw_name']}:{f['policy_number']}" for f in report["findings"]))
    last_alert_at = state.get("last_alert_at")
    try:
        last_alert = (datetime.fromisoformat(last_alert_at)
                      if last_alert_at else None)
    except Exception:
        last_alert = None
    if last_alert is not None and last_alert.tzinfo is None:
        last_alert = last_alert.replace(tzinfo=timezone.utc)
    due = (last_alert is None
           or (_utcnow() - last_alert).total_seconds() >= alert_every_s
           or state.get("reason_key") != reason_key)
    if not due:
        result["state"] = "red-quiet"
        return result

    posted = _post(poster, render_report(report))
    result["alerted"] = posted.get("posted", False)
    result["post"] = posted
    _write_alert_state(directory, {
        "red_since": state.get("red_since") or now_iso,
        "last_alert_at": now_iso,
        "reason_key": reason_key,
        "findings": len(report["findings"]),
    })
    result["state"] = "red-alerted"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect held certificate requests whose policy number "
                    "exists in the applicant index (the EPHE miss class).")
    parser.add_argument("--alert", action="store_true",
                        help="post to the ROBIE health Chat when red")
    parser.add_argument("--data-dir", default="",
                        help="sweep data dir (default: CERT_SWEEP_DATA_DIR)")
    parser.add_argument("--index-csv", default="",
                        help="applicant index CSV "
                             "(default: CERT_APPLICANT_INDEX_PATH)")
    args = parser.parse_args(argv)

    report = check_misses(directory=args.data_dir, csv_path=args.index_csv)
    print(render_report(report))
    if args.alert:
        outcome = maybe_alert(report, directory=args.data_dir)
        print(f"alert: {outcome.get('state')}"
              + (" (posted)" if outcome.get("alerted") else ""))
    if report.get("error"):
        return 2
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
