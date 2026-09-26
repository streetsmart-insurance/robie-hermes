# Daily accounting collector: stage 2 investigation

This is a **draft, unmerged, undeployed** continuation of the stage-1 evidence
prototype in PR #597. It adds test-first read-only pagination and candidate
briefing-input contracts. It does not wire a scheduler or publish the 5:30 AM
briefing. No source can be called production-complete from the current access.

## Ascend

A production GET to `/v1/invoices?page=1` with the box's configured key
returned HTTP 401 on September 26, 2026; no credential value was printed.
The authenticated Ascend dashboard has a Custom integrations screen with a
"Generate new key" control that **expires the current key**. Do not use that
control until other consumers have been inventoried and Carlo explicitly
approves the production credential change and rollout. Secret Manager currently
shows two enabled versions of `ascend-api-key`, so first verify which version
and environment each worker selects without logging secrets.

The new `collect_ascend` path GETs each collection until `meta.next` is null,
rejects missing metadata, loops, duplicate IDs, count mismatches, foreign or
non-sequential next links, and page caps, and emits only all-or-nothing evidence.
Ascend's public invoice API description documents `page` and `meta.next`:
https://developers.useascend.com/reference/listinvoices . Verify pagination
shape and `meta.count` semantics with authorized live responses before
calling a zero-result complete. The existing one-page calls in
`ascend_sync.py` remain unchanged.

## Applied Pay

`collect_applied_portal` validates a supplied complete expanded batch-page
sequence and return/chargeback line IDs. It does **not** log into the portal or
prove that the supplied pages cover every return. The batch-settlement email
reader is scoped to emails and cannot establish a full portal return inventory.
A read-only, source-authenticated portal reader still needs a date boundary,
all-batch paging/virtual-scroll verification, row counts, duplicate detection
and live comparison with settlement emails. Its output must not claim complete
until that reader is verified.

## EZLynx and briefing

The only currently verified Accounting Team view is the Agency Tasks UI,
filtered to Assigned User = Accounting Team. It shows individual accounts,
checklists, assignee and due dates, but lacks proven stable IDs and exhaustion.
No complete item-level API/export has been established, so this collector
rejects UI summaries lacking IDs and returns incomplete. An unverified CSV
or discussion note cannot upgrade task coverage.

`briefing_payload` prepares structured, evidence-linked candidate input with
explicit incomplete coverage and bank-clearing caveat. Scheduling, immutable
storage, freshness enforcement, 5:30 AM delivery, and independent re-checks
remain unbuilt. The briefing consumer must carry warnings, not silently omit
missing sources. Do not schedule or deploy without a separately approved run.

## Test boundary

Synthetic tests only; no client records in Git. Import failure was verified
before implementation. Focused tests assert pagination exhaustion, loop and
foreign-link rejection, auth failure redaction, portal partial-page rejection,
EZLynx missing-ID rejection, and candidate briefing warnings.
