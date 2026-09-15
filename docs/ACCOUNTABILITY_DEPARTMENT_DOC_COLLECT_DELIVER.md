# Department-tab Google Doc collect → publish → deliver

This is the only official weekday accountability report. It is the persistent
Team Lead Doc on `streetsmart-accountability-prod`, emailed Monday–Friday at
9:00 AM Eastern. It is not a MacBook Air High-Risk SLA digest and not the
Hermes Job Engine markdown path (`scripts/run_accountability_job.py` /
`streetsmart-accountability.timer`).

Persistent Doc: `1lnhIplYM8DLdFyITL9tCUVrImYzYy1Vbfax7eeAkdhg`

Subject: `StreetSmart Yesterday Accountability — YYYY-MM-DD`

From: `robie@streetsmart.insurance`

## Schedule

| Step | Eastern time | Command |
| --- | --- | --- |
| Collect | 6:45 AM | `scripts/run_accountability_pipeline.py --collect` |
| Publish + deliver | 9:00 AM | `scripts/run_accountability_pipeline.py --publish --deliver` |
| Late-check | 9:20 AM | `scripts/run_accountability_pipeline.py --late-check` |

9:00 AM retries collect when the 6:45 snapshot is missing. Late-check looks
for `success.json` and must say 9:20 AM Eastern, not 6:50.

## Fixes in this candidate

1. **Sep 10 collect exit 2.** A Gmail `format=metadata` read has headers only.
   The collector must not treat the four EZLynx scheduled-report subjects as
   missing just because that metadata payload has no MIME parts. Headers are
   one read; attachment filenames and IDs are a second, field-masked
   `format=full` structure read that never requests MIME `data`.
2. **Sep 9 `delivered=false` after `gmail.send`.** Delivery credentials are
   `gmail.send` + `gmail.metadata`. Sent-id read-back must use
   `format=minimal`. `format=full` needs `gmail.readonly` and produced a false
   delivery failure after a successful send.

Exact EZLynx subjects (do not weaken):

- `Robie - EZLynx Activities`
- `Robie - EZLynx Overdue Tasks`
- `Robie - EZLynx Sales Center`
- `Robie - EZLynx Policy Changes`

Sender: `Applied Reporting <DoNotReply@appliedsystems.com>`. Expected weekday
arrival is 4:30 / 5:00 / 5:30 AM Eastern.

## Out of scope for this PR

- No merge, deploy, Production SSH, or live leadership email from Engineering.
- This candidate records the existing Doc URL. It does not build a second
  Hermes or MacBook digest and does not enable `streetsmart-accountability.timer`.
- The standalone `/opt/streetsmart-daily-accountability` tab rewriter is not
  in this git tree. Promote to `streetsmart-accountability-prod` only after QA,
  not through the `hermes-poc-01` installer.
- Weekday EZLynx subscriptions to `robie@` before 9:00 AM remain a human ops
  requirement. This repair stops a false-missing collect when the mail is
  present.
