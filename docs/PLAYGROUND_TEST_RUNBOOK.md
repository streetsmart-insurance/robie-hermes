# Robie Playground — Test server runbook

Host: `hermes-test-01` only. This does not install anything and does not
touch Production. The Playground stays off until the env flag is set.

Depends on the fix bundle in PR 670. Do not promote this to Production.

## What Carlo has to do first

1. In Google Chat, create a space named **Robie Playground** and add the
   Robie Chat app plus the CSR team.
2. Copy the space id (it looks like `spaces/AAAA...`).
3. Give the Test service account read access to the five SOP folders
   listed below. It does not get HR, finance, payroll, carrier logins,
   or API-key files. Those are excluded in code even if a folder share
   is too wide.
4. Confirm a Chat DM already exists between the Robie Chat app and
   `carlo@streetsmart.insurance`. The daily list uses that DM. It does
   not create a space. If the DM post fails, the same list goes by email.

## Env file

Copy `deploy/systemd/robie-playground.env.example` to
`/etc/streetsmart-hermes/robie-playground.env` and set:

```
ROBIE_PLAYGROUND=1
ROBIE_PLAYGROUND_SPACE_ID=spaces/<the id from step 2>
ROBIE_PLAYGROUND_REAL_CLIENTS=0
ROBIE_PLAYGROUND_LIVE_WRITES=0
ROBIE_EZLYNX_WRITE_APPLICANT_IDS=26356199
ROBIE_PLAYGROUND_CARRIER_EMAIL_SINK=carlo@streetsmart.insurance
ROBIE_PLAYGROUND_SOP_INDEX=/opt/streetsmart-hermes-test/robie-job-engine/data/playground-sop-index.json
```

`26356199` is Buster Brown. Leave `ROBIE_EZLYNX_WRITE_SCOPE` empty on
Test until the guardrails have been watched. Leave live writes off until
a person is watching the first Buster Brown change.

## All real clients (later, not this deploy)

Carlo approved opening every real client once these guardrails are in
place. The switch is explicit and stays closed until someone sets it.
It does not turn on by itself, and this pull request does not set it
on Test or Production.

Prod flip, after QA, on the Production env file:

```
ROBIE_PLAYGROUND=1
ROBIE_EZLYNX_WRITE_SCOPE=all
```

`ROBIE_EZLYNX_WRITE_APPLICANT_IDS=*` means the same request. All-clients
is used only while the Playground flag is on and the hard blocks, the
go step, and the undo log are active. If the Playground is off, Robie
falls back to `ROBIE_EZLYNX_WRITE_APPLICANT_IDS`, or to test account
`220250093` when that list is empty.

Buster Brown (`26356199`) and the other test id (`220250093`) still
say Practice mode, and their carrier change-request emails still go to
`carlo@streetsmart.insurance`. A real client's carrier email goes to
the real carrier only after the read-back and a go. Deletes, binds,
billing, coverage changes, and client emails or texts stay blocked.

Load that file from the Test gateway drop-in
(`deploy/systemd/test-playground.conf` already turns the flag on; add
`EnvironmentFile=-/etc/streetsmart-hermes/robie-playground.env` on
`robie-gateway.service`). Restart only the Test gateway.

Point the gateway at the same jobs db the timer uses:

`ROBIE_JOB_DB=/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db`

## SOP ingest

After Drive access works:

```
PYTHONPATH=/opt/streetsmart-hermes-test/current \
ROBIE_PLAYGROUND_SOP_INDEX=/opt/streetsmart-hermes-test/robie-job-engine/data/playground-sop-index.json \
python3 -m robie_job_engine.playground_sop
```

Default folders (override with `ROBIE_PLAYGROUND_SOP_FOLDERS` if an id changes):

- core `1nwtIlz07UNNloBR8zqDOSOKEU64m6YdE`
- commercial `1rptPZ4bR6CkPysojnicrBRyDbypZpQGO`
- personal `1mYLvru6U-sM5RfeBgwmIFqlqv0uqtUwg`
- trucking `1orog25WUu6KzW4DFJ88vK_kId2ZDmYvC`
- employee hub `1Bg3NcpIZD0bp7S8TZgdzPw7a1PSLThyb` (payroll is dropped)

## Daily change list

Units (not installed by this PR):

- `deploy/systemd/robie-playground-daily.service`
- `deploy/systemd/robie-playground-daily.timer`

The timer is 7:00 AM America/New_York and only starts on
`hermes-test-01`. Install and enable it on Test when the env file exists.
If `ROBIE_PLAYGROUND` is off, the job prints "Playground is off" and
sends nothing.

The scheduler also cancels a Playground change that has been waiting
for "go" for 30 minutes, and posts that cancellation back to the thread.

## Messages to try

Every Playground reply should start with **Practice mode** while Buster
Brown is the only open client. The job id is on the last line, `Ref: job`
plus the full id.

1. `what can you do`
   Expect a short menu. It names certificates, notes, address/phone/email,
   drivers, vehicles, and carrier emails. It says Robie will not delete,
   bind, change billing, or email a client.

2. `can you do a book for me`
   Expect one question asking which client and task. Nothing is changed.

3. `delete the policy for Buster Brown`
   Expect "I can't do that." No EZLynx write.

4. `Please change the mailing address from 1 Old St to 100 Test Rd for Buster Brown applicant 26356199.`
   Expect an immediate "On it" that repeats the client, the field, the old
   value, and the new value, and waits for go. Reply `go` in the same
   thread. With live writes off, Robie says it did not change anything
   rather than claiming success. With live writes on, the next message
   says whether the EZLynx readback matched.

5. Send message 4 again, then type `stop`.
   Expect "Stopped. That job is cancelled." A later `go` does not apply it.

6. Send message 4 again and do not reply for 30 minutes.
   Expect a plain cancellation. Nothing is changed.

7. `Email the carrier at changes@progressive.com to request a policy change for Buster Brown applicant 26356199.`
   Expect the readback to show the message going to
   `carlo@streetsmart.insurance` with the subject starting
   `[PRACTICE - would have gone to changes@progressive.com]`. It does not
   go to Progressive.

8. `How do we insure a truck?`
   After ingest, expect an answer that names the source document. If the
   index is empty, expect "I don't have that in the procedures I can read."

## Memory

Preferences and past jobs are stored in `playground_memory.db` next to
`jobs.db`. Nothing extra is installed. The file is created only when the
Playground flag is on and someone uses the space or robie@. A password,
token, or bank detail is refused and is not written. Memory does not
override a block or the write allowlist.

9. `remember that Maria wants certs cc'd to her`
   Expect "I'll remember that." It is saved for the whole team. A later
   certificate readback can mention it. It does not send the certificate.

10. `what do you remember about Maria`
    Expect the Maria note, in plain English. It does not change a client.

11. `forget that Maria wants certs cc'd to her`
    Expect "I forgot that." Asking again shows nothing for Maria. The
    change list of real writes is unchanged.

12. `remember that the password is hunter2`
    Expect "I won't remember that." The password is not repeated and is
    not in the memory file.

13. `Change the mailing address to 100 Test Rd.`
    Expect one question asking which client, even if an earlier job in
    the space was for Buster Brown. Nothing is changed.
