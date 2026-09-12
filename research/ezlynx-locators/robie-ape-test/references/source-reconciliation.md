# APE source reconciliation

Source workbook: [Policies-Application/Policy Entry (APE)](https://docs.google.com/spreadsheets/d/1OTrvGJR9GlzH9b_JBkvpIVT-XZ_UQuUcIwYWtc3Bs-A/edit)

Inspected tabs:

- `Personal Lines Policy` (`gid=1527186573`)
- `Commercial Lines Policy` (`gid=1595746460`)
- `Trucking Lines Policy` (`gid=810810458`)
- `Generic LOB` (`gid=418791581`)

The APE skill routes only the 12 profiles in the current Robie Policy Setup
catalog. All other rows need a separate skill. The Trucking tab is excluded;
do not reuse Commercial Auto — Contractors for trucking.

## Known conflicts and gaps

| Source | Issue | Required behavior |
| --- | --- | --- |
| Personal Lines row 4, Dwelling Fire | Says mailing and property addresses are always different | Block and obtain approved case-specific guidance; do not encode an absolute rule |
| Commercial Lines row 4, Commercial Package | Says General Aggregate is Per Policy if unclear | Block when unclear; do not default |
| Commercial Lines row 14, General Liability | Says General Aggregate is Per Policy if unclear | Block when unclear; do not default |
| Commercial Lines rows 8-9 | Cyber and D&O reuse “Death Benefit Coverage” wording | Treat as suspected copy error and return `NEEDS-SKILL` |
| Rows with no guidance and no video | Evidence is incomplete | Return `NEEDS-SKILL` until approved material exists |
| Prior training guidance | Named account `220485040`, which browser verification showed as deleted | Use only the active `ROBIE Test LLC` account `220250093`; see `sessions/2026-08-31-test-vm-account-identity-verification.md` |

## Supported source rows

The exact supported rows, video links, guards, and readiness states are in
`training-pack.json`. Re-read the live sheet when the workbook changes and
create a new pack revision instead of silently mutating trained behavior.
