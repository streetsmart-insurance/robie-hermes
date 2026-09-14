# HITL email / signBlob 403

## Root cause

HITL email uses `accountability_delivery._delegated_gmail_sender` →
`google.auth.iam.Signer` → IAM Credentials **`signBlob`** on the
domain-wide-delegation SA.

On `hermes-poc-01`:

| Piece | Value |
|-------|--------|
| Caller (ADC / VM default SA) | `hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com` |
| DWD / signBlob target SA | same (`ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT`) |
| Mailbox | `robie@streetsmart.insurance` |
| Required IAM | caller needs `roles/iam.serviceAccountTokenCreator` **on** the DWD SA |

Missing Token Creator → HTTP 403 on `signBlob`. That is an Owner IAM grant,
not a git change (called out in PR #394). Chat HITL is a separate channel;
email failure must not authorize continue.

## Owner grant / verify

```bash
bash scripts/grant-gmail-signblob-token-creator.sh verify
bash scripts/grant-gmail-signblob-token-creator.sh grant   # if MISSING
bash scripts/grant-gmail-signblob-token-creator.sh probe-prod
```

`probe-prod` signs, sends one message to `carlo@`, and requires `SENT`
read-back on the message id.

## Code honesty

`ping_carlo` / `_deliver_hitl` always return email error text and
`logger.warning` on email failure — even when Chat still posts
(`hitl_posted=true`). Failures must not go quiet again.

## Verified 2026-09-14 (Prod)

- `signBlob_OK` as gateway identity
- Sent message id `1a0a234bc6383517` — Subject
  `[ROBIE PROBE] HITL email delivery check 2026-09-15`,
  To `carlo@streetsmart.insurance`, From `robie@…`, labelIds include `SENT`

## Test box gap

`hermes-test-01` `/etc/streetsmart-hermes-test/robie-message-runtime.env`
has no `ROBIE_VERIFICATION_*` / `ACCOUNTABILITY_GMAIL_DELEGATED_*` keys.
Test HITL email fails closed on missing config until Carlo mirrors Prod
env + Token Creator for the Test VM SA.
