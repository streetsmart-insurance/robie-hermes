# CURRENT_STATE — what Production actually loads

Verified 2026-08-27 ~9:26am America/New_York on **hermes-poc-01**
(`streetsmart-hermes-poc`). Documentation only. A zip pointer is not “the
whole process is that SHA.”

## Live zip

- GitHub `main` merge of PR 18: `ea3405e51334a4d04edbd949c650c54f949d9efb`
  (12-char `ea3405e51334`).
- Both pointers:
  - `/opt/streetsmart-hermes/current`
  - `/opt/streetsmart-hermes/releases/current`
  - → `/opt/streetsmart-hermes/releases/ea3405e51334/robie-hermes-ea3405e51334`
- `hermes-gateway` restarted `ActiveEnterTimestamp=2026-08-27 13:26:07 UTC`,
  `MainPID=200329`.
- Prior zip `a1b4774ee5e1` left on disk for rollback (PR 17).

## Two code paths (intentional, not leftover)

1. **`hermes-gateway` is a Hermes-agent process.**
   `ExecStart=/home/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run`,
   `WorkingDirectory=/home/streetsmart-hermes/.hermes`,
   `HERMES_HOME=/home/streetsmart-hermes/.hermes`.
2. **The robie-hermes zip is a PYTHONPATH overlay only.**
   `PYTHONPATH=/opt/streetsmart-hermes/releases/current:/srv/robie/current/vendor:/srv/robie/current`
   and `ROBIE_CANONICAL_JOB_ENGINE_ROOT=/opt/streetsmart-hermes/releases/current`.

Zip loads: entire `robie_job_engine/` (`store.py`, `chat_guard.py`,
`engine.py`, `scheduler.py`, …) and `ezlynx_login_bootstrap.py`.

PR 18 heartbeat (`start_generic_chat_job_heartbeat` in `chat_guard.py`) is on
the zip path. That is why 18 can work without patching the plugin adapter.

Recorder tab rebind (`robie_job_engine/browser_capture.py` +
`recording_tab.py`) is on the zip path. Production capture is
`python -m robie_job_engine.browser_capture`, so a zip flip + gateway restart
stops first-ezlynx-wins. The optional Playwright hint write lives in
`deploy/hermes/tools/playwright_tool.py` / write-guard; that is a second
`.hermes` install if the live tool file is stale. Rebind still works without
the hint.

Post-job audit (`robie_job_engine/post_job_audit.py`) is also on the zip path.
`guard_chat_response` / `_render_chat_terminal` in `chat_guard.py` appends the
four-answer audit to the existing Robie Chat APP reply. Production Chat already
imports those functions via PYTHONPATH, so a Chat job close-out posts the audit
without a second `.hermes` adapter install. `JobEngine.run` and the scheduler
orphan path persist the same checkpoint. A zip-only adapter change would miss
Production Chat; this hook does not live only in `integrations/google_chat/`.

## Two distinct `.hermes` homes

Live check 2026-08-27: **not the same folder.**

| Path | inode | owner | mtime |
| --- | --- | --- | --- |
| `/home/streetsmart-hermes/.hermes` | 1918612 | `root:root` | Aug 19 |
| `/opt/streetsmart-hermes/.hermes` | 4220735 | `streetsmart-hermes` | Aug 27 |

`/home/streetsmart-hermes/.hermes` is `HERMES_HOME` in the gateway unit.

Plugin adapter exists **only** at
`/opt/streetsmart-hermes/.hermes/hermes-agent/plugins/platforms/google_chat/`
(`adapter.py`, `oauth.py`, `plugin.yaml`). The `/home` path has no that
directory.

## What ignores the zip pointer

Second, non-atomic deploy path. Zip flip does **not** update these:

- Chat adapter + oauth plugin under `.hermes/hermes-agent/plugins/platforms/google_chat/`
- Hermes tools: `playwright_tool.py`, `playwright_write_guard.py`,
  `gemini_field_tool.py` (must be installed into the Hermes tools dir)
- Hermes skills under `.hermes/skills` (`robie-playwright-browser`,
  `ezlynx-commercial-auto-from-quote`, `ezlynx-gemini-fallback`)
- `config.yaml` / SOUL / `playwright.yaml` merges
- browser profile `.hermes/browser-profiles/ezlynx`
- tokens and drive-skills snapshots
- `/srv/robie/current` + vendor still on `PYTHONPATH` after the zip
- flattened `/opt/streetsmart-hermes/robie-job-engine` (session unit still
  references it)
- Hermes-agent venv / `hermes_cli` / plugin loader (not in the zip)

## Live definition (Carlo 2026-08-27)

“Live” is **not** GitHub `main` alone, and **not** zip pointer alone.

After every Production zip, all three must be true:

1. Both pointers match the SHA.
2. `hermes-gateway` `ActiveEnterTimestamp` is **after** the pointer flip.
3. For a Chat job, a `checkpoints.kind=gateway_progress` row exists in
   `/opt/streetsmart-hermes/robie-job-engine/data/jobs.db`.

Pointer-only is how PR 17 looked deployed while Chat still ran stale plugin
code.

## PR 17 lesson

17 added `heartbeat_generic_chat_job` in `store.py` (zip) and called it from
`integrations/google_chat/adapter.py` (zip copy). Production imports the
plugin adapter from `.hermes`, so the loop never ran. Job `e06d5e13` failed
at 300s with zero `gateway_progress` rows while Playwright kept running.
