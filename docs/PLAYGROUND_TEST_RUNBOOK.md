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
ROBIE_PLAYGROUND_TEAM_MEMBERS=Commercial=Maria;Personal=;Trucking=
ROBIE_PLAYGROUND_MEMORY_BUCKET=
ROBIE_PLAYGROUND_MEMORY_BACKUP_KEEP_DAYS=14
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
`jobs.db` on that server. Test and Production do not share the file.
The gateway user owns it. The code sets mode `0640`. The file is created
only when the Playground flag is on and someone uses the space or robie@.

A row is one of four scopes: person, team, client, or agency. The team
is Commercial, Personal, or Trucking, from `ROBIE_PLAYGROUND_TEAM_MEMBERS`.
Put the exact Chat display name or email address, the same value Robie
sees as the sender. A person preference is visible only to that person.
A team preference is visible only to that team. Agency preferences are
visible to everyone. Client notes and the last few jobs for a client are
visible to the whole agency, and only when the request names that client.
Lookup is exact. There is no vector search.

A password, token, or bank detail is refused and is not written. A
Social Security number or ITIN is not remembered either, including
`123-45-6789`, `123 45 6789`, nine digits labeled SSN or social, and
ITINs. If that number is the whole note, Robie refuses it and does not
save a blanked copy. If the rest of the note is a real preference, Robie
saves only that rest, with `[SSN removed]` or `[ITIN removed]`, and says
so. Policy numbers and phone numbers are not treated as Social Security
numbers. Memory does not override a block or the write allowlist. If the
team map does not name the sender, "remember for the team" asks which
team instead of guessing.

9. `remember that Maria wants certs cc'd to her`
   With Maria and the sender both mapped to Commercial, expect
   "I'll remember that." It is saved for the Commercial team. Someone on
   Personal or Trucking does not see it. It does not send a certificate.

10. `what do you remember about Maria`
    From a Commercial teammate, expect the Maria note. From another
    team, expect nothing for that note. It does not change a client.

11. `forget that Maria wants certs cc'd to her`
    A Commercial teammate can forget it. Expect "I forgot that." Asking
    again shows nothing for Maria. The change list of real writes is
    unchanged. Someone on another team cannot forget it.

12. `remember that the password is hunter2`
    Expect "I won't remember that." The password is not repeated and is
    not in the memory file.

13. `remember that I like short notes`
    Expect it saved for you. A different person asking
    `what do you remember` does not see it.

14. `Change the mailing address to 100 Test Rd.`
    Expect one question asking which client, even if an earlier job in
    the space was for Buster Brown. Nothing is changed.

15. `remember that his SSN is 123-45-6789`
    Expect "Social Security numbers can't be remembered." The number is
    not repeated and is not in the memory file.

16. `remember that Maria wants certs cc'd to her and her SSN is 123-45-6789`
    Expect "I'll remember the rest." The saved note says `[SSN removed]`
    and the reply says the Social Security number was taken out. The
    digits are not stored.

## Memory backup

This pull request does not install the timer and does not create a
bucket. On Test the unit files are
`deploy/systemd/robie-playground-memory-backup.service` and `.timer`.
They run as `streetsmart-hermes-test` at 2:15 AM America/New_York.
`ConditionHost` is `hermes-test-01` only.

Leave `ROBIE_PLAYGROUND_MEMORY_BUCKET` empty until the private bucket
exists. The timer then prints a skip line and uploads nothing.

When the bucket is ready, set:

```
ROBIE_PLAYGROUND_MEMORY_BUCKET=<private test bucket name>
ROBIE_PLAYGROUND_MEMORY_BACKUP_KEEP_DAYS=14
ROBIE_PLAYGROUND_MEMORY_BACKUP_PREFIX=playground-memory
```

Auth is the VM's attached service account, with
`roles/storage.objectAdmin` on that bucket only. Do not create a key
file. Do not set `GOOGLE_APPLICATION_CREDENTIALS` for this timer.
Do not give the account project-wide storage admin.

The job writes a consistent snapshot with sqlite's backup API and
uploads `playground-memory/<hostname>/<UTC stamp>.sqlite`. Snapshots
older than the keep-days setting are deleted after a successful upload.

Restore, on the same host, as the service user. This replaces the live
memory file with that snapshot:

```
sudo -u streetsmart-hermes-test \
  /opt/streetsmart-hermes-test/venv/bin/python \
  -m robie_job_engine.playground_memory_backup \
  --db /opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db \
  restore \
  --object playground-memory/<hostname>/<stamp>.sqlite \
  --confirm
```

List snapshot names with the same command and `list` instead of
`restore`. Without `--confirm`, restore refuses and changes nothing.

Production, later, not this pull request. Different server, different
file, different bucket. Do not point Test at the Production bucket.

```
User=streetsmart-hermes
Group=streetsmart-hermes
ROBIE_JOB_DB=/opt/streetsmart-hermes/robie-job-engine/data/jobs.db
```

The memory file is
`/opt/streetsmart-hermes/robie-job-engine/data/playground_memory.db`,
mode `0640`, owner `streetsmart-hermes`. The bucket setting is:

```
ROBIE_PLAYGROUND_MEMORY_BUCKET=<private production bucket name>
ROBIE_PLAYGROUND_MEMORY_BACKUP_KEEP_DAYS=14
```

Same attached-service-account rule, bucket-scoped only, no key file.
Restore uses the Production venv, user, and db path, and
`--confirm`. Do not install that timer from this pull request.
