# Hello intake classification — wiring guide

## What this is

`robie_job_engine/hello_classifier.py` brings the certificate intake's
pipeline shape to the hello@ inbox:

1. **Classify** every incoming hello@ email — genuine request (with a
   hello-specific request type) vs autoresponder/bounce, acknowledgement,
   noise (newsletters, carrier marketing, system notifications), internal
   (agency staff forwarding), or unknown.
2. **Extract** client identity — sender email/name, company/insured name,
   policy numbers, MC numbers.
3. **Match** to an EZLynx applicant (daily report email/name, hello sender
   aliases, policy anchors via the read-only PolicyApi path). Vendor/system/
   carrier senders NEVER become fixed aliases.
4. **Genuine + matched** → the existing `HelloIntake` routing/filing path
   (`hello_intake.py`).
5. **Genuine + unmatched** → `hello_unmatched_queue.py`: a human-clearable
   JSONL queue with an oldest-first markdown report and a resolve CLI that
   learns the sender→applicant alias on resolve so it never asks twice.

Everything here is read-only: no EZLynx writes, no Zapier calls, no email
sends. The modules never import the EZLynx API.

## Genuine categories (scoped from real hello@ mail, Sep 2026)

| request_type | example observed in hello@ | hello route |
|---|---|---|
| new_business | "Insurance for a new Little Andy's Setup within a market" | originating_producer |
| renewal | "Renewal Quote for Jersey Strong Properties LLC" | applicable_csr |
| midterm | "Trailer to add to my policy", "Policy change - READY 2 ROLL MOVING LLC" | applicable_csr |
| client_issue | "Issues with our policies that need Resolved Immediately" | applicable_csr |
| carrier_notice | "Policy Rescission Notice 1-HNY-NJ-01-014332", "Notice of Cancellation" | applicable_csr |
| billing | "Return premium received for Policy 3AB025200", "SETTLEMENT OFFER / Capital Premium Financing" | applicable_csr |
| endorsement | "Additional Interest added - Business Owners BP00109727" | applicable_csr |
| document | client attaching driver's license / dec pages / loss runs | applicable_csr |
| general_question | direct questions to the agency ("can you confirm…?") | applicable_csr |

Noise held (never queued): autoresponders/bounces (same guard philosophy as
certs — they quote request language, checked first), newsletters/marketing
("Severe Weather Resources", SWYFFT), system notifications ("X has sent you
a text message", Sonant call digests), vendor billing spam. Internal
(Carlo/Jake forwarding carrier notices into hello) is classified
`internal` — real work, already owned; it files but never enters the
unmatched queue as a new client request.

## Where it wires in (after this PR merges)

`hello_intake.py` is Phase-1 routing: it takes a human-confirmed request
type and performs the ownership check. The classifier slots in *before*
it, in the hello sweep worker:

```
for each hello@ message:
    action, request_type, reason = classify_hello(subject, body, sender)
    if action in (AUTO_REPLY, ACK, NOISE):   hold (safe bucket) — no queue
    if action == INTERNAL:                   file to discussion, no matching
    if action == UNKNOWN:                    hold for human review (no queue —
                                             not known-genuine)
    if action == GENUINE:
        identity = extract_hello_identity(subject, body, sender)
        applicant = match(identity)  # report email/name -> hello aliases ->
                                     # policy anchors
        if applicant:  HelloIntake routing/filing path (existing)
        else:          hello_unmatched_queue.enqueue_unmatched(
                           sender_email=..., subject=...,
                           request_type=request_type,
                           route=ROUTE_FOR_REQUEST_TYPE[request_type],
                           hold_reason=..., strategies_tried=[...],
                           gmail_id=..., evidence=[...])
```

The `match()` step is built in `robie_job_engine/hello_match.py`
(tests in `tests/test_hello_match.py`):

- **Document retrieval**: attachments -> text (PDF via pypdf, .txt);
  direct document links in the body are fetched over HTTP and extracted
  the same way. Links that resolve to HTML / 401 / 403 are NOT logged
  into — recorded as `portal_link_needs_human` on the queue entry.
- **True identity**: policy numbers + insured names as printed on the
  carrier doc outrank the email body's claims; conflicts are evidence.
- **Reconciliation ladder**: hello sender alias (skipped for
  vendor/carrier/system senders — never fixed aliases) -> report email
  -> policy exact -> policy normalized (formatting + carrier letter
  affixes) -> EZLynx policy anchor (fail-closed, reuses
  `hello_triage.resolve_applicant_id`) -> fuzzy name (scored; a weak
  fuzzy match is never a hit; name-only never auto-files).
- **Confidence**: only `high` auto-files. `medium`/`low` go to
  `hello_unmatched_queue` via `handle_match_result()` with the doc
  evidence and portal links attached.

The original design note below is kept for context: the match order
follows the certificate intake's strategy order (report email -> report
name -> sender alias -> policy anchor) against the hello alias store
(`hello_sender_aliases.json`, separate from the cert store).

Note: `HelloIntake.assignment_for` only accepts `new_business`,
`renewal`, `midterm`. The hello-specific types (client_issue,
carrier_notice, billing, endorsement, document, general_question) route
to `applicable_csr` in the queue report; extending the intake stub to
accept them is a separate, small follow-up PR.

## Merge order vs the certificate PRs

- **PR #609** (cert classifier fixes) — independent of this PR. Either order.
- **PR #610** (cert unmatched queue) — independent of this PR. This PR
  deliberately duplicates the queue *pattern* into hello-scoped modules
  instead of importing from #610, so there is no cross-PR dependency and
  no merge-order constraint. If #610's queue later grows shared helpers,
  the hello module can adopt them in a follow-up; it must not be blocked
  on #610.

This PR touches zero existing files (4 new files + tests). It can merge
before, after, or independently of #609/#610.
