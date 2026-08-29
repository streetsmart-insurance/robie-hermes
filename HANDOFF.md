# Hermes handoff — Production zip base 8734440 + PR 65

## MUST-CALL — diagnose any Hermes job

Do **not** diagnose from Google Chat text, the published recording, or by
clicking Production/Test Chrome from a laptop. Do not invent leftover
RETRY. Production is not the first test. Do not deploy to
`hermes-poc-01` / `hermes-test-01` from this lookup.

Copy-paste on the VM (or with `ROBIE_JOB_DB` pointed at an isolated copy):

```bash
PYTHONPATH=. python3 -m robie_job_engine.playwright_observability <job-id>
# or
PYTHONPATH=. python3 scripts/lookup-playwright-job.py <job-id>
```

That prints `playwright_exec` rows, CDP `/json/list` url+title snapshots,
and the `playwright-trace.zip` path if present. Zero tool rows on an
EZLynx/Playwright Chat job is FAILED (1df9740b silent-gap), not
UNVERIFIED. No cookies, secrets, or passwords.

This file did not exist on Production SHA `8734440b57cc`. It is the PR 65
MUST-CALL only. It is not the later-main Ascend API handoff.
