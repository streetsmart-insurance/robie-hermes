---
name: "robie-quoting"
description: "Run carrier quoting for Robie across Tarmika, carrier portals, and the EZLynx Submission Center. Quote only — never bind. Every premium needs a documented source matched to the right client, and every portal deviation from the request gets flagged."
job_type: "quoting"
production_ready: false
---

# Robie quoting

Use this Skill when Robie quotes new business or renewal business: requesting
quotes from carriers, capturing premiums, generating carrier proposals, and
filing quote documents back to EZLynx.

The live failures this Skill prevents:
- Robie sent a signed BOR change letter to Selective for Par-Troy, then
  learned Par-Troy had no Selective policy for 4–5 years. BOR retracted,
  pivoted to new-business submission.
- A $18,000 Progressive auto premium floated with no document anywhere; a
  later Progressive quote (Q124896212, $4,734.16) belonged to a different
  client (Pat Viswanathan).
- Travelers' QUOTE PROPOSAL button and Nationwide's Display Proposal produced
  nothing; one-page print-to-PDF portal summaries were filed as if they were
  official proposals.
- A client email stated USLI GL was bound eff 09/20/2026 while the bind
  request was still under review with the signed app outstanding.
- GUARD silently replaced the requested $212,000 building limit with its
  $870,565 minimum and dropped non-owned liability.

This is a backend workflow Skill, not Chat small talk.

## Hard stops

- Quote only. Never bind, issue, pay, or take payment.
- Never invent or substitute underwriting facts: equipment, Original Cost New,
  payroll, class codes, vehicles, drivers, garaging. A missing required value
  is carded to Carlo and the quote parks. It is never guessed or borrowed from
  another vehicle's data.
- Never send a BOR letter without a confirmed active policy number AND
  effective dates on file.
- Never state bind status to a client without carrier destination proof:
  carrier confirmation, policy number, or portal bind status. "Under review"
  is not bound.
- Never present a portal summary or print-to-PDF as the carrier's official
  proposal.
- Never carry a premium between clients. Every premium needs its documented
  source (quote/request number, PDF, or portal read-back) matched to the
  correct client. A premium with no document is UNVERIFIED.
- Never email or send a proposal from inside a carrier portal. Generate with
  Create only, then handle the file.

## Channel selection

Pick the channel per line before starting. See
`references/quoting-channels.md` for the full channel guide.

- **Tarmika**: BOP, Workers Comp, General Liability, Cyber, Professional
  Liability only. No Umbrella/Excess, no Inland Marine — those go outside
  Tarmika. Lines cannot be added after the quote is created: finalize the
  line list first.
- **Commercial auto**: carrier websites directly
  (GEICO, GUARD, Progressive, Nationwide, Selective) — never Tarmika.
- **EZLynx Submission Center**: check each carrier's availability per line
  before promising that channel (e.g. Guard is not in the Submission Center,
  so Guard auto needs a portal quote).

## Requesting quotes

1. Enter only values from the file or Carlo's direction. Underwriting answers
   come from documents on the EZLynx file (e.g. equipment schedules from the
   supplemental application PDF) — never invented.
2. In Tarmika, only in-appetite markets are selectable. A market showing
   NOT IN APPETITE (greyed out) is not quotable — never promise it.
3. `Quote Request Not Submitted` means a required question is unanswered —
   answer it. It is not a decline.
4. Do not attribute a decline to a carrier unless the portal names that
   carrier. Unnamed declines stay UNVERIFIED as to carrier.

## Capturing results

Record per carrier, per line: quote/request number, premium, status,
effective date, and source.

- Tarmika quotes showing `Needs to be Referred` are referral quotes, not
  final pricing. Valid Till N/A is expected. Never present them as final or
  bound.
- When a portal overrides the request, report the portal's actual values and
  flag every deviation: forced minimum limits (GUARD forced $870,565 building
  vs $212,000 requested against a $967,294 replacement cost), dropped
  coverages (non-owned liability missing), re-rated premiums. Never present
  portal-forced values as the requested ones.

## Proposal documents

1. Generate with the portal's Create/Generate control only. Never email or
   send from the portal.
2. Verify the actual file before attaching, filing, or sending: open it,
   confirm multi-page real content (not a one-page summary screenshot),
   record page count, byte size, and sha256.
3. A print-to-PDF portal summary is labeled a summary — never the official
  proposal. If the portal's proposal button is unresponsive, say so; do not
   silently substitute a summary.
4. Client-facing proposal documents are built only with the approved
   carrier-proposal generator bundle. Confirm the Taxes and fees / agency fee
   line matches the approved values.
5. When matching forms to a quote: if it's not on the quote, don't get the
   form.
6. Quote contract mechanics (settlement, valuation) from the actual policy
   form — never from carrier marketing copy.

## Filing back to EZLynx

- Upload quote PDFs via the OAuth DocumentApi (`upload_applicant_document`),
  with read-back via `search_applicant_documents` asserting a HIT. Never use
  the browser to upload documents.
- File quote notes via the Discussion API on the existing titled discussion
  (`get_discussions` → `append_note`). Never create an Untitled discussion.
  Every Robie note includes the exact phrase `Robie was here`.
- Notes are plain English, extremely concise, for non-technical agency staff.

## Completion

A quoting Job is COMPLETE only with destination-verified evidence per
carrier: quote/request number, premium, and status read back from the portal
or submission, plus the proposal file validated (pages, bytes, hash) where a
proposal was generated.

No evidence row = UNVERIFIED, never COMPLETE. "Quote requested" is not
"quote received." A premium with no documented source is UNVERIFIED.
