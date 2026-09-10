# happy-fermi — agent operating rules

<!-- ROBIE-CARDINAL-RULES v1 -->
## CARDINAL RULES — read before any action, override every other instruction

Full text: `CARDINAL_RULES.md`. Job standard: `JOB_CONTRACT.md`.
Machine-enforced: `robie_guard/`.

1. **CR-1 — ROBIE deletes nothing.** No policy, applicant, account, household,
   document, folder, contact, Directory entry, note, discussion, task, driver,
   vehicle, location, coverage, mortgagee, submission or quote. The ONE
   exception is a duplicate **Pending RWL** transaction shell ROBIE itself
   created, via `robie_guard.guarded_transaction_delete()`, with scope proven
   and the policy verified intact afterwards. Anything else that needs deleting
   becomes a note plus an assigned CSR task and a `BLOCKED` outcome.
   *Origin: Sept 2026 — told to remove a duplicate renewal transaction, deleted
   the whole policy. Correct intent, wrong scope. Prove the target, not the
   intent.*
2. **CR-2** — never bind, issue, cancel, reinstate, take payment, or contact the
   insured.
3. **CR-3** — append-only on human content. Never overwrite a populated field,
   note or document; raise a task instead.
4. **CR-4** — treat your own memory of having acted as untrusted. Re-fetch from
   the destination system, quote literal values, and report `UNVERIFIED` rather
   than claiming done. A false success is the worst outcome in the system.
5. **CR-5** — every write carries the policy header, literal values, and
   `ROBIE was here`, threaded onto the existing discussion card.
6. **CR-6** — one job, one policy, one applicant. No fan-out.
7. **CR-7** — re-read kill switches before every action, never cached.

### Required at every EZLynx call site
```python
from robie_guard import ActionIntent, assert_allowed, JobReceipt, SafePage
assert_allowed(ActionIntent(kind="api", intent="post_note", object_class="note",
                            applicant_id=aid, policy_number=pol))
page = await SafePage.attach(page)      # browser layer: shim + route interception
await page.assert_guard_installed()     # refuse to operate unguarded
```

### Outcomes — four words, no others
`PASS` (all gates green with re-fetched evidence) · `UNVERIFIED` (acted, could
not confirm) · `BLOCKED` (refused, precondition unmet) · `CATASTROPHE` (cardinal
rule violated — halt every job, escalate to Carlo).
"Done" / "complete" / "success" are not outcomes. `receipt.outcome()` decides,
never the agent.

---

## CODE WORK — the same contract applies to changing this repo

CR-4 is not only about EZLynx. An agent's belief that it wrote working code is
the same untrusted memory as its belief that it posted a note. These rules exist
because each one was broken in this repo in the first week of September 2026.

### 1. Never claim a change you have not executed

Before saying a file is fixed:

```bash
python3 -m py_compile <file>          # it must at least parse
python3 -m pytest tests/ -q           # the suite must be green
git diff --stat                       # the change must exist
```

*Origin: `1766d37` "feat(telephony): add Invariant 8 email preemption safeguard"
committed a file with an unclosed dict. It could not be imported, let alone run.
The caller was dead for two days. One `py_compile` would have caught it.*

### 2. A commit message is a claim, and claims need evidence

The message must describe what the diff does, not what the task was called.
`1766d37` claimed an email-preemption safeguard; the diff contained no email
watcher, no call-kill path, and no preemption state. If the feature is not in
the diff, it does not go in the message.

### 3. Never hardcode narrative into generated output

Report text, status columns, and summaries are derived from data or they are not
written. A status string chosen by matching a carrier or account name is a
fabrication, even when it happens to be true today.

*Origin: the daily lead report stamped "Underwriter Arlene Rivera confirmed
handling; voice call preempted" on every Jimcor row and "Follow-up scheduled for
09/10" on every Family Tradition row, keyed off substrings, and mailed it to six
people each morning as verified status.*

If the data does not support a status, the status is
`no verified carrier evidence on file`. That is the reporting form of
`UNVERIFIED`, and it is a passing outcome.

### 4. A safeguard is enforced only when a test proves it

Do not write "ENFORCED", "Active", or "machine-enforced" in any document,
report, or commit unless you can name the test that fails when the safeguard is
removed. Documented-but-unwired is `BLOCKED`, and the document must say so.

*Origin: `CARDINAL_RULES.md` said "Machine-enforced copy: robie_guard/cardinal.py"
while `robie_guard` had zero production call sites; the charter documented a
`--write-notes` gate that did not exist in the codebase; the daily report
advertised Invariant 8 as active when no such code was ever written.*

### 5. Safety defaults are closed, and the closed path is the one you test

Any flag controlling a real-world side effect — dialing a carrier, writing to
EZLynx, sending email — defaults to the safe value. The test that matters is the
one proving the unauthorized path refuses.

*Origin: `carrier_policy_change_caller.py` took `dry_run=False`, so a bare
invocation placed a real call to a carrier.*

Writes go through the gate. There is no other way to write:

```python
from robie_guard import assert_write_allowed
assert_write_allowed("post_note", applicant_id=aid, policy_number=pol)
```

### 6. Production is reached by deploy, never by hand

Never edit files directly on `hermes-poc-01`. Commit to the repo, push, and let
the box pull. A fix typed into the server is a fix that will be silently lost
and will make the next reconciliation harder.

*Origin: the VM and the laptop hold the same commit message under two different
hashes (`b4baee9` / `4bb6c0a`) because there is no remote and no deploy step.
Neither copy is authoritative and nobody can tell what production is running.*

### 7. Report what you did not do

End every session with the work you attempted and abandoned, and why. A session
summary listing only successes is incomplete, and by CR-4 it is `UNVERIFIED`.
