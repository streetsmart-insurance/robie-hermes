# ROBIE Worker Rules

**Source:** manual operations as Robie (Ralph) on 2026-09-14, under Carlo Ferrara's direct supervision.
**Status:** DRAFT — every rule below needs Carlo Ferrara's explicit review and approval before it becomes mandatory behavior. This document is documentation only; it changes no Production behavior.

## How to read this document

- **Normative rules (Sections 1–18):** behavior Carlo approved during 2026-09-14 manual operation. These are the rules future automated workers should follow.
- **"Needs Carlo's call" (Section 19):** contradictions, unresolved questions, and unproven items. Do not promote these into mandatory behavior without Carlo's decision.
- **Dated operational appendix (Section 20):** a snapshot of facts true on 2026-09-14 — incidents, counts, pending items. These are evidence for the rules above, not permanent rules themselves. When they conflict with a normative rule, the normative rule wins and the conflict goes to Section 19.

Every claim follows Carlo's evidence contract: **BUILT** = ran and proven, with the check named; **DESIGNED** = written, never proven; **UNVERIFIED** = no proof available.

Related documents:

- `~/workspace/robie-manual-ops/playbook.md` (running manual-ops log; becomes the build spec for these rules)
- `~/workspace/robie-manual-ops/policy-change-worker-rules.md` (policy-change SOP detail)
- `docs/CERTIFICATES_INTAKE_HANDOFF.md` (certificate intake handoff)
- `docs/HELLO_INTAKE_HANDOFF.md` (hello-inbox handoff)
- `docs/INBOX_TRIAGE_PHASE1.md` (inbox triage phase 1)

---

## 1. Cardinal rules (never break)

1. **ROBIE never deletes a policy.** No exceptions.
2. **The four verification workers never contact clients** — by email, call, or any automated message. Carriers and mortgage companies may be contacted; insureds are not. Client-facing contact is relayed through the assigned CSR/producer (e.g., client callbacks from voicemail notices go to the CSR, not to Robie).
3. **Never bind a quote.** Quoting requests quotes only; binding is a separate authority.
4. **Never cancel a policy, never move money, never pay a bill** without Carlo's separate, explicit authority for that exact action.
5. **Never guess an ambiguous match.** No applicant ID, account, person, or policy is guessed or invented to unblock a job. Stop and ask (in chat, or flag for human review) instead.

## 2. Send approval and the operational sender

1. **Nothing sends without Carlo's exact-text approval,** unless a standing, explicitly approved automation covers that exact send. Carlo must see the exact recipient, subject, body, and attachments before the send.
2. **Operational email goes from `robie@streetsmart.insurance`,** not from Carlo's account. (The Monday headshot requests went from Carlo's account by mistake; do not re-send them just to fix the sender.)
3. **Carrier acknowledgments prove receipt only, not completion.** Check destination evidence, not just a local send/API success result.
4. **"Success" from an intermediary is not delivery proof.** Zapier HTTP 200 / "success" only proves Zapier accepted the payload. An EZLynx note exists only when it is read back in the EZLynx Activity feed. Gmail label `SENT` proves the email left the mailbox — confirm it arrived only with destination evidence where it matters.

> **Tension flagged for Carlo (Section 19.1):** Carlo also directed (2026-09-14 ~17:05 EDT) that SOP-violation correction emails send automatically with a softened-but-firm tone, cc'ing the team lead (Sandy). That directive coexists uneasily with rule 2.1.

## 3. Evidence standard — "done" requires destination proof

1. A step is **done** only when the destination system shows it: the EZLynx note in the Activity feed, the document in the account Documents list, the carrier portal showing the change, the email verified delivered/read where verifiable.
2. When destination evidence is unavailable, the answer is **UNVERIFIED** — never "complete," "verified," or "confirmed."
3. Label every claim: **BUILT** (ran with proof), **DESIGNED** (written, never run), **UNVERIFIED** (no proof). Do not write verified/enforced/active/confirmed without naming the check that would fail if it weren't true.
4. Report failed and abandoned approaches and why. A report with only successes is incomplete.
5. If inferring rather than checking, say so, and say what would confirm it.
6. If a request is wrong, impossible, or a bad idea, say so plainly instead of doing the nearest easy thing.
7. **Failure reports must include the path to completion** — what failed, what was tried, and the next step that would finish it. A graded failure without a completion path is incomplete work.

## 4. Identity and system-of-record rules

1. **Never guess or invent an applicant ID.** Resolve via the shared applicant lookup (see below); if the lookup cannot resolve it, stop and ask rather than guessing.
2. **Carlo's Contacts, EZLynx, and agency systems beat public-web results.** When Carlo says "check our directory" for a carrier/MGA/vendor contact, he means his Google Contacts — not EZLynx Agency Admin → Manage Carriers (rating carriers only), not the public website. (Proven 2026-09-14: XS Brokers was in his contacts as accounting@xsbrokers.com / (617) 890-4209; EZLynx had nothing.)
3. **EZLynx Activity is the source of truth** for in-flight carrier/policy work. Gmail is secondary because team members may email outside Robie's mailbox.
4. **Use EZLynx global search** to resolve names to accounts/policies until the daily directory is live.

### Daily Applicant Directory (ordered 2026-09-14, ~16:15 EDT)

- EZLynx saved report `Daily Applicant Directory - Robie Mapping`, SavedReportId `115153`.
- Intended contents: Applicant ID, Applicant Name/DBA, email, phone, policy numbers.
- Intended delivery: emailed to `robie@streetsmart.insurance` every morning, parsed into a shared name→applicant-ID lookup for every worker.
- **UNVERIFIED as of 2026-09-14:** at last check the report returned only 15 rows under an `08/01/2026–08/31/2026` filter; full row count, filter removal, daily delivery, and parsed lookup ingestion are all unproven. Do not claim this directory exists as a usable full report yet.

## 5. Duplicate-send prevention (standing rule, Carlo 2026-09-14 ~20:40 EDT)

Before **any** resend:

1. Open the sending mailbox's **Sent** folder.
2. Search for the exact subject/message.
3. If it is present in Sent, **do not resend**.
4. A timeout or unclear confirmation is **not** proof of failure.
5. When in doubt, stop and ask in chat — never retry blindly.

This rule exists because of the recorded incidents (see Appendix): Hartford duplicated 2026-09-09; AmTrust and Travelers audit emails duplicated 2026-09-14; ~16 Junk Moor/TCR South self-emails 2026-09-14; a timed-out correction send was found exactly once in Sent. The manual check-Sent rule is proven process; the underlying automated duplicate-alert/hourly-report defect was **not** fully fixed in ROBIE as of 2026-09-14 — do not claim automated dedupe is built because this rule exists. (Hourly-report generation work also spans the separate `renewal-automation-system`; do not imply `robie-hermes` controls all of it.)

## 6. EZLynx note discipline (standing rule, Carlo 2026-09-14 ~17:30/17:35 EDT)

1. **Every time Robie touches an EZLynx account, put notes in there** documenting what was done.
2. Whenever an email goes out about an EZLynx policy/account, log a note on that account with: date, sender, recipient, subject, and a summary of the request.
3. Notes must be readable back in the account Activity feed after saving; the read-back is the proof.

## 7. Queue discipline

1. **All worker queues are incremental:** each day adds only new rows. Never rebuild destructively. Preserve statuses, evidence, notes, and history across runs.
2. **Read persistent tracker/worklist state before raw daily CSVs** so prior work is not lost. (A 2026-09-14 tracker-wipe incident was traced to `build_all_trackers.py` rebuilding from the wrong mortgagee source; the local script was patched to carry forward manual columns — confirm the fix belongs in this repo before porting it.)
3. If a current queue email is missing, **use the latest prior file and label the fallback explicitly** — never skip silently.
4. Morning source emails generally arrive from Applied Reporting around 6:00–6:30 AM Eastern.
5. Deduplicate on workflow-specific keys.

## 8. Contact order and calling

1. **Portal → email → call,** across all carrier/mortgage-company outreach.
2. Calls go to **carriers and mortgage companies only — never clients/insureds** (four-worker boundary).
3. **Every call is recorded and logged** (Bland AI; Robie caller ID +17322986745; inbound callbacks route to the office team). Call scripts must be Carlo-approved before any real call.
4. **Caller introduction** (Carlo's recorded decision 2026-09-14): say "producer assistant with StreetSmart Insurance," or "Carlo's assistant" when personally directed. Never say "AI."
   > **Flagged (Section 19.2):** this conflicts with an older AI-identification rule; legal/compliance needs Carlo's call.
5. **When carrier voicemail answers, leave a useful callback message** rather than hanging up.
6. Use Carlo's EZLynx directory/contacts for phone numbers, not public web searching.
7. Assigned-risk NJCRIB audits go by **email** to the servicing carrier (no portal). JIMCOR and NJM have no portal — do not put them on portal-login request lists.

## 9. Portal login onboarding (Carlo's process, 2026-09-14)

For each carrier/vendor portal:

1. Carlo supplies or signs in with his login **once** through a secure method.
2. Create a **separate Robie sub-user** where the portal permits it.
3. Save Robie's credential to Secret Manager / Secure Vault — **never in chat or repo files**.
4. Update the shared login inventory.
5. If sub-users are unsupported, **report the blocker** rather than treating Carlo's login as the permanent worker identity.
6. **Warn Carlo before any password reset.** The Progressive admin login was locked during a reset attempt.

## 10. The four verification workers

### 10.1 Manual renewals (EZLynx report 4247)

- Work renewals **30–45 days before expiration**.
- **Follow up every 3–5 business days** until resolved; log each follow-up (date + channel) in the tracker.
- **Contact order: portal → email → call.** Check whether the policy is still live before chasing it. Do not duplicate teammate remarketing work.
- **Progressive BOR-takeover rule:** Progressive policies set up manually in EZLynx are broker-of-record takeovers — at renewal they **should** download automatically. First check whether the renewal downloaded. If downloaded → no manual work, record done-by-download. If not downloaded → chase it / work it manually.
- Carlo cleared renewal-document upload and manual-renewal processing in EZLynx. Still in force: no delete, no bind, no client contact, no money movement.
- **Renewal filing standards** (from the agency renewal SOP): Discussion title `Renewal Manual / Submission Center`; Automation Center `Manual Renewal`; document folder `Renewal Offers/Declarations`; labels `Renewals` (plus `TO CONFIRM` during review). Renewal documents are temporarily named exactly `renewal offer` pending a permanent filename convention (Section 19.5).
- Daily queue adds only new accounts; do not recreate the whole report.
- **EZLynx side effects are real:** keying the Kotarja renewal auto-triggered the EZLynx Renewal Center — reassigned the Renewal Manager to Jazmin Molina and emailed the client "Your Insurance Renewal Is Ready." Flag unexpected automation side effects to Carlo immediately.

### 10.2 WC audits (EZLynx report 4246 — the Workers Comp Renewal Audit Queue)

- **Use report 4246, not the broader audit file** (the 448/449-row audit file is not the working queue).
- Trigger: **30–45 days after the WC renewal**. Start follow-up on **day 30**, repeat every **3–5 business days**. **Day 45 is a firm escalation:** call the carrier audit desk and cite non-compliance surcharge risk.
- **Portal → email → call.** Obtain papers from the carrier and get them to the agency/team; **do not contact the insured.**
- Working workflow text (updated 2026-09-14): *"Please check with the carrier whether an audit is due at this time. We are looking to avoid an audit non-compliance surcharge. Obtain the audit documents from the carrier (portal first, then email, then call) — do not contact the insured."*
- Checklist item 1: *"Obtain audit documents from the carrier and complete the audit."*
- Activity Master report: `Policy Audit Verification Notes`, SavedReportId `115145`; it showed 48 open notes, then 138 activities on the 8/15/2026–9/14/2026 window. Weekly refresh schedule `audit-report-date-refresh` rolls the window to the last 30 days every Monday 08:00 ET (schedule defined 2026-09-14; first run unproven).
- Hartford audits go through **EBC** (portal-first; the EBC OTP screen was blocked for automation on 2026-09-14 — email + call fallback stands).
- > **Flagged (Section 19.3):** audit checklist item 5 still says "Pay for the audit and Endorsement," which conflicts with the no-money-movement rule. Needs Carlo's call.
- Proof standard: audit verification must confirm the **carrier received payment** — lender/third-party portals alone are not sufficient (also applies to mortgagee payment verification).

### 10.3 Mortgagee verifications (working queue: daily Applied Reporting email `Home Flood Renewal Queue - ROBIE`)

- **Working source is the daily Applied Reporting email** `Home Flood Renewal Queue - ROBIE`. Ignore the stale four-row closed `Mortgagee Verification Queue - ROBIE`.
- Scope: **Home, Flood, and Dwelling Fire** when present.
- Work **30–45 days before renewal**; after the renewal is available, **follow up every 3–5 business days** until payment is confirmed.
- **Escrow source of truth is the Policy Summary `Policy Payor`:** `First Mortgagee` → Yes; `Insured` → No; `Other`/blank → Unknown. **Never infer escrow from Overview `Billing Type`** — recorded comparisons disagreed.
- **No mortgagee on file → record it, blank escrow, no lender follow-up.**
- **Direct billed → skip the lender step.** CSR handoff only if evidence shows the client paid something themselves.
- **Lender portal "paid" is insufficient; confirm the carrier actually received payment.**
- **Portal → email → call;** calls only to carriers/mortgage companies, never clients.
- Lender/HO portal uploads are approved for **payment verification only** (read-only enrichment elsewhere; EZLynx document uploads remain scoped to the manual-renewal worker).
- **Agency bill:** obtain the offer early, collect in advance, remit — unless the carrier accepts direct payment for agency-bill policies.
- Daily incremental queue; enrichment columns preserved across runs.

### 10.4 Policy changes (Reports 5.0 where possible; old report 4359 carried stale/buggy history)

- Keep the daily queue to the **last 90 days**; genuinely open items older than 90 days are exceptions for Carlo.
- **First follow-up 7 days after the original change/email** (unless the carrier gave a shorter window), then every **3–5 business days** until confirmation. Urgent deadlines, carrier questions, or declines trigger **same-day escalation**.
- Before any vehicle add/change, **decode the VIN and validate its check digit**.
- **Close process (Carlo 2026-09-14 ~16:30 EDT):** confirm the change actually happened in EZLynx (the destination policy state, e.g. carrier declaration page or endorsement — not merely a download indicator) **before** closing. Close the **existing purple change-request record** — never create a duplicate. Assign the closed record back to the **original team member**. If verification fails, leave it open and report the blocker.
- **Tasks and policy-change records are different.** The purple change-request record is what gets closed; closing it is the confirmation.
- Edit discussion titles to describe the work; **never leave `CHANGE ME`**.
- **Avoid unnecessary re-saves** — re-saving can retrigger Automation Center, duplicate tasks, and duplicate client messages.
- Log every touch in EZLynx notes, tracker notes, and the digest.
- For EZLynx writes, Carlo required **double or triple verification that the account is correct** before writing (relevant checks: allowlist, policy owner, expected insured name, optional Documents corroboration).
- Carlo's inbox remains a triage surface: incoming email → match to the correct policy → route to the responsible CSR as a training example (e.g., the Santiago/Candor 2815 N Wood Ave item matched to J&J/Scottsdale `DFS4002542`, not RT Specialty `OLF-0002713`).

## 11. Certificates

- Dedicated mailbox: **`certificates@streetsmart.insurance`**. Carlo extended cert-worker mailbox scope to **`accounting@streetsmart.insurance`** (2026-09-14 ~16:33 EDT) — scope stays with the cert worker only.
- Approved division (2026-09-14): **Robie saves requests and creates necessary tasks; Steffany Canales issues certificates. No automatic issuance.**
- **Written requests only.** Verify policy status, forms, endorsements, holder details, and red flags.
- **Non-standard wording needs Producer/Account Manager approval.**
- **Never issue unsupported proof.** Attach actual carrier endorsements.
- **Third-party requests require insured approval.** Most "ambiguous" cert emails are third-party requests — open the email, find the insured/client inside (body, forwarded thread, attachment); the sender is rarely the client. Third-party items are often correction/rejection notices (route to Steffany as reissue work).
- **EZLynx Client Center notifications** (`cplive@ezlynx.com`, "A change request has been submitted") are **real client certificate requests**, not noise. Client name, email, and the "Message to Agent" are inside the email.
- Pending more than **seven days** escalates to Carlo.
- Certificate matching must use a **complete applicant roster**, not only recent activity (the 2026-09-14 directory missed names).
- **Dry runs must not mutate dedupe state** (a 2026-09-14 bug did; fixed locally — confirm the fix belongs in this repo before porting).
- "HTTP 200" or Zapier "success" is **not** destination proof (see 2.4).

## 12. Hello inbox and document retrieval

- Follow the `Mail, Hello & Document Retrieval (StreetSmart SOP)`:
  1. Monitor daily sources; check urgency/duplicates.
  2. Identify by insured name, policy number, contact info, Activities, and Documents.
  3. Upload to the correct account/policy/workflow with agency naming and labels.
  4. Reuse existing work; create workflow/task only when necessary.
  5. Add a complete activity note; update status tracking.
  6. Complete/archive only after filing is complete.
- **Alejandro escalation:** anything uncertain goes to Alejandro.
- **Cancellation handling (manual policies):** always `Cancel Confirmation`; return premium from the notice or `$0`; check history/notes for duplicates; EZLynx normally creates the workflow to the account CSR; **never close a task yourself** — CSR/AP reviews.
- **Downloaded-policy cancellations:** if the cancellation document already exists, archive; otherwise retrieve the carrier NOC and file it. **Do not key a cancellation manually when it should download automatically** — verify the download arrived instead (e.g., Gutierrez `NJH1031130`).
- Ascend tasks go **dynamically to the CSR on the account** — never invent an assignee.
- **`email received` label** on every uploaded email/correspondence.
- **After-cutoff rule:** schedule follow-up next day; recorded cutoff is **4:30 PM EST**.
- Messages with multiple documents/accounts: handle each separately.
- Promotions/ads: label, mark read, archive.
- Interim document filename: `YYYY-MM-DD_ClientName_PolicyNumber_DocType_Detail.pdf` (temporary pending Nicole's real convention — Section 19.5).
- **Voicemail/missed-call notifications are not noise:** they are missed calls requiring a client callback — route to the assigned CSR/producer (workers never call clients). Duplicate canned-response copies of the same voicemail are noise.

## 13. Inbox triage

- Scan **All Mail** (Carlo receives batched delivery). Read-only trial before live filing.
- Match **direct sender email first**; otherwise policy number/insured name.
- **Fuzzy or multiple matches stop for human review** — never guess.
- Create tasks **only when necessary** or when Carlo still needs to act after mail is marked read.
- Zap `Inbox Triage → EZLynx Follow-up Task` (ID `380066050`): Zapier reported it ON, but destination checks on 2026-09-14 showed accepted webhooks **did not** create EZLynx notes. **Do not rely on the Zap path until destination verification is built.**

## 14. HITL (human-in-the-loop) ladder

- Generic loop (Carlo 2026-09-13 ~12:44 EDT): (1) on failure, ask Gemini first and the job keeps going on Gemini's fix — **up to 2 attempts**; (2) if it keeps failing, loop Carlo in via Chat with honest wording (what failed, what Gemini tried); (3) if Carlo doesn't reply in **30 minutes, kill the job** (fail closed, final summary). Carlo replying KILL kills immediately; any other reply gets exactly one final retry.
- The loop never invents success: a step counts as recovered only when the job's own retry reports success.
- **Proven 2026-09-14 19:20:08 EDT:** Carlo's reply "Tell me what is needed" reached a probe, proving the **real email** HITL path works. Caveat: the probe job was synthetic (`hitl-email-probe-20260915`) — do not turn a synthetic probe into a real policy action. Chat-reply watcher/runtime hookup remained pending.

## 15. Cancellation triage (org-wide directive, Carlo 2026-09-14)

- Sweep **all organizational mailboxes** for cancellation/non-renewal notices; match to EZLynx; key the cancellation into the system.
- Downloaded-policy cancellations: check for automatic download **before** manual keying.
- Avoid duplicates: if already keyed, add a source note instead.
- **Pay-or-cancel items escalate urgently** (already-passed or near-dated effective dates).
- No client contact, cancellation execution, or money movement without separate authority.

## 16. SOP-violation correction emails (Carlo 2026-09-14 ~17:05 EDT)

- When Ralph catches a team member violating an SOP (with evidence), **send the correction email** to the violator(s) and **cc the team lead (Sandy)**.
- Always **reference the specific SOP** not being followed. Tone: **softened but firm**; the SOP itself is non-negotiable.
- First use 2026-09-14: Hartford-via-email violation (Arellano WC) → email from carlo@ to Lenin, cc Sandy; ISCA 24-day follow-up lapse → softened email to Angie Valladarez + Erika Palacios, cc Sandy, citing the 4-point policy-change SOP.
- > **Tension flagged for Carlo (Section 19.1):** this auto-send directive sits against the exact-text send-approval rule.

## 17. Quoting

- **Quotes only — never bind.** Binding is a separate authority.
- Submit a quote request only on Carlo's explicit approval of the exact request.
- Record separate labels for state tax ID vs. federal EIN; do not mix them.
- Quote results are comparisons only; Carlo decides next steps.

## 18. Daily build-spec discipline

- Carlo requested a **daily build specification** after completed operational work: manual work is **training input**; durable behavior must be promoted through **repo rules/code** via the governed path (branch → PR → CI → Test → Carlo approval → Production).
- **One-off completion is a stopgap; the goal is dependable batch operation.** Judge work by whether it moves ROBIE forward.
- Each worker chat's manual knowledge should become **shared repo-backed behavior**, not remain isolated in chat.
- The daily digest must **separate done / not done / pending with reasons** and use destination evidence.

---

## 19. Needs Carlo's call

Not yet decided or proven — do not treat as mandatory behavior:

1. **Auto-send vs. exact-text approval:** Rule 2.1 says nothing sends without Carlo's exact-text approval; Section 16 says SOP-violation emails auto-send. Confirm whether the SOP-violation class keeps its standing auto-send exception.
2. **Caller identity wording:** "producer assistant with StreetSmart Insurance" (never "AI") vs. any legal/compliance AI-disclosure requirement. If the repo still contains the older AI-identification wording, it must be reconciled.
3. **Audit checklist item 5** ("Pay for the audit and Endorsement") conflicts with the no-money-movement rule.
4. **Certificate delivery to insureds:** current split leaves issuance/delivery with Steffany. Confirm whether Robie may ever deliver certificates directly.
5. **Permanent document filename convention:** interim pattern in Section 12 pending Nicole's real convention.
6. **Daily Applicant Directory (115153):** full row count, date-filter removal, daily delivery to robie@, and parsed shared lookup all **UNVERIFIED**.
7. **Automated duplicate-send fix ≠ check-Sent rule:** the manual rule is proven; automated dedupe was not fixed in ROBIE on 2026-09-14.
8. **Zap 380066050:** webhook accepted both fires, but no EZLynx notes landed. Needs the Zap run-history review in Zapier.
9. **Reports 5.0:** Discussion text field hidden for Carlo1 in 5.0 (only numeric Discussion ID exposed); conflicting reports on rolling-last-7-days behavior. Do not encode unproven report automation.
10. **API surfaces still unproven:** e.g., `POST /DiscussionApi/discussions/v1/notes` and some response-field shapes — treat as **UNVERIFIED**.
11. Whether quoting rules belong in this general document or a separate quoting section.
12. Whether dated operational examples belong in the normative body or stay in the appendix (current choice: appendix).
13. Report/accountability/hourly-mailbox work spans other repos/systems (e.g., `renewal-automation-system`); do not imply `robie-hermes` alone controls all behavior.
14. Junk Moor / TCR South removal was ordered, but destination completion must be independently evidenced (the ~16 self-send loop was the duplicate-send bug, not proof of removal).
15. Full login inventory and portal statuses are operational state, not permanent rules: include only the onboarding procedure (Section 9) and blockers at a high level — **no credentials, tokens, passwords, one-time codes, or secret values** in this document or the PR.

---

## 20. Appendix — dated operational facts (2026-09-14)

Snapshot only. These explain where the rules came from; they are not permanent worker rules.

- **Mortgagee enrichment result (2026-09-14):** 248 unique policies — 90 escrowed, 84 direct billed, 21 no mortgagee, 37 inconclusive, 16 blocked.
- **Manual renewals:** 3 Progressive rows deprioritized until the admin-33617 lockout resolves — D&C Cruz Trucking `02796124`, Pinnacle Parts and Services Corp `865847509`, Advance Property Maintenance Services LLC `PGR973175709`.
- **Audits:** Activity Master `115145` showed 48 open notes, then 138 activities on 8/15–9/14/2026 after the classic-report refresh (verified after save + reload). Working queue: 11 original policies + 20 report accounts = 31 active after Junk Moor and TCR South were excluded per Carlo. The 22 report accounts were added to `audit-working-queue.csv` (before: 11, after: 33, added: 22); Junk Moor has no WC policy in EZLynx and TCR South's note referenced an inactive policy.
- **Applicant directory:** `115153` returned only 15 rows under an `08/01/2026–08/31/2026` filter; daily delivery and complete roster **UNVERIFIED**.
- **Bulk audit emails:** 6 grouped emails from robie@ covering 15 policies (AmTrust, Travelers, Pie, Liberty Mutual, FMI, Markel). **Duplicate bug:** AmTrust and Travelers each received the email twice (first batch's confirmation parsing failed, retried blindly). 9 policies had no published audit email (NJM x4, PA Lumbermans, Selective, InterGUARD, NorGuard, NJ Casualty) — queued for 2026-09-15 9 AM calls.
- **Hartford 4 audits** (Milzaz `13WECAZ5JUL`, Norris `13WECAY8WDT`, Jay Vijay `13WECAZ4ZNZ`, Cocoa Beach `13WECAD7UT5`): audit-status email sent from robie@ to `agency.service@thehartford.com` (approved by Carlo); Hartford EBC OTP entry blocked for automation (six identical PIN boxes); fallback carrier calls 2026-09-15 9 AM ET. Notes added to all four accounts' EZLynx feeds.
- **NYSIF:** Szwarc Construction `20793527` audit `8999452` "Released to PAD"; Mar Design `26846071` current audit not complete — audit letter saved locally (141,437 bytes, 3 pages), prior-policy final audit posted 7/17. EZLynx note added for Mar Design.
- **Shoreline:** Ascend `$3,528.22` wire (policy `2040593`) applied 2026-09-14 8:37 PM by Carlo Ferrara to invoice `I0HBLJHYIJ`; row re-read as `Applied`, applied-on `Sep 14, 2026`. Carlo authorized pulling Ascend one-time codes from his connected mailbox for the nightly run.
- **Ascend nightly run:** cron `daily-ascend-pay-batch-check` moved from 4:00 AM to **7:00 PM ET** per Carlo. 19:00 run failed (Ascend code expiry, no EZLynx session); rerun ~19:56 completed read-only: Ascend 7,016 entries / $10,920,771.28 (only flagged item: Shoreline unapplied, later applied); Applied Pay 4 outstanding Acknowledge items (DJ Movers $194.00 + $153.49, Segura $2,804.00, Raymond Suarez $796.21; Tammy Breaker and Furniture Express already acknowledged by StefReyes); Daily Balance $444.33 as of 9/12; 27 payouts 8/1–9/14, all recaptured $0.00. Ascend has no native report module or scheduled-report feature.
- **Cancellation sweep:** 17 genuine carrier cancellation/non-renewal notices across 5 mailboxes (results in `~/workspace/robie-manual-ops/runs/cancellation-sweep-2026-09-14.json`). Pay-or-cancel urgent: Selective/Jehova Sama (eff 9/12, passed), USLI/Sam Apartments (eff 9/15, $438), ABA/Sweat It Out LLC (eff 9/16), Berkley/Extreme Mini Golf (eff 9/24), Sterling/Paul Juska (eff 9/28). No EZLynx keying completed by the sweep itself.
- **Policy changes:** 15 tracker rows reviewed — 1 complete, 4 cancelled, 2 renewals, 1 active change (Panda Kitchen Selective WC payroll $60,000 for class 8018, follow-up needed), 7 no recent change activity. ISCA Contracting `BDG-312624001`: 24-day follow-up lapse, correction email sent from carlo@ (Gmail ID `1a0a1c541903ba8a`), follow-up email to XS Brokers (Amy O'Donnell) sent from robie@ (Gmail ID `1a0a1d2cd91afea9`) and documented in EZLynx account `82861889`. Arellano BOP `ART3000087030` done/confirmed in EZLynx; Arellano WC `13WECAT1F8T` (Hartford) still open — Lenin re-submits via EBC; Santiago 2815 N Wood Ave matched to J&J/Scottsdale `DFS4002542`, task to Lenin due 09/16/2026. Family Tradition Plumbing `CAPI082129`: confirmed YES — adds 2007 Isuzu NPR VIN `JALB4W16177400295` and 2016 Chevy Express VIN `1GB0GRFF2G1138353`. Erasto Contreras and KJSD change-verifications done (tasks already completed by team); John Guarini failed verification (policy shows 2500 vs requested 2500/VIN mismatch) — needs Carlo's call.
- **Kotarja renewal:** keyed in EZLynx (applicant `148235525`, policy `25AWA1265-01946`, 10/15/2026–10/15/2027, $4,886.25, pending/not bound); PDF filed as `renewal offer` with labels created. Side effect flagged: EZLynx Renewal Center emailed the client "Your Insurance Renewal Is Ready."
- **Rosales Labrada correction:** cancellation was already keyed 08/03/2026 by Sandy Santana (Cancel Confirmation, eff 7/24/2026, non-pay) — the 9/14 Berkley email was a late notice copy; only a carrier-notice source note was added.
- **Par-Troy quoting:** WC-only quote request `0000377802` submitted (approved by Carlo); 6 premiums returned (PIE $14,828 lowest — below Hartford's $19,214 proposal), Liberty Mutual + Nationwide declined. Federal EIN saved to the EZLynx account (applicant `222032074`) with activity note. Nothing bound.
- **Olivia / Top to Bottom:** reply sent (Gmail ID `1a0a1fc65b40a0a3`) to `billing@toptobottominsulation.com` with Scottsdale policy `CPS8328902` attached; policy filed to EZLynx Documents + account note (applicant `219608909`).
- **Mike Swartz / Express Automotive:** draft created in thread, Carlo to attach quotes and send; Ategrity GL PDF filed to account Documents; Great Bay property PDF pending download (blocked on mailbox/CLI limits).
- **Green Lion / Lawn Buddies:** GAIG case note added (applicant `21587333`, verified in feed); GAIG serial-number call scheduled Tue 2026-09-15 08:35 EDT; certs to `3401.VerifyInsurance@gaig.com` pending.
- **Asphaltech** (applicant `126827834`, found via EZLynx search — absent from local directory): Robin Brown reissue note added, verified in feed.
- **Straight to the Source** (applicant `220890322`): Assurant/Dakota Financial note added directly, verified in feed.
- **Zapier:** webhook reconnect completed; two fires returned HTTP 200/"success" but destination checks showed **no** EZLynx notes landed — do not trust "success" without destination read-back.
- **Merchants:** saved login accepted, MFA then blank page (Cardone Electric `CAPI075976` check blocked).
- **Travelers:** login captured via secure card 19:14 EDT; live sign-in test pending.
- **Progressive:** admin login locked (`ForgotPasswordLocked`); Nicole emailed to unlock and verify/create Robie sub-user `33617r`.
- **Neptune:** credential rejected, reset broken; Nicole emailed to re-provision.
- **JIMCOR:** separate follow-up email to Nicole sent (no confirmed portal).
- **Intuit/QBO:** blocked by bot detection (`Access is denied`); no reports pulled.
- **Junk Moor / TCR South:** excluded from audit queue per Carlo; ~16 duplicate robie@→robie@ self-emails 19:04–19:56 EDT (worker blocked on missing applicant IDs; same duplicate-send bug class).
- **HITL email path:** Carlo's reply "Tell me what is needed" at 19:20:08 EDT proved the real email path live (synthetic probe only).
- **FAS Transport:** USLI GL `GL 2072630` (eff 04/17/2026) confirmed from policy PDF; Excess/Umbrella number **not found** — reply to US Premium Finance on hold pending Maria Bara's confirmation.
- **Byond Transportation:** 10-day Notice of Intent to Cancel (notice 9/11/2026, cancel 9/23/2026), total $395.73; no EZLynx filing completed.
- **Professional Energy Savers** `L259005077-0`: audit payment to direct collections (Tuscano); noted locally only.
- **Maricela Mendoza referral:** task created, assigned to Jazmin Molina, account note logged (applicant `21696180`); no email sent.
- **Inbox trial:** 50 messages classified (22 internal, 11 carrier/third-party, 9 vendor noise, 4 client, 4 system); 13 held for human review. CSV at `~/workspace/goals/inbox-triage-trial-carlo-s-inbox/hidden_files/first-pass-classification.csv`.
- **Hello triage:** 151 unread read-only pass; report at `~/workspace/goals/inbox-triage-trial-carlo-s-inbox/files/hello-inbox-triage-2026-09-14.md`.
- **Cert triage:** 40 messages, last 3 days, read-only; three items need Steffany (Robin Brown/Asphaltech, Assurant/Straight to the Source, GAIG/Western Equipment Finance).
- **Headshot lesson:** Carlo's terse photo labels need person-to-photo confirmation before publishing (Matt vs. Mitch misassignment fixed).
- **"Our directory"** = Carlo's Google Contacts (XS Brokers proven: accounting@xsbrokers.com, (617) 890-4209).
- **Hourly worker-chat recon** ran ~hourly through 20:00 EDT; end-of-day report runonce at 21:00 EDT; robie@ inbox monitoring added to the hourly job.

## 20. Outbound email → EZLynx note logging (APPROVED by Carlo 2026-09-14 ~21:12 EDT)

Every time Ralph or a Robie worker sends an email outside EZLynx about an EZLynx policy or account, the same worker must add a note on that account in EZLynx documenting the outbound email.

Carlo's decisions, in his words:
- ~17:30 EDT: "whenever Ralph emails outside EZLynx about an EZLynx policy/account, Ralph must add an EZLynx note documenting the outbound email. The note should include date, sender, recipient, subject, and a summary of the request." First use: the Amy O'Donnell/XS Brokers email for ISCA Contracting `BDG-312624001`, logged on EZLynx account `82861889` at 5:29 PM.
- ~17:34 EDT: "yes log notes all the time."
- ~20:40 EDT: reaffirmed as a standing rule to all 8 worker chats.

Status: **BUILT as a manual standing rule.** The worker sends the email, then adds an EZLynx Discussion note by hand. Nothing is automatic.

Every note must contain: date sent, sender (e.g. `robie@streetsmart.insurance`), recipient, exact subject line, and a short summary of what the email asked for.

Applicant rule — **BUILT.** Resolve the applicant ID before writing. Never guess. Match by client email, policy number, or insured name against the directory; if it cannot be resolved with certainty, mark the email's logging as UNVERIFIED and report it — do not write the note to the wrong account.

Duplicate check — the Julio's Tree Service lesson. The same underwriter note was logged 4 times on one account — Note IDs `1126364012`, `1126383038`, `1126386695`, `1126390715` — one for each hourly "[URGENT / CSR ACTION]" email to sandy@. Before writing a note, the worker must check whether an equivalent note for the same email already exists on the account. If one exists, do not write another. Today this check is done by eye; there is no automatic duplicate detection.

Automation status — **DESIGNED, not built.** `ezlynx_writers.post_note()` exists in `robie_job_engine/ezlynx_writers.py`, but the file itself says "NOTE POST PATH UNVERIFIED" — it has never run live and sits in open PR #414 (`ralph/ezlynx-write-scope-configurable`) with failing CI. The read side is broken: all four probed API shapes for reading notes return HTTP 404, so an automatic duplicate check cannot work until EZLynx provides a way to read notes.

Open decisions on this rule (Carlo's desk):
1. Carlo's 2026-09-12 ask — "For the email report from robie can we have it clean up its emails using ezlynx api and make sure its on robie forever unless we change" — confirm or correct the interpretation (duplicate-check via API + permanent Robie-server schedule).
2. Should sent-email→note logging become automatic, or stay manual?
3. Approve an EZLynx support request to get a working note-read API path?

## 21. Document filing to EZLynx (APPROVED by Carlo 2026-09-14 ~21:12 EDT)

Which documents the workers save into EZLynx, where they go, and which method they use.

Carlo's decisions, in his words:
- 2026-09-14: "I clear that please do this" — uploading renewal documents to client applicant accounts in EZLynx, scoped to the manual renewal worker. Standing limits still on: never delete a policy, never bind/quote without his per-policy approval, never contact clients, every action in the morning report.
- 2026-09-12: "use the Documents API to push documents into EZLynx" — direction given, never proven live.

Status: **BUILT (browser, verified 2026-09-14).** Every real filing today went through the browser:
- Renewal offers filed as "renewal offer" — e.g. the AmWINS renewal offer for John Kotarja, policy `25AWA1265-01946`, Quote `534809R1-1`.
- Policy PDFs to Documents → "Policy Changes/Declarations" — e.g. the Scottsdale GL policy for Top to Bottom Insulation LLC, applicant `219608909`, uploaded by Carlo Ferrara on 09/14/2026 and verified in the Documents list. Also the Ategrity GL PDF for Express Automotive, filed to the Documents tab.
- Hello inbox SOP: cancellation NOCs are saved/renamed and attached to the matching EZLynx workflow; emails and correspondence get the 'email received' label; one discussion per correspondent account (separate discussions, not one merged thread); when no current workflow exists, the filing goes "for record."

Automation status — split:
- Document search/download via API: **WORKING.** Proven 2026-09-11 on hermes-poc-01 as SSRobie against applicant `220250093` (`GET /documentapi/documents/v1/account/{id}/document-search`).
- Document upload via API: **DESIGNED, not built.** `EzlynxApiClient.upload_applicant_document` merged in PR #295 (2026-09-12), and `ezlynx_writers.upload_document()` has a passing unit test — but it sits in open PR #414 with failing CI and has never run live against a real applicant. The folders/labels the API would land in are unproven.
- Download direction: `~/workspace/robie-manual-ops/ezlynx_docpull.py` (2,464 bytes, created 09-14 02:04) — **DESIGNED, never run.**

Open decisions on this rule (Carlo's desk):
1. Standing clearance for other doc types (cancellation NOCs, audit documents, certificates, correspondence) beyond renewals?
2. Keep browser filing as the standard, or switch to the API upload path after PR #414's CI is fixed and it's merged?
3. Which folders and labels must API uploads use, to match the browser conventions?
4. Should the download direction (`ezlynx_docpull.py`) be activated at all?


---

*End of worker-rules draft. Carlo's review and approval required before any rule is enforced.*
