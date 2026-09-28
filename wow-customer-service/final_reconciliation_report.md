# WOW Customer Service — September 2026 Final Reconciliation Report
# Status: 2026-09-27 (overnight build) — Cross Sell complete; pending only Alejandro's reply (rows 38, 39)

Carlo's standing rules applied throughout: payroll is unavailable, so findings are credits to add/reconcile, not proof of missed payments. Labels and notes are claims; every disposition below is verified against EZLynx (browser, PolicyApi, or Discussion/Document APIs) unless marked PENDING/WEAK. Labels pay separately, but contradictory labels cannot both earn credit ("we can't contradict ourselves"). A rewrite earns no new-business credit; AutoPay, All Star Call, or Google Review credit can still be earned on a rewrite row only if independently supported.

## Carlo's rulings (2026-09-27)

- **Lupano:** counts as a new policy — the carrier endorsed new umbrella coverage onto the home policy. Two WOW rows describe one $170.55 activity: ONE credit.
- **J Swat:** cross-sell, not new customer.
- **Ashley & Troy Huntley:** excluded entirely.
- **Dennis Korkowski:** rewrite — no new-business credit.

## 1. Google Reviews — COMPLETE

- Methodology saved to repo (`google-reviews-methodology.md`); team notified.
- September: 8 reviews reviewed, 5 direct-name credits ($50).
- Attribution rule: "If they say someone's name, we give them the credit." Ambiguous reviews routed to staff via distribution list.

## 2. Autopay Setup (35 rows)

- **REJECTED (6):** notes contradict the label (paid in full / mortgage billed). All 6 by Karla Brown.
- **VERIFIED (1):** Stopka / Progressive 879834477 — EFT confirmed in ForAgentsOnly portal, paid to date, active.
- **PORTAL PENDING (9):** Travelers (5, bot-blocked in sandbox — Dusty brief ready for the box), Bristol West (1, no saved credentials), Liberty Mutual (1, credentials rejected), Plymouth Rock (2, check ran — result to be recorded).
- **WEAK (19):** notes don't mention autopay — likely mislabeled. 3 claim autopay but need verification (DJ Movers, East Coast Framers, Mansukh); 16 don't mention autopay at all.
- Cross-check: row "Cabrera Travelers Home 619315196-633-1" says paid in full — its AutoPay Setup label is wrong.

## 3. New Customer CSR (43 rows) — ledger complete

Full row-by-row ledger: `ncsr_final_ledger.md`.

- **VERIFIED: 27 rows** — independently confirmed in EZLynx as issued September new business (PolicyApi active statuses, document evidence, or browser verification).
- **COUNTS (Carlo ruling): 1 credit** — Lupano rows 19+37, one $170.55 umbrella activity.
- **OCT-EFFECTIVE: 3 rows** (Cabrera home 619315196-633-1, Cabrera auto 619315117-206-1, Gangwar home NJA198092) — sold in September, coverage starts October. Credit is Carlo's call. Kelso (HONJ052827) carries an August-effective timing flag.
- **REJECTED: 7 rows** — Murray King (existing client), MCM Home Services (existing client — cross-sell instead), J Swat (cross-sell per Carlo), Huntley (excluded per Carlo), Gutierrez/Torres (rewrite), Korkowski x2 (rewrite).
- **PENDING: 4 rows** — Puma (browser batch 2), 1812 cyber row 38 (Alejandro), Mendieta (Alejandro), Jaguar (browser batch 3 prior-client check). 1812 row 41's policy is verified; only the row-38 sameness question is pending.
- **Confirmed range: 28 credits minimum** (27 verified + 1 Lupano), up to 35 at Carlo's discretion (3 Oct-effective + 4 pending).

## 4. Cross Sell (13 rows) — ALL RESOLVED (Carlo's rulings 2026-09-28)

- **CLEAN (10):** MCM Home Services, Caruso, J Swat, Top Notch Lawn, EG Smart Home, Puma, SK Direct, Mansukh, Jaguar — all issued, active, existing clients. **Top Notch Tree Service** — September BOR transfer of existing WC (renewal term 11/7/26–11/7/27); counts per Carlo ("even though it is a BOR, that's how we got it"). (Puma: binder-date vs term mismatch and named-insured question flagged for accounting. Mansukh: garage/dealers + property package, not WC. SK Direct: named-insured endorsement open.)
- **OCTOBER (1):** Murray King — Foremost dwelling fire $3,316, PENDING, eff 10/2/26. October credit per Carlo.
- **QUESTIONABLE (2):** Slavin (replacement umbrella for cancelled Nationwide line, nothing issued — Nationwide cancel date pending), Bruder (same-line renewal vs added liability — coverage comparison pending).

PENDING: none outstanding beyond the two coverage questions above.

## 5. Coverage Enhancement (5 rows)

- **POSSIBLE (1):** Yen Tshering — collision + 2 discounts (check 3-enhancement rule).
- **WEAK (2):** umbrella request (not completed), PFA endorsement (unclear).
- **REJECTED (2):** new policies mislabeled as enhancements.
- Final reconciliation of rules pending.

## 6. Referral Won (4 rows)

- Carlo's referral definition applied: 2 of 4 confirmed in Nicole's report, 2 flagged.

## 7. All Star Call (46 rows)

- 6 notes contain AI Notes; no calls substantively verified in Magellan.
- 40 rows without call evidence.
- Alejandro must provide September Magellan data and quality criteria.

## Pending as of this draft

1. Cross Sell browser batch 2 (5 accounts, running).
2. Cross Sell browser batch 3 (Top Notch Tree Service, Jaguar prior-client).
3. Alejandro's reply (Mendieta policy status; 1812 cyber one-or-two). Emailed 2026-09-27 ~21:56 EDT.
4. Plymouth Rock portal results (2 autopay rows).
5. Travelers portal via Dusty/box (5 autopay rows), Bristol West credentials, Liberty Mutual credentials.
6. Magellan/All Star Call data from Alejandro.
7. Carlo's calls: October-effective credits (NCSR rows 3, 5, 11; Kelso timing; Murray King cross-sell timing), Slavin/Bruder cross-sell classifications.

## Blockers / notes

- Travelers portal bot-blocks the sandbox browser; Dusty brief ready for box execution.
- Bristol West: no saved credentials. Liberty Mutual: saved credentials rejected.
- Magellan/Sonant: no API skill; browser access needed for sentiment data.
- July 2026 data missing from AppSheet (August uploaded, September in progress).
- Scheduled WOW jobs (`wow-24th-early-pull`, `wow-27th-second-pass`, `wow-1st-final-reconciliation`) still carry pre-ruling definitions — must be revised before the Oct 1 final run.
- PR #645 carries the working docs; production requires the exact QA-certified digest and Carlo's explicit approval. Nothing here is production-promoted.
