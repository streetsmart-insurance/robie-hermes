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

"Robie Call" and "Robie Lead Follow Up" are unchanged. A shorter name such
as "Robie audit" is not one of these labels and does not dial.

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
account, not Buster Brown and not ROBIE Test LLC. Set
`ROBIE_SPLICE_TEST_APPLICANT_ID` on the Test host to Jake's applicant id.
The id is not stored in this repo.

These values are refused:

- `26356199` — Buster Brown
- `220250093` — ROBIE Test LLC

If the setting is missing, or the task is on a different account, Robie
does not dial. The note asks for Jake's account. The outbound number is
still only his test cell.

`ROBIE_ENV=TEST` still requires `ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS` for
the whole intake. That list is task ids, not a per-label allowlist. Put
the proof task ids in it. Write scope for the outcome note has to include
Jake's applicant id.

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

The number Robie dials comes from the applicant phone record. Press 1
transfers to the producer's direct dial from the staff directory, when
one exists. Neither value is a column on the task. Sales Center Reviewed
Status and Winback Campaign are marketing scripts and still require a
recorded opt-in. Renewal Reach Out and Unresponsive have no text message.
