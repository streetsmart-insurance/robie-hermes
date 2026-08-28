# Hermes cloud desk

Carlo can open this project from any phone or computer. Any LLM (Dusty / Grok, Cursor, ChatGPT, Claude) continues from the same three doors. There is no public SSH. Do not put keys or 2SV in chat.

## 1. Session (Dusty)

This Grok Bot chat is the living session. Open Dusty on desktop or phone. Do not start a second chief-of-staff thread for the same Hermes job.

## 2. Code (any LLM)

- Repo: https://github.com/streetsmart-insurance/robie-hermes
- Cursor cloud agents (phone or desktop): https://cursor.com/agents — pick `streetsmart-insurance/robie-hermes`
- Full operator packet: [HANDOFF.md](HANDOFF.md)

GitHub `main` is the recipe book. A branch or PR is not Production.

## 3. SSH (any browser)

Google Cloud Shell, same login Carlo already has:

https://shell.cloud.google.com/?show=terminal&project=streetsmart-hermes-poc

```
gcloud config set account carlo@streetsmart.insurance
gcloud config set project streetsmart-hermes-poc
gcloud compute ssh hermes-test-01 --zone=us-east1-b --tunnel-through-iap --ssh-key-file=$HOME/.ssh/hermes-nopass
```

Use `hermes-poc-01` only for a proven Production zip. First command after login: `hostname`. If Cloud Shell sticks on Establishing connection: menu ⋮ → Restart once, Authorize once.

Pawel owns durable IAP / OS Login. Do not open a public SSH port.

## What ChatGPT / Grok / Claude should do first

1. Read [HANDOFF.md](HANDOFF.md) and this file.
2. Do not `@robie` a live PAWIVA job.
3. Confirm Test still points to `4df60a0955f3` and that
   `robie-gateway.service` entered active after the pointer flip.
4. Do not enable the Ascend API worker until a sandbox key is stored in an
   isolated Test Secret Manager secret. Never paste the key.
5. Run one non-PAWIVA sandbox API create/read-back Job and record its clean
   Test pass before considering Production.
6. Keep the Playwright locator audit separate for Import document UI testing;
   it stops before Save and does not prove the API creation path.
