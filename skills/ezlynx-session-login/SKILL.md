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

The daily timer is `robie-ezlynx-session.timer`. For initial setup or credential rotation, read [references/provisioning.md](references/provisioning.md).
