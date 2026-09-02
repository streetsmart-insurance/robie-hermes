# Team-lead Google Chat delivery

## Authorization and content boundary

Use an incoming Google Chat webhook only when the private runtime configuration records current administrator approval. Use it only from Test and only after the complete report package and recipient preflight succeed.

The Chat message may contain:

- the prior-business-day reporting date;
- links to the secured Google Doc, all-data workbook, dashboard, and runbook;
- a short fixed checklist asking leads to review callbacks, queues, overdue tasks, policy changes, COIs, Sales Center, submissions, and Magellan;
- a request for corrections or remarks.

Do not include client names, phone numbers, email addresses, account details, employee-level findings, raw rows, attachments, credentials, or the webhook URL. Those belong only in the access-controlled Drive deliverables and approved email.

## Secret handling

Never save the webhook in this skill or any shareable artifact. The sender resolves it at runtime in this order:

1. `STREETSMART_DAILY_REPORT_WEBHOOK_URL` environment variable, when supplied by an approved secret manager; or
2. macOS Keychain service `streetsmart-daily-accountability-google-chat`, with the account name supplied through private runtime configuration.

For Antigravity or a cloud runtime, inject the environment variable from that environment's secret manager. Do not paste the value into an Antigravity prompt, checked-in environment file, scheduler prompt, or command argument.

## Send and verification

Use `scripts/send_team_lead_webhook.py` with the reporting date and the freshly verified Drive links. The helper rejects non-Google Chat hosts, does not print the webhook, and prints only a compact delivery receipt.

Run `--dry-run` first when changing the message contract. A live send is successful only when Google Chat returns HTTP 200 and a message resource name. Do not retry more than once. If Chat delivery fails after the email was sent, preserve the email delivery and report a Chat delivery exception; never resend the email automatically.
