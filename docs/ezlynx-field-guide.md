# EZLynx Field Guide

For ROBIE workers and anyone automating EZLynx. Every entry quotes the exact
label or text seen, with the date and the evidence. Add new discoveries as
dated entries at the bottom of the relevant section — branch, PR, Carlo's
review, like everything else.

Last updated: 2026-09-15.

## 1. Carrier identity: Master Company + NAIC code

- The real carrier identity is the **Master Company** and the **NAIC code**.
- The fields literally labeled carrier ID and writing company read `"0"` on
  every policy seen. That is a proven API/DOM read defect, not the truth.
  Do not use those two fields as the carrier identity.
- Pinned ground truth (2026-09-12): masterCompany `13585`,
  writingCompany `10048`. `420006` is a bad duplicate carrier record;
  `420004` is not in the roster.

## 2. Dropdown label mismatches

- The Billing Type dropdown shows **"Direct"**, not "Direct Bill".
  (Fixed in code by PR #380 as a targeted alias for this one field only.)
- The Department dropdown did not contain "Personal" (2026-09-13 HITL
  failure: `Option 'Personal' not found in Department dropdown`). The literal
  available options are still pending — do not guess them.
- Standing rule: if a dropdown selection fails with "option not found", read
  the actual rendered labels first. Never assume the code's expected label
  matches EZLynx's text. Do NOT generalize one field's alias into a shared
  map without a second confirmed mismatch.

## 3. FormEntry coverage fields (Homeowners)

- FormEntry has tabs:
  `Insured Information | Dwelling Info | Coverages | Underwriting | Additional Interest`.
  Coverages is a **tab**, not a separate page.
- On the Coverages tab, after Select Location and Form #, the
  `COVERAGES / LIMITS OF LIABILITY` rows use plain-English labels, captured
  verbatim from the live page on 2026-09-13:
  - A = `Dwelling` — limit prompt:
    `Enter limit: The limit associated with dwelling coverage.`
  - B = `Other Structures` — limit prompt:
    `Enter limit: The limit associated with other structures coverage.`
  - C = `Personal Property Coverage` — limit prompt:
    `Enter limit: The limit associated with personal property coverage.`
  - D = `Loss of Use` — limit prompt:
    `Enter limit: The limit associated with loss of use coverage.`
  - E = `Personal Liability Each Occurrence` — limit prompt:
    `Enter limit: The limit associated with personal liability each occurrence coverage.`
  - F = `Medical Payments Each Person` — limit prompt:
    `Enter limit: The limit associated with medical payments each person coverage.`
- `Loss of Use` has an `Actual Loss Sustained` checkbox above its amount.
- `DEDUCTIBLES` columns are `AMOUNT / PERCENT / TYPE`, followed by
  `OPTIONAL COVERAGES - ENDORSEMENTS`.
- The coverage form does **not** re-ask for Billing Type or Department.
- Superseded: invented `#HO_CoverageA`–`#HO_CoverageF` selectors never matched
  the live DOM. Do not use them.

## 4. FormEntry traps

- **No location means no Coverages tab.** If the policy has no location, the
  Coverages tab can never open. Check for a location first. (Proven
  2026-09-12 — this was the E01 root cause.)
- **Hidden duplicate controls.** Two elements can share an id or name with
  one of them hidden; a naive fill hits the wrong one. Flag every duplicate
  id/name explicitly. (From Carlo's hand-driven notes, 2026-09-12.)

## 5. PolicyApi limits

- The ACORD XML carries **no** Coverage A–F limits or deductibles, so the
  browser stays for FormEntry. API read-back alone is never sufficient for
  coverage verification.
- There is **no applicant-scoped policy list**. "What is on this account" is
  answerable only per known policy number:
  `PolicyApi/policy/v1/search?PolicyNumber=`.
- `search?ApplicantId=` returns unfiltered rows. Treat
  `applicant_filter_honored=false` — verify by policy number instead.
- Policy create: `POST /PolicyApi/account/{accountId}/policy/v1/create`.
  Write allowlist: `["220250093"]` only. Never bind, never move money.

## 6. DocumentApi (proven 2026-09-10–11)

- Search: `GET /documentapi/documents/v1/account/{ApplicantID}/document-search`
- Download: `GET /documentapi/documents/v1/{DocumentID}/download`.
  Do NOT use the stale `documentUrl` from search results.
- Upload: `POST /DocumentApi/documents/v1/account/{ApplicantID}/document`
  with multipart fields `DocumentName`, `File`, and `PolicyMasterId=0`.
  Returns a new document ID.
- Authenticate as an agency user (SSRobie / Carlo1). The vendor user
  `ssr_userPROD` cannot access agency applicants.
- API identity: user SSRobie, client_id `street_smart_api`, integration
  group 159, scopes `DiscussionApi openid PolicyApi DocumentApi`.

## 7. Notes: read unavailable, post unproven

- Note-read: four endpoint shapes tried, all returned HTTP 404
  (as of 2026-09-14). There is no working read path.
- Note-post: code exists (`POST /DiscussionApi/discussions/v1/notes` is the
  shape marked in code) but it is **unproven**. The manual rule stands: every
  EZLynx account touch gets a note.

## 8. Reports

- Policy Master reports are canned SSRS with a **fixed one-row-per-policy
  grain**. No grouping or distinct option exists. You cannot build a
  one-row-per-customer saved report in the EZLynx UI — dedupe happens in
  code on the export.

## 9. Changelog

- 2026-09-15: guide created from the 2026-09-10–14 quoting and policy-setup
  sessions (Carlo approved).
