# Explicit QBO environment selection (draft only)

This change is based on Test commit 87e36e447c9fd456ce01cc8405ce8b41796f7999.
Nothing is installed, enabled or authorized to run by this document.

Set APPLIED_QBO_ENVIRONMENT=sandbox to use only qbo_sandbox_* secrets and
sandbox-quickbooks.api.intuit.com. Set production explicitly to use the
production secrets and endpoint. Missing, invalid or mismatched environment
selection stops; missing sandbox credentials never fall back to production.
The selected qbo_<environment>_environment secret must contain the exact
selection. Existing worker and snapshot callers use this environment setting.

OAuth refresh may rotate the saved refresh token. By default the client refuses
to refresh or make a QBO query, before any refresh HTTP request or token write.
Only after separate owner approval for that environment's credential rotation
and write-back may deployment set APPLIED_QBO_ALLOW_TOKEN_WRITEBACK=true.
The flag is a mechanism, not evidence of permission. Rotated tokens are saved
only to the selected environment's refresh-token secret; a storage failure
stops before the ledger query. QBO data calls remain GET/query only.

Sandbox success proves the client wiring and matching against sandbox data,
not the real StreetSmart ledger or Wells clearing. The snapshot's Trust account
ID 27 and account-name filters are still production-specific assumptions and
need suitable synthetic sandbox fixtures; this patch does not discover bank
accounts or turn sandbox data into real reconciliation evidence. Real Applied
settlement emails do not automatically have corresponding sandbox receipts.

This patch does not change timers, install secrets, rotate live tokens, fetch
bank clearing records or write QBO transactions or EZLynx account notes.
Synthetic tests cover strict selection, endpoint/secret isolation, missing
secrets, invalid flags, no HTTP/write without approval, selected-environment
rotation, unchanged tokens, and stopping on credential storage failure.
