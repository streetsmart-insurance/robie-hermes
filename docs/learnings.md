# Worker learnings

The four verification workers (policy changes, mortgagee, manual renewals,
audits) accumulate standing rules in `robie_job_engine/learnings/`, one YAML
file per worker:

- `policy_changes.yaml` — policy-change worker
- `mortgagee.yaml` — mortgagee-verification worker
- `renewals.yaml` — manual-renewals worker
- `audits.yaml` — audit-verification worker

## Adding a learning

Append an entry to the worker's file:

```yaml
  - id: pc-014
    rule: "One or two sentences, plain English, actionable."
    learned: "2026-10-05"
    source: "Where it came from (Carlo rule, case name, worker SOP...)"
```

Rules:

- `id` starts with the worker prefix (`pc-`, `mg-`, `rn-`, `au-`) and is unique.
- `rule` is the standing behavior, not the story. Put the story in `source`.
- `learned` is `YYYY-MM-DD`.
- Keep it to one or two sentences. If it needs a paragraph, it belongs in a
  runbook, not here.

Then run the tests:

```bash
pytest tests/test_learnings.py -q
```

The loader (`robie_job_engine/learnings/__init__.py`) validates the schema:
required fields, unique ids, id prefix matches the worker, `learned` is a
real date. `get_learnings(worker)` and `all_learnings()` expose the rules
to the workers.

## Process

Learnings land via pull request like any other change. They stay unmerged
until the merge freeze lifts, like everything else.
