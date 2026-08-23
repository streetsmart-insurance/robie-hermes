# Deploy to Test

1. Require a feature branch with passing automated tests and security/secret checks.
2. Build one immutable release artifact and record its digest.
3. Inventory Test, open Test Jobs, and current Test release; create a rollback pointer.
4. Deploy only to the Test profile using the Test deployment identity.
5. Run health checks and the relevant end-to-end failure-mode tests.
6. Store evidence with the commit SHA, release digest, environment, timestamps, results, and recording links.
7. Report `TEST VERIFIED` only when authoritative evidence passed. Otherwise report `TEST UNVERIFIED` or `TEST FAILED` and do not promote.

