# Carrier statement email acquisition candidate

Stacked on the existing id-based intake draft (#782), which depends on the
parser/provenance draft (#781). Those branches are unchanged. This candidate
adds a missing source-acquisition piece; it does not replace their review
contracts or certify their release.

## Behavior

The operator supplies a private JSON scope: exact entity and mailbox, approved
mailbox list/domain, existing delegated service-account reference, reviewed
Gmail query, timezone-aware received_start/received_end boundaries, and optional
max_pages/max_messages/max_raw_bytes limits. No credentials are accepted.

`python -m carrier_statements.mail_acquisition --config /private/scope.json
--store /private/source-ledger --live-read` uses the existing keyless Gmail
factory with gmail.readonly. The config file must be mode 600; the store and
its blobs/runs folders must be mode 700 and canonical absolute paths without
symlink components. A missing opt-in does not contact Gmail.

It checks exact mailbox identity, exhausts Gmail search pages, fetches exact
message IDs in raw format, and verifies the returned ID, thread and received
window. Each original RFC822 email and decoded attachment is stored privately
under its SHA-256. Original filenames are metadata only. Documents are never
opened, executed, or treated as instructions. It follows no email links and
does not mark messages read, archive, delete, label or send.

The ledger keeps immutable started/finished run journals, source observations,
retrieval times and original hashes. Same bytes reuse a verified blob; changed
bytes preserve both versions. A prior corrupt or unsafe blob is refused.
Writes commit a fully flushed object before exposing its final name. A crash
between journals leaves an unfinished run, not a fabricated complete result.
Finished-journal failure does not return success.

An API/storage error, wrong identity/ID/thread, missing/out-of-window timestamp,
page-token loop, repeated page message, limit, malformed MIME/base64, or nested
attachment requiring review produces UNVERIFIED. Earlier originals remain
preserved. Provider error strings and message identifiers/content do not appear
in stdout. Detailed originals, query, addresses and observations stay private.

## Evidence limits

QUERY_ACQUIRED_REVIEW_ONLY means only that this exact query/window was exhausted
without a detected error. Gmail pagination is not a transactionally frozen
mailbox snapshot. Use an overlapping subsequent run and independent source
comparison to account for messages changing during acquisition. No result
establishes all statements received, a carrier's monthly obligation, a printed
period, a statement classification, financial reconciliation or bank clearing.
An empty query is not a clean carrier-month. Mailbox arrival date and filename
are never substituted for the printed statement period.

Before passing sources to `review_contract.validate_source`, independently
establish actual carrier/agency/account/type/currency/printed-period provenance
and the AppSheet document ID mapping. The acquisition manifest deliberately
leaves carrier_id and printed_period unknown. Existing stored/portal sources
must be checked before treating an email absence as a missing statement.

## Verification and release

Focused offline tests cover paging/exhaustion, duplicates, revisions, corruption,
malformed responses, limits, identity and boundary failures, retained partial
evidence, redaction and private storage. Existing intake/review tests and all
seven original synthetic parser cases remain applicable. Required hosted CI,
immutable release, approved Test execution, independent QA and exact-version
release approval remain separate gates. This candidate adds no timer or unit.

No live mailbox acquisition run or carrier-month coverage is claimed by a code
test. The previously verified runtime mailbox-access diagnostic is separate
evidence and does not prove this candidate installed or tested on a VM.

## Sender setup finding

The existing verification mailer reuses delegated Gmail and a Sent duplicate
guard. That guard currently documents fail-open behavior on lookup errors.
Before missing-statement sending can be enabled, wrap/reuse that path with a
durable exact-request journal and fail-closed preconditions: verified carrier
contact/thread, fresh complete source coverage, no satisfied/already-chased
request, no newer reply/statement, one active writer, and exact Sent readback.
An ambiguous send outcome must remain pending verification, never blind resend.

Sending scope and runtime signing permission are separate from the read-only
acquisition identity. No sender grant, per-message approval loop, financial
effect, new payment route, schedule or Production change is introduced here.
Routine sending can run under the configured scope after the lane is released.

## Initial request planner

`request_plan.plan_missing_request` produces a private draft only. It requires
exact entity/agency/carrier/account/period, a verified monthly obligation,
verified sender/contact/thread, fresh referenced source evidence and explicit
negative checks for already received/chased/unknown send/staff work/new replies.
Unchecked stores, portals or partial email searches hold the draft. Invoice-only
carriers are not monthly-source gaps. The duplicate key includes the exact
entity/carrier/account/period; changing a contact does not reset it. No API send
or follow-up timing is inferred. Input attestations do not authenticate release
authority. Twelve synthetic request-policy tests extend the focused total to 82.

The inherited stacked branch had a fixed-date freshness fixture that prevented
the broad CI gate from reaching accounting tests. Its minimal received_at
freshness correction is ported from current main; application behavior and
stale-source refusal are unchanged. CI must pass on the final candidate SHA.
