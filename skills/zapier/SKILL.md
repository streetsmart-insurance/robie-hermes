---
name: zapier
description: >-
  Fire StreetSmart EZLynx follow-up tasks through the agency's existing Catch
  Hook Zap (Inbox Triage -> EZLynx Follow-up Task, Zap 380066050). Use when
  Hermes needs zap-trigger / robie_job_engine.zapier_tasks.fire_task. Never
  print the webhook URL.
---
# Zapier EZLynx follow-up tasks

Agency Catch Hook Zap: **Inbox Triage -> EZLynx Follow-up Task** (`380066050`).
Do not create a new Zap. This skill only posts to the existing hook.

## Script

```
bin/zap-trigger --payload '<json>' [--dry-run] [--applicant-verified]
```

Required payload keys: `applicant_id`, `task_title`, `assignee`, `source`,
`due_date` (ISO `YYYY-MM-DD`). The script re-checks these even though
`robie_job_engine.zapier_tasks.validate_task_payload` already does, so a direct
caller (e.g. `confirmation_notify`) cannot skip validation.

`assignee` must be an EZLynx **login**, never a display name:
Nicole Segovia -> `SSNicole`. Values containing whitespace are refused.

Output: exactly one JSON object on stdout, `{"ok": bool, ...}`.

| Exit | Meaning |
|---|---|
| 0 | ok (posted, or dry-run validated) |
| 1 | Zap/HTTP/network failure |
| 2 | local config or payload error (nothing was sent) |

## Webhook URL

Read from the first match, never printed, never committed:

1. env `ZAPIER_CATCH_HOOK_URL`
2. env `CUSTOM_ZAPIER_WEBHOOK`
3. env `HERMES_CUSTOM_ZAPIER_WEBHOOK`
4. file `$HERMES_HOME/secrets/custom.zapier-webhook` (default `~/.hermes/...`)
5. file `/workspace/.secrets/zapier-catch-hook.url`, then `~/workspace/.secrets/zapier-catch-hook.url`

Only `https://hooks.zapier.com/` URLs are accepted.

## Hard stops

- Run `--dry-run` first on any new host.
- Never invent an applicant id; pass `--applicant-verified` only when the id is proven in EZLynx.
- A fired task is a claim, not evidence. Confirm the task exists in EZLynx before reporting it done.
- Production install needs Carlo's GO. Test first.
