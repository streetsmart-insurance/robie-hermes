# Promote to Production

1. Require an approved Test run with authoritative evidence and an immutable release digest.
2. Confirm the Production approval and exact digest. Do not rebuild or substitute artifacts.
3. Inventory Production health, open Jobs, active leases, and current release; preserve the previous verified rollback pointer.
4. Promote using the Production deployment identity and the minimum required permissions.
5. Run post-promotion health and bounded end-to-end verification without disrupting existing Jobs or the EZLynx profile.
6. Persist promotion evidence. Report `PRODUCTION VERIFIED` only after authoritative checks pass.
7. If checks fail, stop new work safely and offer the one-action rollback; never claim success.

