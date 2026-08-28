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
3. Finish the Test zip of current `main` on `hermes-test-01` if pointers are not that SHA.
4. Re-run the live Test audit with a Test quote (`--quote`). Carrier / State / Coverage come from that doc.
5. Production zip only after a recorded clean Test punch-list PASS.
