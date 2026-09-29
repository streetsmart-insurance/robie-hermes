# End-state report (Jev)

This is off until someone turns it on. Production does not change.

## What Carlo sees

When the flag is on, every finished Chat or email job replies with one plain-English report:

1. One sentence: what was asked, and what Robie did.
2. End state: what Robie actually observed at the end, in CSR language.
3. Jev's verdict: correct, wrong, or unsure, with a confidence percent and one reason.
4. Details: the checks that ran, screenshot paths, and URLs.
5. `Ref: job <full job id>` on the last line.

Jev (TypeSafe) scores the ask, the ending, the readbacks, and the worker's own claim. If Jev cannot be reached, the verdict is `unsure (Jev unavailable)` and the job escalates. That is not a pass.

A failed hard check still forces **wrong**. Hard checks are the EZLynx documents, notes, and applicant API readbacks, Sent-folder or message-id checks, and documents/activities duplicate checks. Jev does not override a failed one.

Money, bind, and other dangerous-action refusals stay on the existing refusal note. This report does not approve those actions.

With the flag off, replies stay exactly as they are today (the simple "What happened / Anything needed / Status" note from PR 663).

## When Carlo gets a ping

Robie escalates through the existing Gemini-first Chat path, once, when any of these is true:

- the verdict is wrong
- the verdict is unsure
- confidence is under the threshold (default 70%)
- Jev and a deterministic readback disagree

Gemini rescue is unchanged. The job reply is still the end-state report. The extra ping is one Chat message, not a second email.

## How to turn it on (Test only)

Host: `hermes-test-01`. App root: `/opt/streetsmart-hermes-test`. Do not do this on `hermes-poc-01`.

The Chat gateway unit on Test is `robie-gateway.service`. Confirm the active unit before writing a drop-in (`systemctl list-units '*gateway*'`). If email replies come from a different unit, that unit needs the same variable.

```sh
# STOP if this host is Production.
test -d /opt/streetsmart-hermes-test || { echo "STOP: Test root missing"; exit 1; }
echo "ROBIE_ENV=${ROBIE_ENV-<unset>}"

install -d /etc/systemd/system/robie-gateway.service.d
install -m 0644 deploy/systemd/end-state-report-test.conf \
  /etc/systemd/system/robie-gateway.service.d/end-state-report.conf
systemctl daemon-reload
systemctl restart robie-gateway
systemctl show robie-gateway -p Environment --no-pager
```

The drop-in sets `ROBIE_END_STATE_REPORT=1`. Optional: `ROBIE_END_STATE_CONFIDENCE_THRESHOLD` (default 70).

The Jev key is Secret Manager secret `jev-api-key` in project `streetsmart-hermes-poc`. The Test process reads it the same way it reads `gemini-api-key`. `JEV_API_KEY` overrides that if set. Do not put the key in the drop-in, in chat, or in logs.

The code ignores the flag when `ROBIE_ENV` is `PRODUCTION`, `PROD`, or `LIVE`. Turning this on in Production later needs Carlo's GO and a follow-up change. Setting the variable on hermes-poc-01 does not change replies.

## What is stored

Each score is a row in `jev_evaluations` in the job database, plus a `jev_score` checkpoint. That is the tuning log. The API key is not stored.
