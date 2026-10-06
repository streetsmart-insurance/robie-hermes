# ASSUMPTIONS — Weekly Expiration List automation

Sandeep Yadav answered E1–E15 on 2026-10-05. His answers are decision
memos and are **LOCKED** below (each marked "LOCKED 2026-10-05" with the
rule adopted). Every behavior choice not marked LOCKED is still an
assumption that must be confirmed or corrected before this automation is
scheduled. Each item maps to its question number.

Status legend: **LOCKED** = Sandeep's 2026-10-05 answer, implemented as
the rule. **PROVEN** = observed live / ran. **INFERRED** = derived from
the docx or read-only inspection. **UNVERIFIED** = heuristic with no live
proof yet.

---

## Feasibility findings (from the coordinator brief)

**F1. PROVEN.** None of the four docx per-account endpoints existed in the
codebase. All are now implemented as new code in `portal.py` via the
`ezlynx_portal_session` CDP-cookie path, and all five endpoints (the four
plus the Retention Center list) were **proven live 2026-10-05** through the
box's persistent Chrome session:
`POST /EZLynxRenewalCenter/ExpirationList/GetExpirationList`,
`GET /ApplicantApi/v1/sidebar`, `GET /PolicyAPI/v1/PolicyCard/GetPolicies`,
`GET /EZLynxPortalAPI/Discussions/GetPagedDiscussions`,
`GET /EZLynxPortalAPI/Discussions/GetDiscussionDetail`. (The Retention
Center list endpoint is a POST with query-string params, discovered by
reading the page's `retentionCenter.js` — not a GET.)

**F2. PROVEN.** The Retention Center fetcher is new code
(`logic.fetch_retention_rows`). Pagination is implemented; live shape
confirmed (`results[]` with `ID`, `DaysToExpiration`,
`EarliestExpirationDate`, `FirstName/LastName/BusinessName`).

**F3. PROVEN.** The box Chrome (user `carlo_s+`, profile
`/opt/streetsmart-hermes/.hermes/browser-profiles/ezlynx`, CDP
`http://127.0.0.1:9222`) had a live EZLynx session during the build
(40 EZLynx cookies readable). The code only *reads cookies* via CDP —
it never navigates tabs, never launches a competing Chrome, never
touches the profile. If the session dies, every read fails closed.

**F4. UNVERIFIED.** Sheets writes reuse the proven auth pattern from
`streetsmart_agency_exports_sheets_push.py` (token file → ADC). The
HTML-table layout, yellow section rows, blue producer rows, and red
pending-Cancellation fill are implemented as Sheets API `batchUpdate`
payloads — but the *formatting* has never been applied to a live sheet.
`--dry-run` dumps the exact payloads for review. **Do not enable
`--write` until someone eyeballs one dry-run against the real sheet.**

**F5. UNVERIFIED.** The latest-staff-note bot skip is a configurable
heuristic (`config.BOT_TEXT_MARKERS` / `BOT_AUTHOR_MARKERS`). The docx
does not fully specify bot signatures; the marker set is INFERRED from
the docx's examples.

---

## E-questions — Sandeep's answers (LOCKED 2026-10-05)

**E1 — Window is 30 days. LOCKED 2026-10-05.** Sandeep confirmed the
30-day window. `WINDOW_DAYS = 30`; no code change was needed.

**E2 — Day 0 included, expired excluded. LOCKED 2026-10-05.** Sandeep
confirmed: include `DaysToExpiration == 0` (expires today); exclude
negative days. Already `0 <= DaysToExpiration <= 30`; no code change.

**E3 — Pagination stop rule. LOCKED 2026-10-05.** Sandeep: "paginate —
if row 100 on a page is still ≤30 days, open the next page via the
bottom pager; repeat until a page has a row >30 days or no more pages."
Implemented exactly: `fetch_retention_rows` continues to the next page
iff the LAST row of the current page has DaysToExpiration ≤ 30 AND
another page exists (empty page ends the loop).

**E4 — Renewal-discussion matching. LOCKED 2026-10-05.** Sandeep:
a renewal discussion title counts if it contains (case-insensitive)
`renew` / `renwal` / `non-renew` / `"mortgage verification"` / whole-word
`NR`, AND is tied to the expiring policy number (policy number in the
title OR in the notes); otherwise Notes = "No notes". REJECTED titles
(case-insensitive substring on title): `"master certificate renewal"`,
`"policy change request"`, `"certificate"`, `"coi"`, `"audit"`, `"eva"`.
Implemented as reject-first include/exclude in the discussion matcher
(`is_renewal_discussion`); the policy tie is checked after fetching
detail (title OR notes; no policy number anywhere → "No notes"). Verbatim
adoptions: "Non-renewal notice" is accepted (with policy tie); whole-word
"NR" matches ("NR letter sent") but not inside another word; the docx's
"no renewal discussion → 'No notes'" maps to the existing
`NO_NOTES_TEXT` placeholder. Edge noted: the verbatim `"eva"`
substring can reject titles like "Renewal - Eva Smith" (a person's
name) — Sandeep's rule is applied verbatim; flag if false rejects
appear.

**E5 — Cycle start per line of business. LOCKED 2026-10-05.** Sandeep:
90 days personal lines, 120 days commercial/trucking. The fixed
`CYCLE_DAYS = 120` is replaced with per-policy cycle days
(`cycle_days_for_lob`, mapped from the PolicyCard `lob` text — the field
exists per the docx's `"{lob} | {policyNumber}"` format). Missing or
unmapped LOB falls back to 120 days and is flagged in the run summary
(`lob_review`). Mixed-LOB accounts use the LONGEST window (earliest
cycle start) so no policy's renewal discussion is dropped early.
**INFERRED, needs confirmation:** the personal/commercial LOB marker
lists (`config.PERSONAL_LOB_MARKERS` / `config.COMMERCIAL_LOB_MARKERS`)
are my best-guess keyword maps — Sandeep did not provide the full LOB
vocabulary, and the live `lob` values have not been sampled. **UNVERIFIED**
against live PolicyCard payloads from this VM (no browser access).

**E6 — One row per account; account-level assignment wins. LOCKED
2026-10-05.** Sandeep confirmed: one row per account; the sidebar
account-level `assignedTo` wins over policy-level assignees. Already the
behavior; no code change.

**E7 — Non-renewal marker. LOCKED 2026-10-05.** Sandeep: when an NR
discussion exists for the account, start Notes with `"NON-RENEWAL – "`
(en dash, exact). Replaces the old `[Non-renewal]` prefix
(`NONRENEWAL_NOTE_PREFIX` now holds the exact string).

**E8 — No carryover. LOCKED 2026-10-05.** Sandeep: no carryover — each
run rebuilds the tab from scratch; renewed policies drop off. The
green-highlight/carryover concept is REMOVED: policies with
`pendingType="Renewal"` are EXCLUDED from the list entirely (not
highlighted), and an account whose expiring policies are all renewed
drops off the list (`run_account` returns None).

**E9 — Empty producer fallback. LOCKED 2026-10-05.** Sandeep: sidebar
`assignedTo` → Retention Center "Renewal Manager" → an "Unassigned"
section at the bottom. Implemented: `assigned_producer` applies the
fallback chain; the trailing section is renamed "Unmapped" → "Unassigned".
**INFERRED, UNVERIFIED:** the Retention Center list field carrying the
Renewal Manager is assumed to be `RenewalManager` (never observed in a
live `results[]` row from this VM); `parse_retention_row` also tries
`"Renewal Manager"`.

**E10 — Roster source. LOCKED 2026-10-05.** Sandeep: roster/department
source is the AppSheet employees view
(`https://www.appsheet.com/start/a1d9136f-5471-48b3-bcd7-9137cdbf6b15?platform=desktop#appName=NewApp-868850307&view=Employees`).
A bounded Google Drive search did NOT find a backing sheet for this app
(its Drive folder `NewApp-868850307` contains only `_recoveryData`), so
there is currently nothing automatable to read: the hardcoded
`config.SECTIONS` roster is kept, and this URL + gap are recorded here.
Unmapped producers already surface via the "Unassigned" section AND the
run summary (`unassigned_producer`) — verified in tests. Before
scheduling: decide how the AppSheet roster gets read (its backing store
is unknown today).

**E11 — "Last Activity by" format. LOCKED 2026-10-05.** Sandeep: first
name + last initial. Implemented as `author_label`: exact format
**"First L" (no period)** — e.g. "Jazmin M". Single-token names stay
as-is ("Madonna" → "Madonna").

**E12 — Truncation. LOCKED 2026-10-05.** Sandeep: truncate notes at the
last sentence boundary before 450 chars; if none, word boundary; append
"…". Implemented: sentence boundary = the last occurrence of `". "` /
`"! "` / `"? "` at or before char 450 (punctuation INCLUDED), else word
boundary, then "…".

**E13 — New dated tab per week. LOCKED 2026-10-05.** Sandeep: write each
week to a NEW dated tab; NEVER rewrite a tab people are working in;
Sheet1 is a read-only template. Replaces the old backup/merge/`--force`
strategy: the writer duplicates Sheet1 into a new tab named
`"Expiration List YYYY-MM-DD"` (`config.TAB_NAME_BASE`, default
`"Expiration List"`), writes values + formatting to the new tab only,
and fails closed if a tab with that name already exists (a `--force`
flag may allow overwriting ONLY the same-named dated tab, never Sheet1
or another tab). Sheet1 is never written to — the runner refuses
`--tab Sheet1` outright.

**E14 — Verification step. LOCKED 2026-10-05.** Sandeep: keep the
verification step on every run until a few consecutive clean runs, then
spot-check ~10 accounts; re-run if >5% of rows corrected. Encoded as a
RUN-SUMMARY CHECKLIST item (`config.VERIFICATION_CHECKLIST`), printed by
every run — not a code gate. `--verify-sample N` stays as the ad-hoc
re-check flag.

**E15 — Pending Cancellation red flag. LOCKED 2026-10-05.** Sandeep:
pending Cancellation → red flag; pending Endorsement → no effect on list
membership. Implemented: a policy with `pendingType="Cancellation"` gets
`"CANCELLATION PENDING – "` prepended to Notes AND a red-fill
(`#f4cccc`) formatting request across its row (payload-level, dry-run
testable). Endorsement → normal row, no marker, stays on the list.

---

## X-questions (cross-cutting)

**X1 — Canonical sheet / no writes by default. LOCKED 2026-10-06.**
Sandeep confirmed the canonical Expiration List sheet on 2026-10-06:
https://docs.google.com/spreadsheets/d/1wj8ywd5zMs_ub2ugOxNEulRm09bF60BlYIjE44zUvTg/edit
(gid=0) — sheet ID `1wj8ywd5zMs_ub2ugOxNEulRm09bF60BlYIjE44zUvTg` matches
the docx. Per E13 each week writes to a new dated tab duplicated from
Sheet1. **Default mode stays dry-run: zero sheet writes unless `--write`
is passed explicitly.** DISTINCTION (recorded per Sandeep's own words):
his 2026-10-06 12:06 EDT "Edit access is already given" statement was
about the change-request sheet thread, NOT this expiration sheet — edit
access on THIS sheet was never explicitly confirmed. Before any `--write`:
(1) confirm edit access on this sheet, and (2) eyeball one dry-run
payload against the real sheet (see F4 / blocker 6).

**X2 — Timezone.** All date math is `America/New_York`. *Confirm EZLynx and
sheet timestamps are ET.*

**X3 — Failure semantics: fail closed.** If any account read fails, the run
collects the failure and **writes nothing** (exit 3, summary lists the
failed accounts). There is no partial sheet write and no automatic retry;
notification is the run's stdout summary + exit code (no chat/email is
sent by this automation). *Confirm who should be notified and how.*

**X4 — Consumers.** Assumed: the producers/CSRs per section, acting on
no-notes follow-ups; pending-Cancellation rows need carrier follow-up.
*Confirm who consumes the report and what action each section drives.*

**X5 — Session ownership.** The automation never logs in and never enters
credentials; it borrows the box's persistent `carlo_s+` Chrome CDP session
cookies. If that session dies, the run fails closed. *Needs: who owns
keeping the server-side EZLynx session alive (the standing open question
from the grilling email).*

**X6 — Schedule. LOCKED 2026-10-06.** Sandeep confirmed 2026-10-06: the
three reports (change tracker, expiration list, missed calls) run
**every Monday** (`config.SCHEDULE_WEEKDAY = "Monday"`). Suggested time
(~8:00 AM ET) is PROPOSED but UNCONFIRMED. **No timer is installed**
(merge/timer freeze in effect — install only after Sandeep confirms the
time AND Carlo lifts the freeze).

---

## Blockers that need Sandeep before scheduling

1. **E5** — the LOB marker lists are INFERRED (no full LOB vocabulary from
   Sandeep; live `lob` values unsampled from this VM). Confirm the
   personal/commercial marker mapping before trusting per-policy 90/120
   windows.
2. **E9** — the Retention Center `RenewalManager` field name is INFERRED
   (unverified against the live list shape). Confirm the exact field name
   or the fallback silently never fires.
3. **E10** — the AppSheet app's backing store is unknown (Drive folder
   has only `_recoveryData`); decide how the roster is read before the
   hardcoded roster can be replaced.
4. **X1** — canonical sheet is CONFIRMED (LOCKED 2026-10-06), but edit
   access on THIS sheet was not explicitly confirmed, and unattended
   writes are still not approved. Need: (a) edit-access confirmation,
   (b) one eyeballed dry-run (blocker 6), (c) explicit write approval.
5. **X5** — who owns the server-side EZLynx session.
6. **F4** — eyeball one dry-run's formatting payloads against the real
   sheet before `--write` is ever used.

Resolved (coded) as of 2026-10-05: E4 title patterns + exclusions,
E10 (hardcoded roster kept, AppSheet URL recorded, Unassigned +
summary surfacing verified).
