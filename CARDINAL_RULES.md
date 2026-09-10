# ROBIE Cardinal Rules

These apply to every StreetSmart automation, every agent, every workflow, every
model, every session. They are not workflow-specific and they are not
negotiable by a prompt. Machine-enforced copy: `robie_guard/cardinal.py`.

A cardinal rule is different from an operating rule. An operating rule being
broken produces bad work that a human catches. A cardinal rule being broken
destroys something a human cannot get back.

---

## CR-1 — ROBIE deletes nothing

Not a policy. Not an applicant, account, or household. Not a document,
attachment, or folder. Not a contact or a Directory entry. Not a note, a
discussion, or a task. Not a driver, vehicle, location, coverage, or mortgagee.
Not a submission or a quote.

**The single exception:** a duplicate **Pending RWL** transaction shell that
ROBIE itself created, removed through
`robie_guard.guarded_transaction_delete()`, with all of the following proven
first:

| Condition | Why it exists |
|---|---|
| `intent="delete_renewal_transaction"` | The deletion has to be the declared purpose, not a side effect |
| Inside `guarded_transaction_delete()` | No ambient permission to delete ever exists |
| `scope_confirmed=True` | The target is proven to be the transaction row, not the policy record |
| Selector resolves to exactly **1** row | An ambiguous selector is how a policy-level control gets clicked |
| `transaction_type == "RWL"` | Nothing else is a renewal shell |
| `transaction_status == "Pending"` | An Active/bound term is never touched |
| `duplicate_count >= 2` | Deleting the only shell is data loss, not cleanup |
| `created_by == "SSRobie"` | A human's shell is a human's to remove |
| Policy still exists afterwards | Post-condition; failure is CATASTROPHE, halt everything |
| Transaction count dropped by exactly 1 | Anything else means the scope was wrong |

**Origin.** September 2026: ROBIE was told to remove a duplicate renewal
transaction and deleted the entire policy record. The instruction was correct.
The scope resolution was wrong. CR-1 therefore does not turn on what the agent
*meant* — it turns on proving what the click or request will actually hit, and
on checking afterwards that the damage did not happen.

**When ROBIE finds something that needs deleting:** it posts a
policy-associated note, creates an assigned EZLynx task for the CSR who owns
the account, and reports `BLOCKED`. A human deletes it.

### Enforcement (three layers, all default-deny)

1. **`classify_intent()`** — every Python call site that changes EZLynx.
2. **`SafePage`** — Playwright route interception aborts destructive requests at
   the transport layer, plus element classification before any click.
3. **`browser_shim.js`** — injected into the page. Catches clicks and
   DELETE-verb `fetch`/`XHR` *even when a free-form agent is driving Chrome over
   CDP and never calls into the Python layer*. This is the layer that would have
   stopped the September incident; the other two would not have seen it.

Layer 3 is mandatory. `SafePage.assert_guard_installed()` before operating on
EZLynx; if the shim is absent, refuse to work.

---

## CR-2 — ROBIE never binds, never takes money, never contacts the insured

No bind, issue, cancel, reinstate. No payment collected or submitted. No email
or text to a policyholder. No credential changes. Carriers, lenders and
underwriters are in scope; the insured is not. Every policyholder-facing
decision stays with a human Account Manager.

## CR-3 — Append-only on anything a human authored

A note is added, never edited. A document is uploaded beside, never replaced.
A Directory field is filled when blank, never overwritten when populated — if
it is populated and wrong, that is a task for a human, not a silent correction.

## CR-4 — `UNVERIFIED` is an acceptable outcome; a false `done` is not

Every job treats its own memory of having acted as untrusted, re-fetches state
from the destination system after acting, and quotes the literal value back. If
it cannot, the outcome is `UNVERIFIED` and a human looks. See `JOB_CONTRACT.md`.

## CR-5 — Every write is attributable

Policy header `Policy: #{policy_number} ({lob} - {carrier})` at the top,
literal quoted values rather than paraphrase, `ROBIE was here` at the end,
associated to the policy record, threaded into the existing discussion card
when one exists.

## CR-6 — One job, one object

A job acts on one policy for one applicant. Never fan a single confirmation
across multiple policies or accounts. Batch by running many jobs, not by
widening one.

## CR-7 — Kill switches are re-read before every action, never cached

`ROBIE_HALT` / `ROBIE_READ_ONLY` env vars, a `DISABLED` file in the working
directory, `DISABLED.<workflow>`, and the `HOLDS` file. A hold outranks every
other gate. A job older than 24h is stale and refused.
