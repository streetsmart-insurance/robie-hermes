# Progressive Commercial Quote — Team Setup

## What this is
A Muse skill that runs a commercial auto **quote** through Progressive's ForAgentsOnly
agent portal. You provide the ACORD application + a short intake checklist; it logs in,
answers every underwriting question, and reports the quote number and premium.
Quote only — it never binds, issues, e-signs, or pays.

## Part 1 — Get Muse (skip if you already have it)
1. **Download:** iPhone → App Store, Android → Google Play, or use the web app at
   https://muse.ai. (Muse is available in the US and Canada.)
2. **Sign up** in the app.
3. **Redeem the invite (if Jake sent you one):** on mobile go to
   Settings > Redeem token (available for 48 hours after joining); on the web go to
   Settings > General > Usage > Redeem invite code. Enter the code and confirm.

## Part 2 — Install the skill (about 2 minutes)
1. Unzip this folder into your Muse workspace so you end up with:
   `~/workspace/skills/progressive-commercial-quote/SKILL.md`
   (Easiest: attach the zip in Muse chat and say *"Install this skill"* — your
   assistant will place it for you.)

## Part 3 — Connect your accounts (about 5 minutes)
1. **Save your Progressive login.** In Muse chat, say: *"Save my Progressive
   ForAgentsOnly login."* You'll get a secure form — enter **your own** agent
   credentials. Never share logins between people.
2. **Connect your email** to Muse if you haven't already. Progressive sends a
   one-time passcode at every login; the skill reads it from your mailbox
   automatically so you're not interrupted.

## Part 4 — Run a quote
1. Fill out `references/quote-intake-checklist.md` — 13 questions the ACORD doesn't
   cover (truck value, radius, mileage, driver history, current insurance, ELD
   provider, filings, commodity, GL yes/no). Have the ACORD application PDF ready.
2. In Muse chat, attach both and say: *"Run a Progressive commercial auto quote."*
3. The skill answers everything it already knows from the agency defaults below and
   asks you only for what's genuinely missing.
4. You get back: the Progressive quote number, the quoted premium, and confirmation
   of who is listed as business owner.

## Agency defaults (baked in — change only if Jake says so)
- Not currently insured with Progressive Commercial Auto
- Owner's home address = primary business address
- No loan/lease · no attached equipment ($0) · no hazmat · continuous coverage 1+ yr
- Smart Haul: always enroll, data-sharing consent yes
- Blanket Additional Insured: yes · Blanket Waiver of Subrogation: yes
- State filings: no (unless advised) · all vehicles insured (MCS-150): yes
- UM/UIM: minimum · cargo $100k/$1k deductible
- GL (when quoted): $1M occurrence / $2M aggregate, 100% for-hire income, no
  warehouse, no installation, no oil/gas work, no other businesses
- Commodity default: Consumer Goods → Other Consumer Goods
- Smart Haul savings email: the insured's email (never the agent's)

## Notes
- Every quote's questions and answers are appended to `references/qa-log.md` — the
  skill gets smarter with each quote. New questions get classified static/variable
  with you after the run.
- Driver's license numbers are never stored — used transiently for the live quote only.
- Binding, issuing, e-signing, or paying always requires your explicit approval.
