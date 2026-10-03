# End-state report (Jev)

This is off until someone turns it on. With the flag off, Production
replies stay as they are today. Setting ``ROBIE_END_STATE_REPORT=1``
turns the report on in that process, including Production.

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

The flag is off unless it is set. Setting `ROBIE_END_STATE_REPORT=1` turns the report on in that process, including when `ROBIE_ENV` is `PRODUCTION`, `PROD`, or `LIVE`. This document does not install the flag anywhere.

## Write jobs (plan, readback, Jev)

Chat and email jobs that change EZLynx run three steps. A question does not.

1. Before any write, the model states a plan: the write, the target, and the values. That plan is locked. A note, document, or policy-setup tool refuses the write until the plan is locked. Set `ROBIE_PLAN_MODEL=1` on Test to have Gemini (`gemini-api-key`) state that plan before the agent starts. The plan call allows 4096 tokens and asks for JSON. A cut-off object is repaired, then the model is tried once more. A string `target` is accepted (an account id, a policy number, or a discussion name). The refusal names the field that is wrong. The same field is refused at most twice; the next reply stops and the user is told nothing was written. Without the flag, the model states the plan on the write tool call, and the tool locks it before the API post. The flag does not invent values. A write that lands nothing tells the user `Nothing was changed or noted` and the reason, in one line.
2. At the end, an EZLynx API readback compares each planned value. The comparison does not use the model's claim. A discussion note is re-read by applicant id and discussion id (the discussion is fetched and the note text is matched). A policy number is required only for a policy-level write. On Test this runs because `ROBIE_EZLYNX_DISCUSSION_API=live`. `ROBIE_EZLYNX_API_READBACK=1` turns the same read on elsewhere. If the read does not run, the reply says so. It does not treat the claim as a pass.
3. Jev scores that end state as correct, wrong, or unsure. The key is Secret Manager secret `jev-api-key`. A failed readback forces wrong. Jev does not override it.

The user reply quotes the readback and Jev's score. It does not repeat the model's claim. This loop is not the `ROBIE_END_STATE_REPORT` flag.

## What is stored

Each score is a row in `jev_evaluations` in the job database, plus a `jev_score` checkpoint. That is the tuning log. The API key is not stored.
