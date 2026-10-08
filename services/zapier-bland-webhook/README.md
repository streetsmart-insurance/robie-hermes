# Bland Dispatcher

Cloud Run webhook service that turns EZLynx note labels into Bland AI voice
calls. Replaces the stub at
`https://zapier-bland-webhook-751771086524.us-east1.run.app/webhook`
(which only returned `{"status":"received"}`).

## How it works

11 Zapier Zaps POST form-encoded to `POST /webhook` with
`applicant_id`, `label_name`, `label_id`, `campaign_id`, and (recommended)
`discussion_id` and `note_body`.
The service responds immediately with `{"status":"received"}` and dispatches
in a background thread:

| Campaign | Flow |
|---|---|
| `robie-call` | **Freeform.** Fetches applicant (name, phones) and policies from EZLynx; uses the **`note_body`** POSTed by the Zap (the "New Note" trigger's note text — the only channel that carries the CSR's actual words, since the EZLynx API never returns note bodies) as Eva's call instruction. Falls back to the **discussion title** via the v8 API when no note_body arrived (see "Instruction sources" below). Eva has FULL context and "runs with it" even when the instruction is vague ("call about renewal"). |
| `robie-lead-followup`, `robie-client-outreach`, `robie-cancellation`, `robie-audit`, `robie-returned-mail`, `robie-esign`, `robie-additional-info`, `robie-recommendations`, `robie-unresponsive`, `robie-renewal-reachout` | **Deterministic.** Looks up the applicant's phone, uses the campaign's approved canned script from `scripts_config.json`. A campaign with no approved script is a **hard stop** — it will not dial. |

Every call runs **Jake's double-dial voicemail policy** and **writes back to
EZLynx** (concise note via the Discussion API + MP3 via the Document API)
per Carlo's standing rule.

### Eva's call behavior (Jake's requirements, 2026-10-02)

- Voice: Karen (`29158307-9893-4149-8a75-bc9ce313d64e`).
- Eva identifies **up front** as an AI assistant calling on behalf of Jake
  from StreetSmart Insurance, then states the reason in one sentence.
- Screener handling: answer directly and slowly, repeat if asked, stay on
  the line until connected. Never hang up on a screener.
- End only for: person unavailable, or voicemail.
- Double-dial: 1st voicemail → silent hangup, retry after 10s; 2nd voicemail
  → full SLOW message with AI disclosure + callback number digit by digit.
- Callback number: 732-462-8343. Outbound caller ID: +17322986745.

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask app: `GET /health`, `POST /webhook` (form + JSON) |
| `dispatcher.py` | Campaign routing, freeform context building, dispatch orchestration |
| `bland_client.py` | Bland API (`POST /v1/calls`, `GET /v1/calls/{id}`), double-dial logic |
| `ezlynx_client.py` | EZLynx: Classic API (applicant, policies), Discussion API (notes), Document API (MP3) |
| `prompts.py` | Eva's prompt builders; deterministic script loader |
| `writeback.py` | EZLynx note formatting (no phone numbers — the Discussion API rejects them) |
| `scripts_config.json` | The 10 deterministic scripts (Carlo approved 2026-10-03) |
| `config.py` | Env-var configuration + fail-closed live-call gate |
| `Dockerfile` / `requirements.txt` | Cloud Run build |
| `tests/` | Unit tests: routing, freeform context, redial logic, writeback formatting, v8 parsing, script wiring |

## Instruction sources (freeform "Robie Call" flow)

**The problem:** EZLynx's API never returns note *bodies* or *labels*
(proven by live probe 2026-10-03 — see `label-probe-results.md`).
Zapier's "New Note" trigger is the only place that sees the note text,
so the **Zap POSTs it to the webhook** as `note_body` (alias: `note_text`).
That is Eva's primary instruction — the actual words the CSR typed.

**Priority order in code:**

1. **`note_body` from the webhook (primary).** The Zap maps the New Note
   trigger's note text into this field. When present, the dispatcher uses
   it verbatim and skips any discussion API lookup for the instruction.
2. **Discussion title via the v8 API (fallback).** Used only when the Zap
   didn't send `note_body`. The dispatcher reads the title of the
   `discussion_id` (or the most recently modified discussion) and uses it
   as the instruction.
3. **Context only.** If the title is empty or still says "Untitled", Eva
   proceeds with just the applicant's policies/info, and the writeback
   notes that no specific instruction was given.

`discussion_id` is still recommended in every POST — it's where the
writeback puts the outcome note (next to the instruction that triggered
the call), regardless of which instruction source was used.

**For staff:** just write the note naturally. Whatever you type in the
note body is what Eva gets. The discussion title is only a fallback —
naming the discussion with the instruction still works, but it is no
longer required.

## Environment variables

| Var | Required | Default | Purpose |
|---|---|---|---|
| `PORT` | no | `8080` | Set by Cloud Run |
| `DRY_RUN` | no | `1` | `1` = log only, never dial. Set `0` for live. |
| `ROBIE_VOICE_AUTODIAL_LIVE` | for live | — | Must be `1` for live calls (fail-closed) |
| `ROBIE_HALT` / `ROBIE_READ_ONLY` | no | — | Kill switches; block live calls when set |
| `BLAND_API_KEY` | for live | — | Bland API key |
| `VOICE_ID` | no | Karen's id | Bland voice |
| `FROM_NUMBER` | no | `+17322986745` | Outbound caller ID |
| `CALLBACK_NUMBER` | no | `732-462-8343` | Spoken digit-by-digit on 2nd voicemail |
| `TRANSFER_NUMBER` | no | `+17324622360` | Carlo (live transfer) |
| `REDIAL_DELAY_SECONDS` | no | `10` | Jake's double-dial delay |
| `EZLYNX_USER` / `EZLYNX_PASSWORD` / `EZLYNX_APP_SECRET` | yes | — | Classic API (applicant + policies) |
| `EZLYNX_OAUTH_CLIENT_ID` / `EZLYNX_OAUTH_CLIENT_SECRET` / `EZLYNX_OAUTH_USERNAME` / `EZLYNX_INTEGRATION_GROUP_ID` | yes | — | OAuth2 (Discussion + Document APIs) |

## Local dry-run testing

```bash
cd ~/workspace/bland-dispatcher
python3 -m pytest tests/ -q          # 48 unit tests
DRY_RUN=1 python3 app.py            # serves on :8080, never dials

# In another shell:
curl localhost:8080/health
curl -X POST localhost:8080/webhook \
  -d "applicant_id=12345&label_name=Robie%20Call&campaign_id=robie-call&discussion_id=disc-1&note_body=Call%20Jane%20about%20her%20auto%20renewal"
# -> {"status":"received"}; the dry-run Bland payload is logged.
# discussion_id is recommended (writeback target); note_body is the
# freeform instruction (fallback: the discussion title via the v8 API).
```

Dry-run proves: routing, prompt construction, the exact Bland payload that
would be sent (voice, from-number, voicemail actions), and the hard stops
(unknown campaign, unapproved script, missing phone).

## Deterministic scripts (wired 2026-10-03)

All 10 scripts are live in `scripts_config.json` — Carlo approved the
wording ("let's get these going"). Each entry has `reason_sentence` (one
sentence, spoken right after Eva's AI disclosure) and `script` (Eva's
opener, which she follows exactly).

To change a script later: edit its entry in `scripts_config.json` and
redeploy. The dispatcher loads the file at import; a campaign whose
script is missing or still says `PENDING` refuses to dial (hard stop).
```

## Deploy to Cloud Run

```bash
cd ~/workspace/bland-dispatcher

# One-time: create the service (us-east1, same project as the current stub)
gcloud run deploy zapier-bland-webhook \
  --source . \
  --region us-east1 \
  --project streetsmart-hermes-poc \
  --allow-unauthenticated \
  --set-env-vars "DRY_RUN=1" \
  --set-secrets "BLAND_API_KEY=bland-api-key:latest, \
EZLYNX_USER=ezlynx-classic-user:latest, \
EZLYNX_PASSWORD=ezlynx-classic-password:latest, \
EZLYNX_APP_SECRET=ezlynx-app-secret:latest, \
EZLYNX_OAUTH_CLIENT_ID=ezlynx-oauth-client-id:latest, \
EZLYNX_OAUTH_CLIENT_SECRET=ezlynx-oauth-client-secret:latest, \
EZLYNX_OAUTH_USERNAME=ezlynx-oauth-username:latest, \
EZLYNX_INTEGRATION_GROUP_ID=ezlynx-integration-group:latest"

# Later deploys (new code or approved scripts):
gcloud run deploy zapier-bland-webhook --source . \
  --region us-east1 --project streetsmart-hermes-poc

# Go live (ONLY after Carlo approves scripts + a dry-run review):
gcloud run services update zapier-bland-webhook \
  --region us-east1 --project streetsmart-hermes-poc \
  --update-env-vars "DRY_RUN=0,ROBIE_VOICE_AUTODIAL_LIVE=1"
```

> **Do not set `DRY_RUN=0` until Carlo has approved all 10 scripts and
> reviewed a dry-run.** Live calls are fail-closed by default.

## Smoke test after deploy (dry-run)

```bash
BASE=https://zapier-bland-webhook-751771086524.us-east1.run.app
curl $BASE/health
curl -X POST $BASE/webhook \
  -d "applicant_id=TEST_APPLICANT&label_name=Robie%20Call&campaign_id=robie-call&discussion_id=disc-1&note_body=Test%20instruction%20from%20the%20note"
gcloud run services logs read zapier-bland-webhook \
  --region us-east1 --project streetsmart-hermes-poc --limit 30
# Expect: "webhook hit", "[DRY_RUN] would place call", no Bland POST.
```

## Production hardening (2026-10-03 audit)

Fail-closed safety:
- **Live gate enforced in `dispatch()`**: on the production path
  (`dry_run=None`), live calls additionally require
  `ROBIE_VOICE_AUTODIAL_LIVE=1`, no `ROBIE_HALT`/`ROBIE_READ_ONLY` kill
  switch, and a Bland API key. If the gate fails, the dispatch is forced
  to dry-run and `live_blocked_reason` is recorded. (An explicit
  `dry_run` parameter is a test seam and bypasses the gate; `app.py`
  never passes one.)
- **No phone, no call**: garbage phone values ("N/A", "0") are filtered;
  a missing phone fails the dispatch with `no phone number on applicant`.
- **Unknown campaign rejected at the edge**: the webhook 400s unknown
  `campaign_id`s instead of queueing them; non-numeric `applicant_id`s
  are 400-rejected too.

Reliability:
- **Retries with exponential backoff** (3 tries, 1s/2s/4s) on transient
  failures for all EZLynx calls (`get_applicant`, `get_applicant_policies`,
  `get_discussions`, `append_note`, `upload_document`) and Bland
  `POST /v1/calls` (also 429/5xx). Other 4xx fail fast.
- **OAuth refresh on 401/403**: `get_discussions` forces one token
  refresh before giving up (the 2026-10-03 SSRobie failure class).
- **Bland circuit breaker**: after 5 consecutive `place_call` failures,
  calls short-circuit for 120s instead of hammering a down API.
- **Idempotency**: Zapier retries the same
  `(applicant_id, campaign_id, discussion_id)` within 30 minutes are
  suppressed with `202 {"status": "duplicate_suppressed"}` — a retry
  never places a second call. (Per Cloud Run instance; multi-instance
  deploys share nothing.)
- **Writeback correctness**: the deterministic flow now writes to the
  Zap's `discussion_id` (was: silently fell back to most-recent). If the
  trigger discussion is gone, it falls back to most-recent with a
  warning; if the discussion list itself was unreachable, it attempts
  the trigger ID anyway and surfaces the POST result.
- **Honest results**: `ok` is now `call_ok AND note_ok`. Bland failures
  surface as `call_error`; note failures as `writeback_error`; MP3
  failures as `recording_error` (surfaced, does not flip `ok`).

Input hygiene:
- `note_body` capped at 2000 chars before it becomes Eva's prompt.
- `applicant_id` is URL-quoted in all EZLynx paths.

### Outcome health check (Carlo's standing rule)

`GET /health/deep` verifies the outcome path, not just liveness: secrets
present, all 10 scripts loaded (none PENDING), EZLynx Classic auth + phone
lookup, EZLynx OAuth token, a full dry-run dispatch, Bland key present.
Returns 200 `{"status": "ok"}` or 503 `{"status": "degraded"}`.

`scripts/outcome_health_check.py` probes `/health/deep` and posts a
plain-English alert to the ROBIE health Chat on failure. Wire it on a
daily Cloud Scheduler job (exact commands in the script header), then
prove both directions per the standing rule:
`--test-quiet` (stays silent when healthy) and `--test-alert` (alert
path fires).

## Kill switch (instant, no redeploy)

Two independent halt mechanisms; **either one** stops all calls:

1. **Secret Manager (instant)** — the dispatcher reads the secret
   `bland-dispatcher-kill-switch` on every dispatch (cached 60s).
   Flip it without redeploying:
   ```bash
   # HALT all calls immediately:
   echo -n "1" | gcloud secrets versions add bland-dispatcher-kill-switch \
     --data-file=- --project=streetsmart-hermes-poc
   # Resume:
   echo -n "0" | gcloud secrets versions add bland-dispatcher-kill-switch \
     --data-file=- --project=streetsmart-hermes-poc
   ```
   First use: `gcloud secrets create bland-dispatcher-kill-switch
   --replication-policy=automatic --project=streetsmart-hermes-poc`,
   then grant the Cloud Run service account
   `roles/secretmanager.secretAccessor` on it. A missing/unreadable
   secret is treated as OFF (fail-open on the switch itself, fail-closed
   on the call — the live gate still applies).
2. **Env vars (needs redeploy)** — `ROBIE_HALT=1` or `ROBIE_READ_ONLY=1`.
   Kept as a backup; the secret is the fast path.

When halted, `dispatch()` returns immediately:
`{"ok": False, "error": "kill switch active"}` — no Bland call, no
writeback, and the halt is logged.

## Chat alerting policy

Alerts go to the ROBIE health Chat via `ROBIE_HEALTH_CHAT_WEBHOOK`
(Secret Manager: `accountability-robie-health-chat-webhook`). The policy
is deliberately anti-spam:

| Event | Chat post? |
|---|---|
| Single dispatch failure | No — logged; the dispatch result carries the error. (The EZLynx note shows the failure if the writeback itself succeeded.) |
| 3+ failures in 15 min | **Yes** — `"Bland dispatcher: [N] failures in last 15 min. Last error: [error]. Campaign: [campaign], Applicant: [applicant]."` Counter resets after alerting (max one alert per 15-min window). |
| Bland circuit breaker trips | **Yes** — `"Bland API circuit breaker tripped — calls paused for 120s."` Once per trip, not per failure. |
| Call succeeded but EZLynx writeback failed | **Yes, always** — `"Bland call completed but EZLynx writeback failed. Applicant: [id], Campaign: [campaign], Call IDs: [ids]. Manual note may be needed."` This is silent data loss without the alert. |
| Two different labels for one applicant within 5 min | **Yes** — `"Two different labels fired for applicant [id] within 5 min: [campaign1], [campaign2]. Both calls placed."` Both calls still go out; the alert is visibility, not a block. |

Alert delivery never raises — if the webhook is missing or the POST
fails, the failure is logged and the dispatch path continues.

## MP3 recording note

The EZLynx outcome note now states the recording status explicitly:
- Uploaded → "Recording saved to the applicant's Documents tab."
- Failed → "Call recording unavailable — the MP3 upload failed."
- Not yet available → "Call recording not yet available."
- Skipped (dry-run / no call) → no recording line at all.

### Known limitations (not fixed)
- **Daemon threads**: dispatch runs in a background thread; if Cloud Run
  scales the instance to zero mid-call, the dispatch dies silently.
  Mitigate with `--min-instances=1`.
- **Late MP3s**: if Bland hasn't produced the recording URL yet, the
  recording is marked `pending` — there is no async follow-up to fetch
  it later.
- **Idempotency is per-instance** (in-memory); a multi-instance deploy
  could double-process if Zapier retries hit different instances.
- **Deep health is read-only**: it cannot verify `append_note` without
  writing a note, so the write path is only proven by live tests.
