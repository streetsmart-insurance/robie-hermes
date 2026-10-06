# Splice workflow labels

Nine EZLynx activity labels select the Splice call scripts. Create these
labels in EZLynx exactly (matching ignores case, spaces, hyphens, and
underscores):

| EZLynx activity label | Script |
| --- | --- |
| Robie Audit Not Complete | Audit Not Complete |
| Robie Recommendations Follow-Up | Recommendations Follow-Up |
| Robie Returned Mail | Returned Mail |
| Robie E-signature Follow-Up | E-signature Follow-Up |
| Robie Additional Information Follow-Up | Additional Information Follow-Up |
| Robie Sales Center Reviewed Status | Sales Center Reviewed Status |
| Robie Winback Campaign | Winback Campaign |
| Robie Renewal Reach Out | Renewal Reach Out |
| Robie Unresponsive | Unresponsive |

"Robie Call" and "Robie Lead Follow Up" stay the two live labels and do
not read the Splice flag. Robie Call now dials only a number typed in
the task. See "Which phone is dialed". A shorter name such as
"Robie audit" is not one of these labels and does not dial.

## Where they dial

On the Test server (`hermes-test-01`, or `ROBIE_ENV=TEST`) all nine are
on. Every Test dial is forced to Jake's cell (`robie-test-jake-cell`).
`ROBIE_PHONE_REAL_CLIENTS=1` cannot post a client number from Test.

On Production the nine stay off until `ROBIE_SPLICE_WORKFLOWS_LIVE=1`.
That one flag is the Production switch for all nine. The two live labels
do not read it.

Tasks that already existed when the nine turned on are baselined and
never dialed. A task created after that moment can dial. The kill switch,
one-call-per-note-per-day rule, and assigned-producer check still apply.

## Test client

Proofs for these nine labels run on Jake Ferrara's own EZLynx client
account, applicant `25486692`, not Buster Brown and not ROBIE Test LLC.
`ROBIE_SPLICE_TEST_APPLICANT_ID` names that account. The Test task-intake
unit sets it to `25486692`. Change the setting if the proof account
changes. Do not hardcode a different client in a test.

These values are refused:

- `26356199` — Buster Brown
- `220250093` — ROBIE Test LLC

If the setting is missing, or the task is on a different account, Robie
does not dial. The note asks for Jake's account. The outbound number is
still only his test cell.

`ROBIE_ENV=TEST` still requires `ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS` for
the whole intake. That list is task ids, not a per-label allowlist. Put
the proof task ids in it. The Test unit sets
`ROBIE_EZLYNX_WRITE_APPLICANT_IDS=25486692`, so the outcome note can be
filed only on Jake's applicant. It does not open every client.

## What the task does not carry

The spoken script uses the client's first name (Account Name) and the
assigned producer. It does not read the task note for:

- which audit, or the payroll figures
- the recommendation text
- which mail was returned
- which document needs a signature
- what additional information was requested
- a quote number, policy number, or prior carrier
- the renewal date

## Which phone is dialed

The nine Splice labels dial only the client's phone on file. A phone
number or a policy number typed in the task note is ignored. It does not
override the phone on file, and it does not stop the call as an
ambiguous number.

Robie Call dials only a phone number a person typed in the task. It
never falls back to the client's phone on file. If the task has no
usable typed number, or the number might be a policy, claim, or quote
number, Robie does not call. The note asks for a new Robie Call task.
When the number was unclear, it says to write "call at" or "phone"
before the number. Intake does not open
the same task again once it has a job, so editing that task does not
cause another dial.

Robie Lead Follow Up is unchanged: a typed number first, then the phone
on file.

On Test, none of those numbers are posted. The outbound leg is Jake's
cell from `robie-test-jake-cell`. The applicant phone lookup is not
called. Press 1 still transfers to the producer's direct dial from the
staff directory, when one exists. Sales Center Reviewed Status and
Winback Campaign are marketing scripts and still require a recorded
opt-in. Renewal Reach Out and Unresponsive have no text message.

## Test intake unit

`deploy/systemd/robie-task-intake-test.service` and its timer run on
hermes-test-01 as `streetsmart-hermes-test`, with code under
`/opt/streetsmart-hermes-test`. `scripts/install-robie-task-intake.sh
--install-test` installs that pair only when the host is
`hermes-test-01`, from
`/opt/streetsmart-hermes-test/releases/current` unless
`ROBIE_RELEASE_ROOT` is set. It does not change the Production units.
`--rollback` removes the Test units as well as the Production units.
`ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS` is empty until an operator
fills the proof task ids. The unit loads the existing Test files
`/etc/streetsmart-hermes-test/robie-message-runtime.env` (EZLynx
secret references) and
`/etc/streetsmart-hermes-test/robie-accountability.env` (the Gmail
account for the task report). Applicant `25486692` lives on the
Production EZLynx tenant, so the unit sets
`ROBIE_EZLYNX_API_PROD_SECRET` to `ezlynx-api-prod` and
`ROBIE_EZLYNX_DISCUSSION_API=live`. Test intake then reads that
Production secret instead of the UAT secret. Outcome notes use
`ROBIE_EZLYNX_WRITE_APPLICANT_IDS=25486692` and do not set an
all-clients write scope, because this box talks to the Production
tenant. The Test service account needs `secretAccessor`
on `robie-test-jake-cell` and `bland-dispatcher-kill-switch`. The Bland
key it reads is `robie-test-bland-api-key`. Do not grant those roles
from this repo.
