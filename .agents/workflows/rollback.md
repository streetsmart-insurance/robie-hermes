# Roll back

1. Identify the current release and the immediately previous verified Production digest.
2. Confirm the rollback target, open Jobs, and active leases. Preserve durable Job data and artifacts.
3. Pause new claims if required; do not delete or reset Job state.
4. Restore the previous immutable release and restart only the services required by the release manifest.
5. Run health checks and a bounded verification test.
6. Persist rollback evidence and report success only after authoritative verification passes.

