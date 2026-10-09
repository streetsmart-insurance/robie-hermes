# Automation readiness and implementation plan

## What can be automated

The repetitive monthly preparation is suitable for deterministic automation:

- calculate the reporting period;
- copy and validate the master spreadsheet;
- run or retrieve the EZLynx shared report;
- parse the CSV and normalize columns;
- preserve creator and source identifiers;
- construct exact-label candidates;
- batch-write the raw-data input range;
- refresh pivots or calculate equivalent summaries;
- reconcile counts and generate an exception report;
- read back the finished working copy.

Eligibility verification should be automated only where an authoritative source can be queried and matched deterministically. Ambiguous account matching, missing evidence, coverage judgment, and case-by-case incentives remain human review.

## Inputs still required before end-to-end execution

1. The canonical master WOW spreadsheet ID, destination folder, input tab and range, protected ranges, pivot names, and exact Apps Script function name.
2. A current EZLynx export sample with the authoritative column headers, label encoding, activity ID, archive behavior, and at least one sanitized example of each label.
3. The approved active employee roster source, role mapping, department mapping, and WOW eligibility rule.
4. The authoritative Google-review feed and the allowed matching keys. The recordings documented one StreetSmart Chat search method and referred to a second method that was not included.
5. Authoritative evidence locations for completed sales, referral conversion, coverage changes, top-QA calls, saved cancellations, EFT signatures, carrier/finance confirmation, effective dates, and written premium.
6. The process-specific SOP that resolves exact boundary rules, qualifying coverage enhancements, and case-by-case incentives.
7. The approved schedule, report owner, review recipients, delivery channel, and retention policy.
8. Sanitized historical fixtures with expected candidate, verified, unverified, and payout results.

Do not hide these gaps in code constants or infer them from one historical month.

## Recommended implementation phases

### Phase 1 — Read-only transform

Accept a manually exported sanitized CSV. Normalize, classify candidates, populate a working-copy template, and produce reconciliation plus exceptions. Do not query live EZLynx or calculate payout-grade totals without evidence adapters.

### Phase 2 — Controlled extraction

Automate the exact EZLynx shared-report filters and export in Test with stable selectors, schema checks, progress checkpoints, idempotent reruns, and raw-export hashing. A changed report name, column set, or employee selector fails closed.

### Phase 3 — Verification adapters

Add one independently tested adapter per label evidence source. Each adapter returns `VERIFIED`, `NOT_QUALIFIED`, or `UNVERIFIED` with a source locator and timestamp. Do not let one adapter's outage convert candidates to zero.

### Phase 4 — Scheduling and review

Schedule the prior-month run only after the prior phases reconcile on sanitized history. Produce a management review queue before delivery or payout processing. Sending and payout remain separately authorized actions.

### Phase 5 — Promotion

Keep the skill `production_ready: false`. Run the repository's CI and regression battery, then three clean Test jobs with persisted post-job-audit `PASS` results. Create the required promotion record and obtain approval for the exact QA-certified digest before any Production use.

## Acceptance criteria

- Rerunning the same source is idempotent and creates no duplicate credit.
- The master template is unchanged and the working copy preserves formulas, pivots, protections, and scripts.
- Every source row is accounted for; every candidate disposition has a reason.
- `created_by` is never silently dropped or inferred.
- Exact-label candidate totals, verification totals, pivots, summaries, and incentive amounts reconcile.
- Tracking-only labels never generate payout.
- Unverified rows never generate payout.
- New Customer CSR uses verified new-business premium rather than a simple count.
- Missing sources, changed schemas, and ambiguous employees fail closed.
- No raw client export, recording, credential, or browser state enters GitHub.

## Known walkthrough gaps

- The recordings end while Google Review verification is still in progress and refer to a continuation that was not present in the reviewed folder.
- The second Google Review verification method was not documented.
- The master spreadsheet link, protected input ranges, and exact Apps Script function name were not provided.
- The walkthrough used manual employee removal and was uncertain about new account-manager eligibility; automation needs a maintained roster instead.
- The manual copy occasionally omitted the creator column; automation must validate it explicitly.

