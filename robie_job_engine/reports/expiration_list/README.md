# Weekly Expiration List report

Server-side automation of the "Weekly Expiration List Report (EZLynx →
Google Sheet)" procedure. Reads the Retention Center expiration list
(≤30 days), per-account sidebar + PolicyCard + renewal discussions via the
box's EZLynx CDP session cookies (read-only), picks the latest staff note
(skipping bots/automation), and builds the producer-sectioned layout for
the **Expiration List** sheet tab.

Every open question is recorded in `ASSUMPTIONS.md` — read it before
scheduling anything.

## Exact run command

Dry-run (default — full read path, zero sheet writes):

```bash
cd /path/to/robie-hermes
python -m robie_job_engine.reports.expiration_list.runner
```

Useful variants:

```bash
# Limit accounts (smoke test) and print JSON summary
python -m robie_job_engine.reports.expiration_list.runner --limit-accounts 3 --json

# Run the read path against recorded fixtures instead of the live portal
python -m robie_job_engine.reports.expiration_list.runner --fixtures ./fixtures

# Ad-hoc independent re-check of the first N rows (docx Step 4)
python -m robie_job_engine.reports.expiration_list.runner --verify-sample 5

# WRITE to the sheet (requires explicit opt-in; see ASSUMPTIONS.md F4/X1/E13)
python -m robie_job_engine.reports.expiration_list.runner --write
python -m robie_job_engine.reports.expiration_list.runner --write --force   # overwrite the same-named dated tab only (never Sheet1)
```

Options: `--sheet-id`, `--tab` (default `Expiration List`),
`--state-dir` (default `~/.robie/expiration-list`), `--limit-accounts`,
`--fixtures DIR`, `--verify-sample N`, `--json`.

Exit codes: `0` success · `2` fail-closed abort (nothing written) ·
`3` partial read failures (nothing written).

## Requirements

- Box (hermes-poc-01): the persistent Chrome CDP session must be alive;
  the code only *reads its cookies* (never navigates, never logs in).
- Sheets writes need `google-api-python-client` + either
  `ROBIE_GOOGLE_TOKEN_FILE` (default
  `/opt/streetsmart-hermes/.hermes/google_token.json`) or Application
  Default Credentials. Dry-run needs neither.

## Layout

| Module | Purpose |
|---|---|
| `config.py` | Constants: sheet/tab, colors, producer roster, bot heuristics, windows |
| `portal.py` | The 5 portal endpoint clients (CDP-cookie path; injectable transport) |
| `logic.py` | Pure business logic: filters, discussion matching, note picking, grouping |
| `layout.py` | Sheet row model, reference HTML table, Sheets `batchUpdate` payloads |
| `sheets.py` | Sheets API I/O: auth, Sheet1 duplication, A:E-only clear/write, fingerprint |
| `runner.py` | Orchestration, CLI, `--dry-run`, state, `--verify-sample`, fixture replay |

Tests: `tests/test_expiration_list_report.py` (60 tests, offline).

## Safety

- Read-only on EZLynx: only GETs and one read-only POST (the list query).
- Dry-run by default; writes need `--write`.
- Never touches a tab people are working in: --write creates a NEW dated tab
  ("Expiration List YYYY-MM-DD") by duplicating Sheet1; Sheet1 is the read-only
  template and is never written. Fail-closed if the dated tab already exists
  (--force may overwrite only that same-named dated tab).
- No merges to main, no timers, no emails, no chat messages from this branch.
