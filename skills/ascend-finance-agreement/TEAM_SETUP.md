# Ascend Finance Agreement — Team Setup

## What this is
A Muse skill that builds an Ascend **premium-finance agreement** (premium finance
program) from a carrier quote. You provide the quote details + a short intake
checklist; it signs in to Ascend, creates the program, and saves it. Save only —
it never e-signs, accepts terms, sends anything to the insured, or uploads
documents.

## Part 1 — Get Muse (skip if you already have it)
1. **Download:** iPhone → App Store, Android → Google Play, or use the web app at
   https://muse.ai. (Muse is available in the US and Canada.)
2. **Sign up** in the app.
3. **Redeem the invite (if Jake sent you one):** on mobile go to
   Settings > Redeem token (available for 48 hours after joining); on the web go to
   Settings > General > Usage > Redeem invite code. Enter the code and confirm.

## Part 2 — Install the skill (about 2 minutes)
1. Unzip this folder into your Muse workspace so you end up with:
   `~/workspace/skills/ascend-finance-agreement/SKILL.md`
   (Easiest: attach the zip in Muse chat and say *"Install this skill"* — your
   assistant will place it for you.)

## Part 3 — Connect your accounts (about 5 minutes)
1. **Sign in to Ascend with your own agency email.** Ascend is passwordless: enter
   your agency email on the sign-in page and it emails you a one-time code. Never
   share Ascend logins between people — each team member uses their own.
2. **Connect your email** to Muse if you haven't already. Ascend sends the
   one-time code at every sign-in; the skill reads it from your mailbox
   automatically so you're not interrupted.

## Part 4 — Build an agreement
1. Fill out `references/program-intake-checklist.md` — 15 items, most copied
   straight off the carrier quote (insured, contact, carrier, quote number,
   dates, premium + taxes + fees, commission %, broker fee, billing email).
2. In Muse chat, attach the carrier quote and say: *"Build an Ascend finance
   agreement."*
3. The skill uses the agency defaults below and asks you only for what's genuinely
   missing.
4. You get back: the saved program URL, gross premium, down payment, installment
   schedule, APR, finance charge, total, first installment due date, and the
   distribution split. The program is saved only — nothing is sent to the client
   until you say so.

## Agency defaults (baked in — change only if Jake says so)
- Commission: the % stated on the carrier quote (never guessed)
- Broker/agency fee: $500, financeable (Jake has directed $550 on recent programs —
  confirm per program)
- Save only: never e-sign, accept terms, send to the insured, or upload documents
- Billing/notice email: the agency's shared inbox (hello@streetsmart.insurance),
  never a personal inbox
- Policy effective date: 2 weeks from the quote date unless the quote states one
- One program can finance a combined premium from more than one carrier

## Notes
- Every build is appended to `references/build-log.md` — program URL, terms, and
  status — so the team can see what's saved vs still pending with the client.
- If the policy effective date is more than ~20 days before the build date, Ascend
  requires the carrier quote PDF attached before it will save — have the PDF ready.
- E-signing, sending, or accepting terms always requires your explicit approval.
