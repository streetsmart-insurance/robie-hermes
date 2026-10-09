# Wiring the unmatched queue into certificate intake

**Status: NOT APPLIED.** This wiring targets the certificate intake code
that lives on branch `ralph/cert-false-positive-repair` (PR #609).
It must be applied **after PR #609 merges** — the intake modules
(`cert_intake.py`, `cert_verification.py`, `cert_intake_runner.py`) do not
exist on `master`, and the alias work on the #609 branch must not be
disturbed. Applying this early would break the branch.

## What to change

In `robie_job_engine/cert_verification.py`, on top of the merged PR #609
code:

1. Import the queue module at the top of the file:

```python
from .cert_unmatched_queue import enqueue_unmatched
```

2. Add a helper next to `verify_record()`:

```python
def _hold_identity(res: "VerificationResult", reason: str,
                   record: Any) -> "VerificationResult":
    """Hold for identity reasons AND queue the request for human matching.

    The queue write must never break verification: if it fails, the hold
    stands and the failure is recorded in evidence.
    """
    try:
        enqueue_unmatched(
            sender_email=res.requester_email
            or record.facts.requester_email or "",
            subject=record.subject,
            insured_name=res.insured_name or record.facts.insured_name,
            company_name=record.facts.dba,
            policy_numbers=list(res.policy_numbers or []),
            hold_reason=reason,
            strategies_tried=_strategies_tried(record),
            gmail_id=record.gmail_id,
            evidence=list(res.evidence or []),
        )
    except Exception as exc:  # noqa: BLE001 — hold must stand regardless
        res.evidence.append(f"unmatched-queue write failed: {exc}")
    return res.hold(reason)


def _strategies_tried(record: Any) -> list[str]:
    tried = []
    if getattr(record.facts, "requester_email", None):
        tried.append("report_email")
    if getattr(record.facts, "insured_name", None):
        tried.append("report_name")
    if getattr(record, "match", None) is not None:
        tried.append(f"report_match:{record.match.status}")
    if getattr(record.facts, "policy_numbers", None):
        tried.append("ezlynx_policy")
    return tried
```

3. In `verify_record()`, replace the **genuine-request identity holds**
with `_hold_identity(res, reason, record)` — i.e. change
`return res.hold(<reason>)` to `return _hold_identity(res, <reason>, record)`
at exactly these sites:

| # | Site in verify_record() | Reason it queues |
|---|---|---|
| 1 | `conflict = detect_insured_conflict(candidates)` hold | conflicting insured candidates — human must pick |
| 2 | `status == "AMBIGUOUS"` hold | name matches several applicants |
| 3 | MATCHED but policy numbers do not anchor hold | possible wrong-applicant match |
| 4 | `verifier is None` NO_MATCH hold | no report hit, no EZLynx check possible |
| 5 | `len(applicant_ids) > 1` hold | policy anchors to several applicants |
| 6 | final NO_MATCH hold | no report hit and no EZLynx policy anchor |

Do **NOT** queue these holds — they are not genuine identity cases:

- automated-response hold (`ACTION_AUTOREPLY`)
- unclassifiable-message hold (`ACTION_UNKNOWN`) — not a proven request
- PDF-unreadable hold — readability, not identity
- `ACTION_ACK` — not a hold at all (thank-you, no new request)

## Why the queue module takes plain data

`enqueue_unmatched()` deliberately takes strings/lists, not
`VerificationResult`/`IntakeRecord` objects, so the queue, report, and
resolve CLI are unit-testable without the intake branch. The helper above
is the only glue.

## After wiring

- Genuine-but-unmatched requests land in
  `robie_job_engine/data/cert_unmatched_queue.jsonl` (or
  `CERT_UNMATCHED_QUEUE_PATH`).
- Daily report: `python3 -m robie_job_engine.cert_unmatched_cli report`
  (markdown for the human, `--format json` for tooling).
- Human resolves: `resolve <id> --applicant-id <N> --by <name>` writes a
  `strong` sender alias; `--not-our-client` or vendor senders write none.
- The EZLynx task-creation Zap is broken (`no_hook_configured`), so the
  queue file + report is the tasking mechanism until the Zap is repaired.
  Do not gate the queue on the Zap.
