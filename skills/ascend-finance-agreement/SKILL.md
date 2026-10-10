---
name: "ascend-finance-agreement"
description: "Build an Ascend premium-finance agreement (premium finance program) in a live browser task on dashboard.useascend.com: passwordless sign-in via an emailed one-time code, create the program from the carrier quote's premium/taxes/fees with the quote-stated commission and a broker fee, save only (never e-sign, send, or upload), then report the loan breakdown. Trigger when the user asks for an Ascend finance agreement or premium finance program."
---

# Ascend Finance Agreement

## Purpose
Turn a carrier quote's premium, taxes, and fees into a saved Ascend premium-finance
program the client can later e-sign. Build only — it never e-signs, accepts terms,
sends anything to the insured, or uploads documents, unless the user explicitly
approves that exact action.

## Workflow
1. **Gather the inputs** from `references/program-intake-checklist.md`. Extract from
   the carrier quote: named insured, primary contact (name, email, phone), business
   address, coverage type(s), carrier, MGA/wholesaler, quote number, policy
   effective/expiry dates, quoted premium + taxes + fees, and the commission %
   stated on the quote. Ask the user only for what the quote and checklist don't
   cover (usually just the broker fee and the billing email).
2. **Spawn the browser task** at `https://dashboard.useascend.com` (the Ascend
   dashboard). Brief it with the program inputs and the instruction to create and
   **save the program only**.
3. **Sign-in.** Ascend is passwordless: enter the user's agency email on the sign-in
   page, then Ascend emails a one-time verification code. Find the fresh Ascend
   code email in the connected mailbox via the protected verification-code read
   path, then relay the exact `[credential:<uuid>]` to the task with
   `browser.steer_task` — the browser fills it with `credential_fill` (never
   extract or type the raw code). Each team member signs in with **their own**
   Ascend account; logins are never shared.
4. **Create the program.** Click "New program" and fill the form: producer/account
   manager, insured business + primary contact details, coverage type(s), carrier,
   MGA/wholesaler, quote number, policy effective/expiry dates, quoted premium +
   taxes + fees, commission %, and the broker/agency fee (financeable). Gross
   premium = quoted premium/taxes/fees + broker fee.
5. **Review before saving.** Check the payment distribution (agency receives
   commission + broker fee; each carrier/MGA receives the rest) and the loan terms
   (down payment, number of installments, APR, finance charge, total of payments,
   first installment due date). Fix anything that doesn't match the quote.
6. **Save only.** Click **Save program** and stop. Verify on the saved program page
   that the Communications tab shows no emails sent and nothing is e-signed.
   Exception: if the policy effective date is more than ~20 days before the build
   date, Ascend may require the carrier quote PDF attached before it will save —
   grant the PDF to the browser task for upload, or ask the user to attach it under
   the program's Documents tab.
7. **Report.** Program URL, gross premium, down payment, installment amount ×
   count, APR, finance charge, total of payments, first installment due date, and
   the distribution split (agency $X / carrier(s) $Y). Do not claim the program is
   saved until the live task confirms it.

## Standing answers (confirmed with Jake)
Use these without asking; only deviate when the quote or Jake says otherwise.

**Always (static):**
- Commission: use the commission % **stated on the carrier quote** (Jake's standing
  rule since 2026-09-19). Ask only if the quote doesn't state one or it's
  ambiguous.
- Save only: never e-sign, accept terms, send to the insured, or upload documents.
- Billing/notice email on the program: the agency's shared inbox
  (hello@streetsmart.insurance) — never the producer's personal inbox.
- The program finances the quote's premium + taxes + fees **plus** the broker fee.

**Defaults (use unless Jake says otherwise):**
- Broker/agency fee: $500, financeable. Jake has directed $550 on recent programs —
  confirm the fee per program when he hasn't stated it.
- Policy effective date: 2 weeks from the quote date (matches the agency's quoting
  rule), unless the quote or Jake states one.
- One program may finance a **combined** premium from more than one carrier (e.g.
  auto liability from one carrier + motor truck cargo from another) — gross premium
  is the sum, distribution splits per carrier.

**Always confirm per program (variable):** the exact premium/tax/fee figures from
the quote, the broker fee for this program, the billing email, and the insured's
primary contact details.

## Output Contract
- Saved Ascend program URL (e.g. https://dashboard.useascend.com/programs/<id>)
- Gross premium, down payment, installments, APR, finance charge, total, first
  installment due date
- Distribution split: agency amount (commission + broker fee) vs carrier/MGA
  amount(s)
- Explicit confirmation: saved only — nothing e-signed, sent, or uploaded

## Operating Rules
1. **Save only.** Never e-sign, accept loan terms, send the agreement to the
   insured, or upload documents unless the user explicitly approves that exact
   action.
2. **One-time codes stay protected.** Relay the `[credential:<uuid>]` reference;
   never extract, type, or repeat the raw code, and never reuse a code for a new
   sign-in.
3. **Never invent numbers.** Premium, taxes, fees, and commission come from the
   carrier quote — not from memory, estimates, or a similar account.
4. **Verify before reporting.** The Communications tab must show nothing sent; the
   program page must show the saved terms. Report only what the live task
   confirmed.
5. **Log every build.** Append each program to `references/build-log.md` (date,
   insured, carrier/quote, gross, terms, program URL) so the team can track what's
   saved and what's still pending with the client.
6. **Your own login.** Each team member installs this skill in their own Muse and
   signs in to Ascend with their own agency email. Never share Ascend logins.
