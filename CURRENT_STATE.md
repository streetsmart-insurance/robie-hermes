# CURRENT_STATE — what Production actually loads

Verified 2026-08-27 on **hermes-poc-01**
(`hermes-poc-01.c.streetsmart-hermes-poc.internal`, project
`streetsmart-hermes-poc`). Documentation only.

Hermes still runs. Production used to have **two code paths** that could
silently disagree after a zip pointer flip:

1. Zip overlay (`/opt/streetsmart-hermes/releases/current` via PYTHONPATH)
   — Job Engine, preflight, audit, tab cleanup, login bootstrap.
2. `.hermes` tree — Google Chat adapter, Playwright tools, skills.

A zip pointer match is not “Chat is that SHA.” Pointer-only is how PR 17
looked live while the Chat adapter still ran stale `.hermes` code (no
`gateway_progress`).

**Official install is the only supported Production flip.**
`scripts/install-official-release.sh` — not a long typed SSH cookbook —
flips both pointers and installs every Chat-critical file (adapter,
oauth, Playwright tools, write-guard, gemini helper) as a zip-load shim
or zip PYTHONPATH load, then refuses `OFFICIAL INSTALL DONE` until live
proof holds. Skills stay a separate Drive → `.hermes` install. User-owned
Loom `ascend-finance` is never overwritten here.

Live proof cannot lie. Done requires all of: both pointers match,
`hermes-gateway` `ActiveEnterTimestamp` after the flip, Chat-loaded
adapter/tools equal that zip (md5/inode or zip-load shim), and an
`install_proof` row in jobs.db. Pointer-only is not live. Chat looking
busy is not live.

After a Chat job, prove a `checkpoints.kind=gateway_progress` row in
`/opt/streetsmart-hermes/robie-job-engine/data/jobs.db`.
**Destination-verified evidence rows > 0 is success**, not Chat looking
busy. COMPLETE still requires that evidence. Success prose without it
stays UNVERIFIED/FAILED.

The install/preflight path must not leave Production at 0 CDP tabs.
Attach-only bootstrap fails with `Persistent EZLynx browser has no page`.
Empty CDP target list is INCONCLUSIVE / fail — never “session fine”. Open one
seed page then AUTHENTICATE, or stop. Do not dump secrets. Do not restart
Chrome.

This tree is **not** deployed to `hermes-poc-01`. Jake Approves, Carlo
Confirms later.

## Live zip (PR 23 deploy, 2026-08-27)

- GitHub `main` squash of PR 23:
  `256bd8fa5e0b09de1ef287c63507a0d4875f5a3a`
  (short `256bd8f` / 12-char `256bd8fa5e0b`).
- Official zip tag: `production-256bd8fa5e0b` (pre-release).
- Zip sha256:
  `48f829bb721d02fbd7e7f727d92c6643d015459c7ca59192df62558cf4806444`.
- Both pointers flipped **19:31:59 UTC**:
  - `/opt/streetsmart-hermes/current`
  - `/opt/streetsmart-hermes/releases/current`
  - → `/opt/streetsmart-hermes/releases/256bd8fa5e0b/robie-hermes-256bd8fa5e0b`
- `hermes-gateway` restarted after the pointer flip:
  `ActiveEnterTimestamp=2026-08-27 19:34:09 UTC`.
  Previous was `2026-08-27 18:25:05 UTC` on `b91e7bf6a3fa` (PR 22).
- `robie-gateway` left inactive. `robie-ezlynx-browser` left active (not
  restarted).
- RUNNING jobs at deploy: **0**.
- Old release dirs left in place (`b91e7bf6a3fa`, `9e8e0ba6684a`, and
  others). Leftover dirs are not the live tree.

## Zip overlay now live via PYTHONPATH / `current`

Verified on the live `current` tree after the flip — not a full release
listing:

- `robie_job_engine/ezlynx_account_nav.py` is in the live `current` tree
  (14690 bytes).
- Job Engine / `chat_guard` / `playwright_write_guard` ride the zip.

The zip is a PYTHONPATH overlay. It loads `robie_job_engine/` and
`ezlynx_login_bootstrap.py`. Do not treat a pointer match as proof that
Chat / Playwright loaded the new `.hermes` copies.

## `.hermes` overlays copied after extract (PR 23)

Zip flip does **not** install these. This deploy copied them from
`deploy/hermes` after extract. Each dest was backed up as `.pre-23`.

Live dests updated:

- `/opt/streetsmart-hermes/.hermes/hermes-agent/tools/playwright_write_guard.py`
- `/opt/streetsmart-hermes/.hermes/SOUL.playwright.md` (generic `SOUL.md`
  untouched)
- `/opt/streetsmart-hermes/.hermes/skills/ezlynx-commercial-auto-from-quote/SKILL.md`
- `/opt/streetsmart-hermes/.hermes/skills/robie-playwright-browser/SKILL.md`

That last path is the live dest. Do **not** claim
`.hermes/hermes-agent/skills/` or
`.hermes/skills/browser/robie-playwright-browser/` were updated — those
were not the live files.

## Two code paths (intentional, not leftover)

1. **`hermes-gateway` is a Hermes-agent process.**
   `ExecStart=/home/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run`,
   `WorkingDirectory=/home/streetsmart-hermes/.hermes`,
   `HERMES_HOME=/home/streetsmart-hermes/.hermes`.
2. **The robie-hermes zip is a PYTHONPATH overlay only.**
   `PYTHONPATH=/opt/streetsmart-hermes/releases/current:/srv/robie/current/vendor:/srv/robie/current`
   and `ROBIE_CANONICAL_JOB_ENGINE_ROOT=/opt/streetsmart-hermes/releases/current`.

PR 18 heartbeat (`start_generic_chat_job_heartbeat` in `chat_guard.py`) is on
the zip path. That is why 18 can work without patching the plugin adapter.

Recorder tab rebind (`robie_job_engine/browser_capture.py` +
`recording_tab.py`) is on the zip path. Production capture is
`python -m robie_job_engine.browser_capture`, so a zip flip + gateway restart
stops first-ezlynx-wins. The optional Playwright hint write lives in
`deploy/hermes/tools/playwright_tool.py` / write-guard; that is a second
`.hermes` install if the live tool file is stale. Rebind still works without
the hint.

Production pre-flight (`robie_job_engine/production_preflight.py`) is on the
zip path. It is infra only: host/intake health, not a job-type gate. A
pre-flight yes does not authorize a new job type on Production.
New job types still need N clean Test (`hermes-test-01`) jobs before
Production on a real account (N = 3; see RELEASE_PROCESS.md). It is not a
dashboard and not a zip-pointer check. After every pointer flip +
`hermes-gateway` restart, the PYTHONPATH drop-in `ExecStartPost` runs seven
yes/no checks (gateway+Job Engine PYTHONPATH, CDP `/json/version`, an
EZLynx `/web/` tab that is not login, Secret Manager ENABLED versions, no
`conversation_job_links.active=1` terminal bind, Chat intake / Pub/Sub
listener, Chat-runtime dests equal that zip). An every-day oneshot timer (`robie-production-preflight.timer`)
repeats that hourly from 7am through midnight `America/New_York`. The first
no posts one Robie Chat APP message to `spaces/AAQAZbLJO78` and the same
text to Carlo and Jake via the existing Chat APP poster
(`chat_app_post.post_as_chat_app` + `spaces.findDirectMessage` on an
already-existing DM). There is no outbound email API on `hermes-poc-01`.
It does not `@robie`, bind, or restart Chrome / `hermes-gateway` / the
browser.

The regression battery (`robie_job_engine/regression_battery.py`) is the
job-type / logic / Test-replay detector. It is not pre-flight and not
post-job audit. It **only guarantees previously seen failures have not
come back**. Simulator passed means nothing we have already seen is
wrong — known scenarios did not regress — never that nothing is wrong.
Every Production incident that was a NEW failure mode gets a named
deterministic scenario in `deploy/regression_battery/scenarios.json`
before we call the incident closed. That is how the simulator grows.

`ascend:locator-and-artifact-audit` is the named Test-only Job Engine
scenario for a NEW site/workflow (Ascend locator walk + quote-PDF
artifact path). `artifact-path:concat-job-id-eb96f620` is the
deterministic FAIL for Production job `eb96f620` (lookup was
`{job_id[:-11]}{artifact_id}/`; required `{full_job_id}/`).
`hitl-tone:dry-playwright-blocked` rewrites cowboy/slang HITL before
send. GitHub CI asserts the punch list and that the artifact
folder equals the job id. The live Ascend walk is `hermes-test-01` only.
This tree does **not** flip that job type onto Production. Commercial
auto already on Production is not a free pass. N=3 clean Test jobs still
applies. Loom `ascend-finance` is never overwritten.

`hermes-test-01` is **not** a clone of Production: different service-account
permissions, isolated Job DB / browser / ingress, and Test must not read
Production secrets. A Test all-clear is **not** a Production all-clear.
A check that cannot be proven on Test because of a known
permission / secret / browser gap is **INCONCLUSIVE** (never green).
Chat may note that once. The living diff list is
`deploy/regression_battery/parity.json` — update a row when a deploy or
HITL shows drift; do not treat it as a one-time snapshot.

GitHub Actions job `test` (same ROBIE verification gate family as
`canonical-paths`) runs the battery on every PR via `--ci`. That runner
never talks to EZLynx or `hermes-test-01`. After every Production zip
pointer flip + `hermes-gateway` restart, the same drop-in starts
`--notify --detach`. A NEW fail (not a known-accepted Test outcome, and
not DESTROYED-latest + older ENABLED which is HEALTHY) posts one Robie
Chat APP message to `spaces/AAQAZbLJO78` and may open a **draft** GitHub
PR. Flaky / timeout / network evidence does not open a PR. The same
failure signature comments on an existing unmerged auto-draft instead of
piling another. HITL resume after a gateway restart must re-lease the
same job (09d69760, da53765b, 6cf6f6ae) — no second job, no RETRY of
leftover failed ids. COMPLETE-shaped / I-did-it prose with 0 destination
evidence stays UNVERIFIED or FAILED (30777947, c31f9c69); Chat names
that class if it returns. Owners: Carlo + Jake. Jake Approves, Carlo
Confirms, Dusty pings if it sits. It does not `@robie`, bind, email the
insured, merge, flip Production, or restart `hermes-gateway`.

Post-job audit (`robie_job_engine/post_job_audit.py`) is also on the zip path.
`guard_chat_response` / `_render_chat_terminal` in `chat_guard.py` appends the
four-answer audit to the existing Robie Chat APP reply. Production Chat already
imports those functions via PYTHONPATH, so a Chat job close-out posts the audit
without a second `.hermes` adapter install. `JobEngine.run` and the scheduler
orphan path persist the same checkpoint. A zip-only adapter change would miss
Production Chat; this hook does not live only in `integrations/google_chat/`.

Dead-tab cleanup (`robie_job_engine/tab_cleanup.py`) is on the zip path with
the audit and pre-flight. COMPLETE / FAILED / UNVERIFIED close-out closes
leftover pages of any host that finished job opened — not only EZLynx account
/ login / `about:blank` / Ascend — via CDP `Target.closeTarget`
(`GET /json/close/{id}`). It does not restart Chrome, `hermes-gateway`, or
`robie-ezlynx-browser`, does not wipe the EZLynx profile, and does not log
out the shared session. A pre-flight flush (after the seven yes/no checks,
best-effort and never a pre-flight failure) closes every leftover page that
no RUNNING / AWAITING_HUMAN_INPUT / VERIFYING job claims, and keeps exactly
one authenticated `https://app.ezlynx.com/web/` session tab. Recorder and
Playwright select the current job tab with `select_recording_tab` /
`select_playwright_page` after cleanup — not `pages[0]` / first-ezlynx-wins.
The Playwright hint write still lives in
`deploy/hermes/tools/playwright_tool.py` (a second `.hermes` install).

PR 23 account-id nav (`robie_job_engine/ezlynx_account_nav.py`) is on the
zip path with `chat_guard` and `playwright_write_guard`. The `.hermes`
write-guard / skill / `SOUL.playwright.md` copies above are a second
install.

## Two distinct `.hermes` homes

Live check 2026-08-27 (morning, before the PR 23 overlay copy): **not the
same folder.**

| Path | inode | owner | mtime |
| --- | --- | --- | --- |
| `/home/streetsmart-hermes/.hermes` | 1918612 | `root:root` | Aug 19 |
| `/opt/streetsmart-hermes/.hermes` | 4220735 | `streetsmart-hermes` | Aug 27 |

`/home/streetsmart-hermes/.hermes` is `HERMES_HOME` in the gateway unit.

Plugin adapter exists **only** at
`/opt/streetsmart-hermes/.hermes/hermes-agent/plugins/platforms/google_chat/`
(`adapter.py`, `oauth.py`, `plugin.yaml`). The `/home` path has no that
directory.

The PR 23 overlay copy targeted `/opt/streetsmart-hermes/.hermes` dests
listed above. Inodes were not re-checked after that copy.

## What ignores a pointer-only zip flip

Official install now covers Chat adapter, oauth, and Playwright tools
(zip-load shims under `.hermes`). A pointer-only flip without that
script is still the PR 17 hole; `deploy_truth` will not call it done.

These still live in `.hermes` on purpose and are **not** part of the
official zip proof:

- Hermes skills under `.hermes/skills` (`robie-playwright-browser`,
  `ezlynx-commercial-auto-from-quote`, `ezlynx-gemini-fallback`, and
  user-owned Loom `ascend-finance`)
- `config.yaml` / SOUL / `playwright.yaml` merges
- browser profile `.hermes/browser-profiles/ezlynx`
- tokens and drive-skills snapshots
- `/srv/robie/current` + vendor still on `PYTHONPATH` after the zip
- flattened `/opt/streetsmart-hermes/robie-job-engine` (session unit still
  references it)
- Hermes-agent venv / `hermes_cli` / plugin loader (not in the zip)

## Live definition (Carlo 2026-08-27, tightened)

“Live” is **not** GitHub `main` alone, and **not** zip pointer alone.
Pointer-only is not live.

After every official Production zip, all of these must be true:

1. Both pointers match the SHA
   (`/opt/streetsmart-hermes/current` and
   `/opt/streetsmart-hermes/releases/current`).
2. `hermes-gateway` `ActiveEnterTimestamp` is **after** the pointer flip.
3. Chat load path equals that zip: each Chat-loaded dest
   (`adapter.py`, `oauth.py`, `playwright_tool.py`,
   `playwright_write_guard.py`, `gemini_field_tool.py`) is a zip-load shim
   or the same bytes/md5 as the zip file, and
   `robie_job_engine/gemini_field_helper.py` is on that zip PYTHONPATH.
4. An `install_proof` row exists in
   `/opt/streetsmart-hermes/robie-job-engine/data/jobs.db`. That row is
   not COMPLETE and does not authorize COMPLETE.
5. For a Chat job, a `checkpoints.kind=gateway_progress` row exists.

`scripts/install-official-release.sh` records `flip_at` even when not yet
done, then `prove` writes the `install_proof` row. Pointer-only and Chat
looking busy are not live. The row is never COMPLETE.

Skills stay in `.hermes` on purpose (Drive sync, including user-owned Loom
`ascend-finance`). They are a separate install. Official zip install does
not overwrite them.

Job success is separate: **destination-verified evidence rows > 0**. Chat
looking busy is not success.

## Official install (one source of truth for adapter / tools / Job Engine)

`scripts/install-official-release.sh` is the **only** supported Production
zip flip. Long typed SSH commands are not the install path. It does not
`git pull` on the VM. It does not bind. It does not print secrets. It
does not restart `hermes-gateway` or Chrome (operator restarts the
gateway after approval, then re-runs `prove`; `flip_at` is already
recorded so prove does not need a remembered timestamp).

What was two paths: pointer flip (Job Engine) plus a later hand copy into
`.hermes` (Chat adapter / Playwright tools). What is now one proof: the
same script flips both pointers, writes zip-load shims for every
Chat-critical Python file, and refuses `OFFICIAL INSTALL DONE` until
pointers + Chat dests + gateway-after-flip + `install_proof` row all
hold. A later zip flip then loads Chat from the new SHA without a
file-by-file `.hermes` patch.

What still lives in `.hermes` on purpose: Hermes-agent itself, plugin
loader, `plugin.yaml`, tokens, browser profile, config/SOUL merges, and
**skills** (Drive → `.hermes`, including Carlo’s Loom `ascend-finance`).

## Write-guard TimeoutError gap (c31f9c69 vs da53765b)

Job `da53765b` HITL'd correctly when unique-write raised `PLAYWRIGHT_BLOCKED`
on an ambiguous EZLynx field. Job `c31f9c69` (PAWIVA Ascend, 2026-08-28)
uniquely resolved `quotes.0.carrier_id` then sat on a Playwright
`TimeoutError` filling a hidden / combobox control until the worker timed
out with zero destination evidence (UNVERIFIED/FAILED). The write-guard only
wrapped locator uniqueness, not fill/click/select_option/type timeouts or
hidden / aria-hidden / not-visible / combobox-hidden targets, so the model
retried instead of asking Gemini then HITL Carlo.

Gemini stuck-field help (`gemini_field_helper.py` / `ezlynx-gemini-fallback`)
is for a stuck Playwright write on any site Robie drives (EZLynx, Ascend,
carrier portals, anything), not EZLynx-only. The prompt names the current
page (host/title); unique-write, no `.first`/`.nth`/`.last`, no bind, and
HITL Carlo stay the same.

## PR 17 lesson

17 added `heartbeat_generic_chat_job` in `store.py` (zip) and called it from
`integrations/google_chat/adapter.py` (zip copy). Production imports the
plugin adapter from `.hermes`, so the loop never ran. Job `e06d5e13` failed
at 300s with zero `gateway_progress` rows while Playwright kept running.

## EZLynx login secrets (on-call)

Job `6cf6f6ae` HITL'd on a DESTROYED Secret Manager version. Operator
runbook (no secret values): **[LOGIN_SECRETS.md](LOGIN_SECRETS.md)**.

Production `hermes-poc-01` / project `streetsmart-hermes-poc` reads
`ezlynx-username` and `ezlynx-password`. Live bootstrap uses newest
**ENABLED** by `create_time`, not `versions/latest`. A DESTROYED version
cannot be restored; add a new ENABLED version (Pawel), then RETRY in Chat.
Chat text does not change the version. The zip-path preflight
(`robie_job_engine/login_secret_health.py`, hooked from `open_chat_job` and
the scheduler) ALERTs / HOLDs only when there is no ENABLED version. A
DESTROYED latest leftover with an older ENABLED version is healthy (bootstrap
already uses newest ENABLED). There is no EZLynx password-rotation webhook
in this repo.
