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
  the same way. Links that resolve to HTML / 401 / 403 are portal links:
  they are ATTEMPTED through a carrier portal login
  (`robie_job_engine/hello_portal_fetch.py` — portal registry by domain,
  credentials read at runtime from GCP Secret Manager via ADC, one
  Playwright login attempt on the box Chrome, no retry loop). Fetched
  bytes flow into the normal extraction pipeline with `via_portal=True`.
  When the portal domain is unknown, or the login fetch fails or times
  out, the link is recorded as `portal_link_needs_human` on the queue
  entry — the deferral is the fallback, never a silent drop.
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

## Portal login fetch — adding a new carrier portal

Portal links (HTML / 401 / 403) are attempted through
`robie_job_engine/hello_portal_fetch.py` before deferring to a human.
To add a portal:

1. Create the two secrets in GCP Secret Manager (project
   `streetsmart-hermes-poc`) — record only the NAMES, never the values:
   `gcloud secrets create <carrier>-portal-username --project=streetsmart-hermes-poc`
   `gcloud secrets create <carrier>-portal-password --project=streetsmart-hermes-poc`
2. Register the portal where the worker starts up (the registry is
   in-code; unknown domains keep deferring to a human):

```python
from robie_job_engine.hello_portal_fetch import register_portal, PortalConfig

register_portal(PortalConfig(
    domain="agents.examplecarrier.com",   # matches subdomains too
    username_secret="projects/streetsmart-hermes-poc/secrets/<carrier>-portal-username/versions/latest",
    password_secret="projects/streetsmart-hermes-poc/secrets/<carrier>-portal-password/versions/latest",
    login_url="https://agents.examplecarrier.com/login",
    handler="generic_form",  # or a custom handler via register_handler()
))
```

3. The worker passes the real fetcher into retrieval:
   `retrieve_link_docs(body, portal_fetch=make_portal_fetch())`
   (no args = ADC secrets + box Chrome, exactly one login attempt).

Per-portal quirks (MFA, multi-step logins, non-form sign-ins) are
follow-ups: add a handler via `register_handler(name, fn)` and point the
portal's `handler` at it. The generic username/password form handler
covers the common case.

Safety recap: secret values are transient-only (ADC at runtime on
hermes-poc-01, never a key file in the repo, never logged, never in
errors); failed logins are never retried in a loop; timeouts everywhere.

The original design note below is kept for context: the match order
follows the certificate intake's strategy order (report email -> report
name -> sender alias -> policy anchor) against the hello alias store
(`hello_sender_aliases.json`, separate from the cert store).

Note: `HelloIntake.assignment_for` only accepts `new_business`,
`renewal`, `midterm`. The hello-specific types (client_issue,
carrier_notice, premium_finance, invoice_billing, endorsement, document,
wholesaler_mga, voicemail_text_notify, binder_bound, general_question)
route to their `ROUTE_FOR_REQUEST_TYPE` roles in the queue report;
extending the intake stub to accept them is a separate, small follow-up
PR.

## Phase 2 — the six inbox fixes (2026-09-27)

Built on this branch against the 6-month hello@ analysis
(`~/workspace/your_files/hello-inbox/hello-inbox-analysis.md`).

### 1. Forward unwrapping + dedupe — `hello_forwarding.py`

53% of hello@ volume is forwards (mostly Carlo bare-forwarding carrier
material). The module detects forward envelopes (Gmail
`---------- Forwarded message ---------` and Outlook
`-----Original Message-----` blocks, `Fwd:` subjects), unwraps to the
original sender/subject/body/date, and collapses direct/forward
duplicates on the (original sender, normalized subject, calendar day)
key — falling back to body-hash when the sender is unknown so two
different forwards never collapse. `classify_with_envelope()` returns
the envelope (`internal_forward` / `internal_discussion` /
`external_forward` / `external_direct`) and always classifies the INNER
content — the forwarder's address never decides the request type.

### 2. Per-category filing spec — `hello_filing.py`

Single source of truth for what "filed" means per category: owner role,
SLA, discussion hint, whether documents upload, whether a human task is
required, whether an on-duty alert fires. `build_filing_plan()` produces
pure data; `execute_plan()` runs it against injected interfaces
(dry-run by default — zero writes unless interfaces are supplied AND
`dry_run=False`). Fail-closed gates: triple identity verification
(policy/sender-anchored high-confidence match only — name-only matches
fail, the Rivera lesson), the money-claim rule (no "payment posted"
without bank/feed/register + date + amount), placeholder rejection,
task dedupe against open tasks (never duplicate a Client Center task).
DONE = every required action read back AND every required human task
exists (created or already open).

### 3. Cancellation / non-pay workflow — `hello_cancellation.py`

`notice_subtype()` (cancellation / reinstatement / non_renewal /
renewal_notice / other; DNOC counts as cancellation) drives the plan.
A cancellation creates ONE same-business-day client-call task
(`cure_or_cancel`) that stays open until cure or confirmed cancel; the
append-only JSONL `CancellationLedger` keeps the per-policy through-line
so repeat notices (the PHLY PHPK2734817-000 loop: five notices, zero
action) attach to the open entry instead of spawning new tasks.
Reinstatement files its own note and proposes closing the open entry; a
human confirms the close with `cure_confirmed` / `cancel_confirmed` /
`replaced` — only those values are accepted.

### 4. Premium-finance routing

The old `billing` type is split: `premium_finance` (Capital Premium,
Ascend, USPF) vs `invoice_billing` (carrier invoices, return premium).
Past-due/cancellation PF mail files as `premium_finance_urgent`:
Accounting owner, 24-hour SLA. Routine PF mail: Accounting, 48-hour
SLA, filed for the record. Vendor-sender protection is untouched
(`useascend.com`, `phly.com`, `brownandjoseph.net` still never become
aliases).

### 5. Voicemail / SMS callback — `hello_voicemail.py`

`parse_voicemail_notification()` extracts message type, caller name,
callback number, transcription, and received time from RingCentral
relays. The filed summary note carries the transcription but NEVER the
digits (`sanitize_note_body()`); the on-duty alert carries the callback
number with the one-business-hour SLA task. The old classifier's
"text-message relay is noise" behavior is retired — 14 unanswered
notifications in 6 months is the evidence.

### 6. Classifier updates — `hello_classifier.py`

`billing` → `premium_finance` + `invoice_billing`; new
`wholesaler_mga` (UW-requirement content patterns + sender-domain
fallback — Bridge, JJINS, RPS, CRC, Tuscano, Bass, RLIG, XPT), new
`voicemail_text_notify`, new `binder_bound`; `carrier_notice` gains
reinstatement patterns; INTERNAL now returns the envelope subtype
(`internal_forward` vs `internal_discussion`) in `request_type`.
`notice_type_of()` (`hello_triage.py`) learned the observed
"pending cancel" subject. Routes: premium_finance/invoice_billing →
accounting, wholesaler_mga → originating_producer, voicemail_text_notify
→ on_duty.

## Merge order vs the certificate PRs

- **PR #609** (cert classifier fixes) — independent of this PR. Either order.
- **PR #610** (cert unmatched queue) — independent of this PR. This PR
  deliberately duplicates the queue *pattern* into hello-scoped modules
  instead of importing from #610, so there is no cross-PR dependency and
  no merge-order constraint. If #610's queue later grows shared helpers,
  the hello module can adopt them in a follow-up; it must not be blocked
  on #610.

This PR now touches existing files (`hello_classifier.py`,
`hello_triage.py`, `hello_match.py`) plus new modules
(`hello_forwarding.py`, `hello_filing.py`, `hello_cancellation.py`,
`hello_voicemail.py`) and tests. It can merge before, after, or
independently of #609/#610.
