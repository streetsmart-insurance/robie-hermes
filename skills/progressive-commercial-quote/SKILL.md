---
name: "progressive-commercial-quote"
description: "Run a commercial auto quote through Progressive's ForAgentsOnly agent portal in a live browser task: sign-in (saved credentials + email one-time passcode), step through the quote flow, and answer underwriting questions from the stored Q&A log, asking the user only for what isn't already answered. Trigger when the user asks for a Progressive quote or to quote a trucking/business-auto risk through Progressive."
---

# Progressive Commercial Quote

## Purpose
Complete a Progressive commercial auto **quote** (never bind, issue, or pay) for a trucking/business-auto risk, reusing stored answers across submissions so Jake only gets asked what's genuinely new.

## Workflow
1. **Gather application data.** The ACORD application (Business Auto Section + driver table) from `~/workspace/user/files/`, or the EZLynx documents link Jake provides — plus the answers to `references/quote-intake-checklist.md` (the 13 questions the ACORD doesn't cover). Extract: named insured, garaging/mailing addresses, DOT number, operation type, radius, drivers (names, DOBs, license numbers + states from the ACORD driver table), vehicles (year/make/model, VIN, value), requested coverages/limits/deductibles, effective date.
2. **Spawn the browser task** at `https://www.foragentsonlylogin.progressive.com/Login/` (the direct login page — not the foragentsonly.com homepage). Brief it with the application data and the instruction to quote only.
3. **Sign-in.** Credentials are saved in the Secure Vault; the browser task fills them. Progressive usually requires an email one-time passcode afterward: find the fresh Progressive passcode email in the connected mailbox via the protected verification-code read path, then relay the exact `[credential:<uuid>]` to the task with `browser.steer_task` — the browser fills it with `credential_fill` (never extract or type the raw code).
4. **Answer underwriting questions.** As the task hands off questions: first check `references/qa-log.md` for a stored answer marked static or previously answered for this client; steer those directly. For anything unanswered, ask the user in one short message, then **append the new Q&A to the log** before steering. Prefer `references/quote-intake-checklist.md` answers when the user provides them with the application.
5. **Report.** Quote number, quoted premium, and explicit confirmation of who is listed as owner/principal. Do not claim the quote is complete until the live task confirms it.

## Standing answers (confirmed with Jake 2026-09-25/26)
Use these without asking; only deviate when the application or Jake says otherwise.

**Always (static):**
- Not currently insured with Progressive Commercial Auto
- Owner's home address = primary business address
- No loan/lease on the vehicle; no permanently attached equipment ($0)
- No hazmat placard (general-freight default)
- Continuous coverage ≥ 1 year: Yes
- Smart Haul: enroll Yes; consent to driving-data sharing Yes
- Blanket Additional Insured: Yes; Blanket Waiver of Subrogation: Yes
- State filings: No (unless Jake advises otherwise); all commercially owned/operated vehicles insured (MCS-150): Yes
- UM/UIM: minimum available
- Motor truck cargo: $100,000 limit / $1,000 deductible (unless advised otherwise)
- GL (when quoted): 100% for-hire trucking income; no warehouse or customer-accessible premises; no installation/set-up work; no oil/gas field work; no other businesses operated; limits $1M occurrence / $2M aggregate

**Defaults (use unless the app/request says otherwise):**
- Commodity: Consumer Goods → Other Consumer Goods
- Smart Haul savings-notification email: the insured's email (never the agent's)

**Always ask or extract per quote (variable):** whether the owner is also listed as a driver, truck market value, radius of operation, annual mileage, driver license numbers/states + 5-year accident/claim/violation history, currently insured?, current business policies, policies to purchase through Progressive, ELD/telematics provider, federal filings required, commodity (confirm the default).

## Output Contract
- Progressive quote number (e.g. CA118213447)
- Quoted premium
- Confirmation that the correct person is listed as business owner

## Operating Rules
1. **Quote only.** Never bind, issue, e-sign, or take payment unless the user explicitly approves that exact action.
2. **Government IDs stay transient.** Driver's license numbers come from the application for the live quote only — use them in the browser task, never persist the values in the skill, memory, or logs. The log records *where* the answer came from (e.g. "ACORD driver table"), not the value.
3. **Log every question.** Each new underwriting question and its answer goes into `references/qa-log.md` with date and client, so the queue only grows.
4. **Static vs variable.** After each quote, review the log with the user: mark answers reusable across submissions (static) vs per-client (variable). Only static answers may be reused without asking.
5. **Never invent application data.** If the app doesn't state something and it's not in the log, ask Jake — don't guess.
