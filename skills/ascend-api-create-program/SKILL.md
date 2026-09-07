---
name: "ascend-api-create-program"
description: "Create an Ascend program and quote billables through the bounded Ascend API worker with independent API readback. No browser or Playwright."
job_type: "ascend.create_program"
production_ready: false
---

# Ascend API program creation & payment agreement workflow

Use this skill when a user emails Robie or requests creation of an Ascend payment agreement from an insurance quote document.

The operational implementation is fully automated:
1. Extract quote details from email text / attached PDF (carrier, wholesaler, insured, policy/quote number, dates, pure premium, agency fee, commission rate, surplus lines tax, terrorism coverage).
2. Clarify with user via HITL if any of the 4 critical underwriting parameters are ambiguous or missing:
   - Is there an agency fee?
   - What is the commission rate?
   - Is there surplus lines tax?
   - Is terrorism coverage applicable? (Especially if quoted both with and without TRIA).
   If these are clear in the document, proceed immediately without asking.
3. Call Ascend API (`AscendApiClient` / `AscendCreateProgramWorker`):
   - Resolve carrier and wholesaler identifiers via `search_carriers` and `search_wholesalers`.
   - Find or create insured via `find_or_create_insured`.
   - Resolve producer/account manager UUID via `resolve_user`.
   - Create program (`POST /v1/programs`) and billable (`POST /v1/billables`) with `pure_premium_cents`, `agency_fees_cents`, `organization_commission_rate`, and `surplus_lines_tax_cents`.
   - Freshly read back created program and billable (`GET /v1/programs/{id}`, `GET /v1/billables/{id}`).
   - Capture checkout agreement URL (`program_url`).
4. File agreement into EZLynx via EZLynx API:
   - Post discussion note to applicant file with checkout link, premium/fee breakdown, and signature:
     ```text
     Robie was here
     ```
5. Return the Ascend checkout agreement link and summary to the user.

## Required inputs

- Carrier name or identifier
- Insured name / business name
- Effective and expiration dates
- Pure premium amount
- Clarified parameters (agency fee, commission rate, surplus lines tax, terrorism coverage)

## CLI & Module Reference

- `robie_job_engine.quote_extractor`: `QuoteExtractor`, `ExtractedQuote`
- `robie_job_engine.ascend_api`: `AscendApiClient`, `AscendCreateProgramWorker`
- `robie_job_engine.ezlynx_note_poster`: `EZLynxAgreementPoster`, `format_ascend_agreement_note`
- `robie_job_engine.ascend_workflow`: `AscendWorkflowManager`

## Safety and evidence

- Never use Playwright or browser clicks for Ascend; use Ascend API only.
- Secret `ascend-api-key` is fetched securely from Google Secret Manager.
- Duplicate requests reuse idempotency keys to prevent duplicate billing programs.
- Action Gate requires a recorded clean test pass before Production execution.

