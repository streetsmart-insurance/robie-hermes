---
name: "ascend-api-create-program"
description: "Create an Ascend program and quote billables through the bounded Ascend API worker with independent API readback. No browser or Playwright."
job_type: "ascend.create_program"
production_ready: false
---

# Ascend API program creation

Use this skill for new Ascend program creation. The operational implementation
is API-only: `POST /v1/programs`, then `POST /v1/billables`, followed by fresh
authoritative `GET` readback of the program and every returned billable.

## Required inputs

- Existing Ascend insured UUID
- Producer UUID
- Account Manager UUID
- At least one billable with quote identifier, carrier identifier, coverage
  identifier, effective and expiration dates, and premium in integer cents
- Explicit execution approval (`execute=true`)

Missing or ambiguous identifiers are `NEEDS_CLARIFICATION`; never infer them
from display names. Reject unsupported fields and the forbidden PAWIVA /
`221398001` accounts before loading credentials.

## Safety and evidence

- Never use Playwright, CDP, browser selectors, or the Ascend dashboard UI.
- Test accepts only `https://sandbox.api.useascend.com` and a versioned sandbox
  credential referenced through Secret Manager.
- Never email, send a checkout link, take payment, bind, or use a real client
  as part of program creation.
- Persist the returned program ID and billable IDs.
- A partial billable failure is not COMPLETE and must not automatically create
  another program.
- COMPLETE requires `ASCEND_API_FRESH_GET` authoritative evidence matching the
  requested insured, Producer, Account Manager, billable count, and program ID.
- Production remains gated until an independently reviewed Test API creation
  passes and Carlo separately approves the exact release.

## Acceptance tests

1. Plan mode performs no network request.
2. TEST refuses the Production API origin; Production requires its second
   explicit enable flag.
3. Missing, bad, or forbidden values fail before credential access.
4. Program and all billables are created once and freshly read back.
5. Duplicate delivery returns the same durable Job and does not repeat POSTs.
6. Interruption or partial execution never silently retries program creation.
7. Missing or mismatched API readback remains UNVERIFIED.
8. No browser, recording, click, email, checkout, payment, or bind is involved.
