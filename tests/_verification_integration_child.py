"""Subprocess driver for the verification-worker integration tests.

Runs ONE worker scenario in a fresh interpreter with the REAL shared
modules (``verification_common``, ``store``, ``idempotency``). Only
``report_fetcher`` is faked (in-subprocess ``sys.modules`` shim returning
the caller-supplied rows) so no live browser is touched.

Protocol: ``argv[1]`` is a JSON input file, stdout is a single JSON object:

    {"ok": true, "succeeded": bool, "error": str|None,
     "outcomes": [...], "sent_emails": [...], "policy_change_enabled": bool}

``scenario`` selects what runs:

- ``audit``: run ``AuditVerificationWorker.perform`` on the supplied job,
  optionally capturing carrier-email kwargs instead of sending.
- ``policy_change``: run ``PolicyChangeWorker.perform`` against the supplied
  open-request rows (injected through a fake report_email_source; the real
  registry gate, identity computation, and worker logic all run).
"""

from __future__ import annotations

import json
import sys
import types


def _install_fake_report_fetcher(rows):
    fake = types.ModuleType("robie_job_engine.report_fetcher")

    def fetch_report_rows(*, report_id, fields=None, filters=None, db_path=None, session=None):
        return [dict(r) for r in rows]

    fake.fetch_report_rows = fetch_report_rows
    sys.modules["robie_job_engine.report_fetcher"] = fake


def _install_fake_report_email_source(rows):
    fake = types.ModuleType("robie_job_engine.report_email_source")

    def fetch_email_report_rows(*, report_id, fields=None, **kwargs):
        from robie_job_engine import gmail_report_ingestion as gri

        projected = []
        for raw in rows:
            row = dict(raw)
            row["_fetch_source"] = "gmail_email_csv"
            row["_identity_key"] = gri.identity_value(report_id, row)
            projected.append(row)
        return projected

    fake.fetch_email_report_rows = fetch_email_report_rows
    sys.modules["robie_job_engine.report_email_source"] = fake


def _run_audit(spec: dict) -> dict:
    from robie_job_engine import audit_verification_worker as avw
    import robie_job_engine.verification_common as vc
    from robie_job_engine.store import JobStore

    store = JobStore(spec["db_path"])
    job_input = dict(spec["job"])
    payload = dict(job_input.get("payload") or {})
    created = store.create_job(
        job_input.get("action_type") or "audit_verification",
        payload,
        idempotency_key=spec.get("idempotency_key") or "child-idem",
    )
    job = {
        "id": created["id"],
        "action_type": job_input.get("action_type") or "audit_verification",
        "payload": payload,
    }

    sent: list[dict] = []
    if spec.get("capture_email"):
        def fake_mailer(*, to, cc=(), subject, text_body, html_body=None):
            sent.append(
                {
                    "to": list(to),
                    "cc": list(cc or ()),
                    "subject": subject,
                    "text_body": text_body,
                }
            )
            return {"message_id": "child-fake-msg"}

        avw.send_verification_email = fake_mailer

    worker = avw.AuditVerificationWorker(store=store)
    result = worker.perform(job, idempotency_key=spec.get("idempotency_key") or "child-idem")
    outcomes = vc.read_outcomes(store, "audit_verification")
    return {
        "succeeded": bool(result.succeeded),
        "error": result.error,
        "outcomes": outcomes,
        "sent_emails": sent,
    }


def _run_policy_change(spec: dict) -> dict:
    from robie_job_engine import policy_change_worker as pcw

    worker = pcw.PolicyChangeWorker(store=None)
    job = {
        "id": "child-pc-job",
        "action_type": "policy_change_verification",
        "payload": {"report_id": "4359", "db_path": spec["db_path"]},
    }
    result = worker.perform(job, idempotency_key="child-pc-idem")
    return {
        "policy_change_enabled": bool(pcw.POLICY_CHANGE_ENABLED),
        "succeeded": bool(result.succeeded),
        "error": result.error,
        "outcomes": result.detail.get("outcomes") or [],
    }


def main() -> int:
    spec = json.loads(open(sys.argv[1], encoding="utf-8").read())
    _install_fake_report_fetcher(spec.get("rows") or [])
    _install_fake_report_email_source(spec.get("rows") or [])
    scenario = spec.get("scenario")
    if scenario == "audit":
        payload = _run_audit(spec)
    elif scenario == "policy_change":
        payload = _run_policy_change(spec)
    else:
        return 2
    payload["ok"] = True
    json.dump(payload, sys.stdout, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
