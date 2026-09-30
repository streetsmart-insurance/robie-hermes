# Email watcher limits and the Chat turn ceiling

## Where the ~10 minute cutoff lives

`scripts/robie_email_agent.py` kills the email child at **930 seconds**.
That is not what stopped the certificate and mailing-address jobs.

The earlier stop is `robie_job_engine/email_agent_runner.py`: the Hermes
`chat` subprocess timeout. The first attempt defaults to **600 seconds**
(10 minutes). The recovery attempt stays 300 seconds. When the 600 second
limit fires, the log and the job text say, in plain English, that the
chat step hit its time limit.

Set `ROBIE_EMAIL_AGENT_TIMEOUT_SECONDS` to change the first-attempt limit.
Do not lower `agent.max_turns` in `config.yaml`. The email agent and Chat
share that file.

## Chat total-time ceiling

`gateway/run.py` in hermes-agent only has an idle timeout
(`agent.gateway_timeout`, default 1800 seconds). This repo does not contain
that file. The Chat adapter enforces a separate total ceiling,
`agent.gateway_max_turn_seconds`, default **600**. Override with
`ROBIE_GATEWAY_MAX_TURN_SECONDS`.

The gateway's `handle_message` starts the agent in the background and
returns. The Chat handler must return too. The gateway reads one Chat
message at a time (`GOOGLE_CHAT_MAX_MESSAGES` stays 1), so a ceiling wait
inside the handler blocks `/stop` until the job ends. The ceiling is a
background watchdog. It does not hold the message slot. When the limit
fires, or someone sends `/stop`, the adapter looks up the session key the
runner is actually holding (`agent:main:google_chat:dm:spaces/...`) and
calls `_interrupt_and_clear_session` on that key. A derived
`chat:spaces/...` key is logged beside it and is not the key that is
cancelled. That stops the agent loop and releases the turn lease. The adapter then cancels the
session task, drops the session guard, and SIGKILLs the browser and
recording process group (the capture process, ffmpeg, and the Playwright
node). A stopped job cannot post another message or start another tool
call. The thread gets "I stopped after 10 minutes." or the cancelled
reply, and that post is stored as a `chat_delivery` checkpoint when
Google returns a message id.

A new message in a busy session does not cancel the running job. Only
`/stop` and the ceiling cancel. The new message gets "I'm finishing
another job, one moment." and waits until that session is free. A
message in a different session is not queued behind it.

A gateway restart fails Chat jobs still marked RUNNING, even if they have
a recent heartbeat. That heartbeat belonged to the process that just died.

## Email watcher service

`hermes-email-watcher.service` needs the environment in
`deploy/systemd/hermes-email-watcher.env.example`.

- `ROBIE_CHAT_SA_KEY_FILE` — path to the Chat app service-account key on a
  protected mount. Do not commit the key.
- `ROBIE_CHAT_APP_CLIENT_EMAIL` — must match that key's client email.
- `ROBIE_EMAIL_WORKER_CONCURRENCY` — default 2. Each email is its own job.
  EZLynx-writing jobs still take the existing session lock.

Help requests from the watcher email Carlo at carlo@streetsmart.insurance.
They do not email clients. If the Chat key is unset, the watcher does not
call Chat (that path sends a "[ROBIE] Chat app identity failure" mail) and
uses the email sender instead.
