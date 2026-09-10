# The ROBIE Job Contract — the minimum bar

The bar is not "ROBIE does the work correctly." The bar is:

> **Every job either produces evidence it did the work, or says it didn't.
> Nothing in between, and nothing irreversible either way.**

That is what makes the engine relyable-on without being good. A job that
does 40% of the work and reports the other 60% as `UNVERIFIED` is a *passing*
job. A job that does 100% of the work and reports `PASS` without evidence is a
*failing* job, because it costs a human more to trust it than to redo it.

Insurance automation is not hard because the steps are hard. It is hard because
a wrong step is expensive and a false report of a right step is worse. So the
contract targets exactly those two things: cardinal rules stop the expensive
mistakes, the receipt stops the false reports.

---

## 1. Outcome vocabulary — four words, no others

| Outcome | Meaning | Who touches it next |
|---|---|---|
| `PASS` | All applicable gates green, each backed by re-fetched evidence | Spot-check sample only |
| `UNVERIFIED` | Acted, but could not independently confirm the result | A human confirms |
| `BLOCKED` | Refused to act because a precondition was unmet | A human unblocks |
| `CATASTROPHE` | A cardinal rule was violated or damage was detected | Halt all jobs, escalate to Carlo |

"Done", "complete", "success", "finished" are not outcomes. An agent that says
any of them without a receipt is reporting `UNVERIFIED`.

`UNVERIFIED` is not failure and must never be treated as one, or agents learn to
avoid it by guessing. The only punished outcome is a `PASS` that a spot-check
disproves.

---

## 2. The seven gates

Six are identical across all four workflows. Only **G6** changes.

| Gate | Asks | Evidence that counts |
|---|---|---|
| **G1 Subject** | One real record, still active, still in the working window? | Applicant ID + policy number + status + expiration, re-read from EZLynx |
| **G2 Source** | Right artifact, right term, from the carrier? | Document type + term dates quoted from **inside** the PDF, never the filename |
| **G3 Filed** | In the right folder, right label, tied to the policy? | Document library re-listed after upload, the entry quoted back |
| **G4 Noted** | Policy header, literal values, signature, existing card reused? | Discussion re-fetched, the posted note body quoted back |
| **G5 Handoff** | If a human must act, does an assigned task exist? | Task ID + assignee + due date re-read from EZLynx |
| **G6 Write** | The workflow's own write — exactly one, correct scope | Destination re-read, written values quoted back |
| **G7 Reverified** | Was destination state re-fetched *after* the write? | Proof JSON whose fetch timestamp postdates the write timestamp |

A gate that does not apply must be explicitly skipped with a real reason.
`n/a` is refused by the code.

### G6 by workflow

| Workflow | G6 is satisfied when |
|---|---|
| `manual_renewal` | Exactly one pending RWL shell on the correct account; premium matches the offer PDF; writing company set; producer/CSR = Carlo Ferrara; `bind=false`. Never Add Policy from a Quote ID. |
| `audit_verification` | Audit status recorded with the carrier's own figure (return or additional premium) quoted; term-verified to the current audit period. Never a prior-term final. |
| `policy_change` | Three-way match green: original request vs carrier endorsement/dec vs the EZLynx record. Zero exceptions before closing the task; any discrepancy holds it open. |
| `mortgagee_verification` | Mortgagee clause and escrow billing status written from the dec PDF (PDF wins over typed fields); lender confirmation recorded; loan number quoted. |

**This table answers "do the 7 points apply to the other workflows?" — yes,
six of seven apply unchanged. Only the write itself differs.**

---

## 3. What counts as evidence

Enforced in `robie_guard/receipt.py`, not left to judgement.

- **Re-fetched.** Read back from the destination system *after* the write.
- **Literal.** The actual string or figure, not a paraphrase. `"premium
  $12,480.00"`, not `"premium matched"`.
- **Attributed.** A named source such as `ezlynx_api:get_applicant_policies`.
  Sources named `agent`, `memory`, `self`, or `assumed` are rejected by the
  code — the agent's recollection is not evidence.
- **Fresh.** For G7, a `refetched_at` that predates the write is rejected as
  stale.

```python
receipt = JobReceipt(job_id=job_id, workflow="manual_renewal",
                     applicant_id=applicant_id, policy_number=policy_number)
receipt.gate("G1_SUBJECT", f"{policy_number} Active exp {exp_date}",
             source="ezlynx_api:get_applicant_policies")
...
receipt.mark_acted()
policies = client.get_applicant_policies(applicant_id)      # fresh read
receipt.gate("G7_REVERIFIED", f"pending RWL x{n}, premium {premium}",
             source="ezlynx_api:get_applicant_policies")
outcome, problems = receipt.outcome()     # the ONLY thing allowed to say PASS
receipt.write("data/renewal_proofs/")
```

The agent never sets the outcome. `receipt.outcome()` computes it. That single
inversion is most of the value in this document.

---

## 4. Preconditions every job checks before acting

1. Kill switches re-read (CR-7) — env, `DISABLED`, `DISABLED.<workflow>`, `HOLDS`.
2. Job is younger than 24h, else stale and refused.
3. The guard shim is present in the page (`assert_guard_installed()`).
4. Idempotency: has this job already run for this policy? Re-run means
   **verify**, never re-write. *This is what prevents the duplicate shell that
   started the whole deletion problem — prevention beats a permitted deletion.*
5. Nothing about the subject is on `HOLDS`, and the work is not already done
   out-of-band by a teammate.

---

## 5. What a job may never do

Straight from the cardinal rules: no deletion (one narrow exception), no bind,
no payment, no contact with the insured, no overwrite of human content, no
multi-policy fan-out, no cached kill switch. A job that wants any of these
reports `BLOCKED` with an assigned task naming what it wanted and why.

---

## 6. Graduation — when a workflow leaves hand-training

A workflow moves from Antigravity hand-training into the bounded engine when:

- **20–50 consecutive real cases** with zero false-`PASS` claims
- every run carries a receipt whose gates hold re-fetched evidence
- a **spot-check sample** of those receipts is confirmed accurate by Carlo or Jake
- the workflow **self-flags** `UNVERIFIED` / `BLOCKED` correctly when it cannot
  confirm — a streak with no `UNVERIFIED` at all is suspicious, not excellent
- zero `CATASTROPHE` outcomes, ever

Until then the workflow runs, produces receipts, and a human reviews every one.
The streak counter is the graduation signal, not anyone's confidence.

---

## 7. The daily report

One line per job, outcome first, so the queue can be read in a minute:

```
[PASS]       manual_renewal  GAT0003099-01  premium $12,480.00 / 1 pending RWL
[UNVERIFIED] manual_renewal  CM92DC00437-001  G7: discussion re-fetch returned 0 notes
[BLOCKED]    audit_verif.    R2WC771037  duplicate pending shell -- task 88213 -> Maria
[PASS]       policy_change   FINFR17078371  3-way match 0 exceptions
```

`CATASTROPHE` never waits for the daily report. It halts the queue and pages
Carlo immediately.
