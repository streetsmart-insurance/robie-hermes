# Plaid bank reader candidate

First implementation slice: exact entity/Item/institution/account scope, pinned
Test secret loading, Sandbox-only vendor HTTPS reads, complete transaction sync
in memory and pure Hosted Link request preparation. No bank enrollment,
production network transport, token exchange, scheduler or durable storage is
installed. Not a completed bank connection or released accounting automation.

The HTTPS allowlist contains only Item, account and transaction reads. It
refuses redirects, caller credential overrides and live tokens; it caps response
size and time, sanitizes errors and performs no blind retry. A pagination
mutation discards the partial delta and restarts from the original cursor once.
Missing history readiness, stale/failed source updates, unexpected accounts,
duplicate records, malformed groups, looping cursors or exhausted bounds hold.

Raw provider rows, pending flags, signed amounts, original descriptions,
removals, pages and final cursor remain private in memory. The returned account
balances are **cached**, not fresh Balance API evidence. No claim of cleared
funds, financial reconciliation, currency normalization or durable completion
is made. A complete empty delta is possible only with explicit historical pull
completion. Returned data is not a complete lifetime bank history guarantee.

## Concrete prerequisites

1. Privately map owner-approved bank/account types to company, provider Item,
   institution and exact account IDs. Last four digits help humans identify an
   account but are not the authoritative join key. Verify consent and coverage.
2. A credential administrator provisions separate Sandbox and live credentials
   through the approved private Secret Manager route. Do not copy keys through
   chat, commands, source files, PRs or the engineering/audit identity. This
   candidate accepts a pinned `projects/.../secrets/plaid-...-test/versions/N`
   reference and rejects key-file credentials. Pins do not prove IAM isolation;
   the operator must independently verify Test/Production isolation.
3. Build a durable enrollment executor: atomically reserve entity/operator/bank
   intent, reuse pending sessions, bind returned Link tokens to the intended
   user, cap lifetime-created free Trial Items at 10 and refuse paid upgrades.
   Tokens never belong in logs, stdout, command arguments or unencrypted stores.
   Re-check existing Items before exchange; unknown outcomes stop, not retry.
4. Owner authorizes exact bank accounts and data scopes in bank-hosted Link.
   Pure request preparation here does not authorize any connection. Correct
   institution/account readback is mandatory before binding an Item. Existing
   Muse connections are separate from this server integration.
5. Add encrypted source/evidence storage with an atomic transaction delta +
   cursor commit, concurrency guard and original-page hashes. No consumer may
   advance the cursor until persistence succeeds; failed reads retain the old
   cursor. Preserve modified/removed transactions and pending-to-posted changes.
6. Add optional statement/balance retrieval after exact product support and
   original private replay. Capital One pending data/refresh limitations and
   Wells clearing semantics need independent validation. Never translate a
   Plaid posted transaction into `posted_cleared` automatically.
7. Integrate these tests in hosted CI, independently QA on Test, preserve rollback
   and release the exact certified archive through the existing release process.
   Carlo's exact-version release approval remains required. No live calls or
   Production install are authorized by this document.

No user financial originals, personal details or real provider identifiers are
in the synthetic tests. No deployment units, secret provisioning or IAM changes
are included. The module is imported only by explicitly opting-in callers.

Primary specifications consulted:
- https://plaid.com/docs/link/hosted-link/
- https://plaid.com/docs/api/products/transactions/#transactionssync
- https://plaid.com/docs/api/items/#itemget
- https://support.plaid.com/hc/en-us/articles/39994173227159-What-is-the-Plaid-Trial-plan

Required QA: cross-company/account rejection; incomplete historical readiness;
partial failure with unchanged durable cursor; mutation restart; removal and
pending transitions; encrypted-store failure/concurrency; duplicate enrollment
and ambiguous exchange; revoked consent; lifetime Trial budget; no plaintext
token exposure; exact Test/Production identity and secret isolation.
