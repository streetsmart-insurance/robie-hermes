---
name: ezlynx-session-login
description: Ensure Hermes's persistent Chrome profile has an authenticated EZLynx session using credentials stored in Google Secret Manager. Use for the scheduled 9 a.m. EZLynx login check or when an authorized operator asks Hermes to restore its EZLynx session. Do not use to bypass MFA, CAPTCHA, or account security controls.
---

# EZLynx Session Login

Maintain the server-owned EZLynx session without putting credentials in prompts, files, logs, or job payloads.

## Run the session check

Execute:

```sh
python -m robie_job_engine.ezlynx_session
```

The runtime checks the persistent Chrome session first. It reads `ROBIE_EZLYNX_USERNAME_SECRET` and `ROBIE_EZLYNX_PASSWORD_SECRET` from Google Secret Manager only when EZLynx is logged out, then submits the credentials through the local Chrome CDP endpoint.

Treat `SIGNED_IN` as success. If the command reports `INTERACTIVE_AUTH_REQUIRED`, tell the authorized operator that EZLynx requires MFA or another manual verification step. Never request the password in chat, expose secret payloads, solve a CAPTCHA, disable MFA, or claim that an expired session was restored.

The sole scheduled owner in this repo is `.github/workflows/monitor-ezlynx-session.yml`
(hourly, seven days a week). It checks the session and, only when the check
returns LOGGED_OUT, runs `ezlynx_login_bootstrap.py` against the existing
CDP at `127.0.0.1:9222`. One login attempt per check. Two consecutive
LOGGED_OUT checks with a readable prior state stop trying and fail loudly.
A missing or unreadable last-check file still allows one login but sets
`cap_state=UNEVALUATED` and prints that. Logout cause stays UNVERIFIED.
`robie-ezlynx-session.timer` was never installed on Production and is
deleted from this repo. `robie-chrome-refresh.timer` stays in-tree as
retired; the live copy is disabled, not removed. For rotation, read
[references/provisioning.md](references/provisioning.md).
