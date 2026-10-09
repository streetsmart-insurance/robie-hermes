# Carrier document retrieval: state after the 2026-10-08 reconcile

This note covers the carrier pull workers (`robie_job_engine/*_pending_cancellation*.py`,
`utica_login.py`, `carrier_dry_run.py`). It replaces the "do not merge" status in Ralph's
2026-10-06/07 handoffs. The merge freeze ended Oct 5. EZLynx filing for carrier documents stays
off (`ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=0`).

## Where the code came from

| Source | What it was | Where it is now |
|---|---|---|
| `feat/carrier-reconciled` @ `abe12c40` | What hermes-test-01 ran (main only had the #798 cherry-pick) | Utica, Guard, Progressive FAO and GEICO workers fast-forwarded to it (lossless: main's copies were ancestors) |
| Ralph's `fix-utica-auto-login` (58768d32, e7c0e439, b0f356c4, 792a4c33) | Utica auto-login and Progressive NJA policy numbers | Recovered from the Test release because the git bundle arrived corrupted. Ralph's `tests/test_utica_login.py` was lost and has been rewritten |
| Hand patches on hermes-test-01 | Utica Title Case locators; Guard anchors and screenshot; GEICO non-High, screenshot and gds-table | Reconciled, then tightened (case-insensitive helpers, hold reasons) |

## Utica First MFA needs server-side Gmail

Utica's Okta login can ask for an emailed one-time code. `utica_login.get_verification_code()`
reads that code from Gmail, and **only works server-side**:

- Set `ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT` in the job environment on the host that
  runs the pull (hermes-test-01 today). The service account needs domain-wide delegation with
  `https://www.googleapis.com/auth/gmail.readonly` on the MFA mailbox (`UTICA_MFA_MAILBOX`,
  default `carlo@streetsmart.insurance`), plus the token-creator binding that keyless signing
  uses (see `scripts/grant-gmail-signblob-token-creator.sh`).
- Agent/connector Gmail access **redacts** verification codes as `[credential:<uuid>]` in
  every format, `format=raw` included. A chat agent can never relay a code, so do not build a
  flow that depends on that.
- If the server-side read also comes back redacted, the login holds immediately with a clear
  reason instead of polling until it times out.
- Only emails received after the "send me an email" click count, so an older code is never
  reused.
- With no delegated account configured, the login holds with
  "Gmail API is not configured on this host". A human can sign in once to establish a trusted
  session, and `ensure_utica_page()` reuses a signed-in tab.

Auto-login runs only on hermes-test-01. Production hosts and unknown hosts hold before any
credential is read.

## Known limits and follow-ups (not done in this PR)

1. **Utica ExtJS grid rewrite.** `load_transactions()` and `list_documents()` expect one HTML table,
   but UFirst Now is ExtJS (29 nested tables on the transaction list). They now hold with
   "found N tables; the ExtJS grid parser is a follow-up". The fix needs a captured live DOM (or a
   direct per-policy path that skips the grid). Zangara and KLM (processed 09/25) also do not appear
   in the default list view.
2. **Utica session expiry.** UFirst Now drops idle sessions after about 20 minutes. Keep a pull inside
   that window, or re-run `ensure_utica_page()` between policies (Okta usually re-SSOs without MFA).
3. **Guard document disambiguation.** When more than one "Policy Documents" entry matches a
   cancellation term, the policy holds. The hold reason now lists each candidate with its
   scribeItemId. The heuristic (date match against the cancel date, or a specific form) should be
   chosen from those real names after the next live dry run.
4. **Progressive BOP.** The shared-tab fix lives on `feat/carrier-reconciled`, but that version fails
   7 of its own tests, so it is not taken here and the dry run still has no BOP spec.
5. **Travelers, Farmers of Salem, NatGen, `carrier_daily_retrieval`, `uticafirst_*`.** These had no
   Test hand patches and are still behind `feat/carrier-reconciled` / PR #797.
6. **Shared JS-click helper** for Blazor (Guard) and GDS/ExtJS (GEICO, Utica) controls. Each module
   has its own inline version today.
7. **Document IDs** (Utica docId, Guard scribeItemId) regenerate per session. Always harvest them
   fresh and never cache them across runs.

## Hold reasons

Every held item carries a specific reason. `carrier_dry_run` prints one line per held item, even
when the carrier is OK. It prefers `hold_reason`, because Guard's `reason` field is the carrier's
cancellation reason. JSON output adds a per-carrier `hold_reasons`. GEICO non-High alerts are
skipped and reported as held with their severity.

## Live testing still needed (hermes-test-01, carrier browser on CDP 9223)

| Carrier | Next live proof |
|---|---|
| Utica First | With the delegated Gmail account configured, run `carrier_dry_run --carriers uticafirst` from a signed-out browser: auto-login and MFA, then the Title Case tab and filter. Expect a hold at the ExtJS grid until follow-up 1 lands. For the 7 open PDFs (LSJ, Best Laid Plans, Zangara, KLM), the DocGenServlet + Referer path is proven; the grid step is not. |
| Progressive FAO | Pull one NJA policy PDF end to end and check for `%PDF-` and a sane byte count. Confirm the DOCUMENTS tab still matches in any casing. |
| Guard | Dry run. Read the new ambiguous-document hold lines, choose the disambiguation rule, then do one PDF smoke test. |
| GEICO | Dry run. The previously unexplained "1 held" now prints its reason (likely a non-High alert or a commercial policy). |
| Farmers of Salem | First retrieval past login. No code change here (locators are already case-insensitive). |
| NatGen | Past the two-stage login. No code change here. |
| Travelers | Nothing to fix in code: the portal says "Direct Bill unavailable" for the checked policies. Try another policy set or ask the carrier. |
