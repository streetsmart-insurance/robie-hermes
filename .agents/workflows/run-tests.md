# Run tests

1. Confirm the working tree and branch. Refuse to run release work directly from the Production branch.
2. Run the complete automated test suite and security/secret checks defined by the repository.
3. Record the commit SHA, test commands, pass/fail counts, failures, and artifact hashes.
4. Do not deploy or claim readiness when any required check is missing or failing.

