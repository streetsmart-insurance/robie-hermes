# ROBIE and Codex approval boundaries

## ROBIE application approvals

ROBIE may send Google Chat cards for a specific durable Job or release gate.
The server stores the authorized users, permitted choices, exact Job checkpoint,
expiry, and optional session scope before the card is sent. A click is accepted
only when the signed Google Chat actor matches that stored allowlist. Client
parameters cannot widen the stored scope.

Accepted choices are idempotent and auditable. Conflicting, expired, malformed,
or unauthorized events do not resume the Job. A denial stops the Job. An
approval resumes only the exact paused checkpoint; it never proves that the
underlying EZLynx action succeeded and cannot bypass independent verification.

## Codex security approvals

ROBIE and Zapier must not approve Codex sandbox, filesystem, network, account,
or destructive-action prompts. Those approvals remain inside the Codex client.
Google Chat can notify Carlo that input is needed, but the safe response path is
Codex desktop or Codex Remote in the ChatGPT mobile app.

## Notification behavior

- Send a Google Chat DM only when a genuine human decision is required.
- Include the Job/release name, environment, requested action, risk, expiry,
  and a link to the relevant evidence or review page.
- Do not send secrets, raw credentials, cookies, browser-profile data, or full
  model context.
- Rate-limit duplicate notices and persist delivery evidence.
