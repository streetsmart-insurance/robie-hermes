# streetsmart-accountability-prod units

Copies of the systemd units on the dedicated accountability VM
`streetsmart-accountability-prod`, read with `systemctl cat` on 2026-09-24.
They live in this directory so they never overwrite hermes-poc-01's
`deploy/systemd/streetsmart-accountability.service`, which is a different unit
on a different host.

Files map to `/etc/systemd/system/` on the VM. The only change from the live
copy is the new drop-in `30-intentional-stop-not-failure.conf`
(`SuccessExitStatus=80`), paired with the SIGTERM trap in
`scripts/run_daily_accountability_vm.sh`. Nothing here is installed
automatically.

## Team Lead Chat EOD

`streetsmart-accountability-team-lead-chat-eod.timer` is Mon–Fri 17:05
America/New_York. It is a separate oneshot from morning publish and from
`streetsmart-accountability-collect-evening.timer` (17:00 collect only).
Do not point the evening collect unit at this service, and do not add
`--publish`, `--deliver`, or Gmail to the EOD unit.

Copy onto the VM before `systemctl enable`, without replacing
`production_main.py` or the morning email reporter:

- `src/reporters/team_lead_chat.py` (webhook helper). If that path already
  posts the morning Chat message, diff first and keep the morning formatter
  calling `post_team_lead_chat`.
- `src/reporters/team_lead_chat_eod.py`
- `scripts/run_team_lead_chat_eod.sh` (mode `0755`)
- `streetsmart-accountability-team-lead-chat-eod.service`
- `streetsmart-accountability-team-lead-chat-eod.timer`
- `streetsmart-accountability-team-lead-chat-eod.service.d/30-intentional-stop-not-failure.conf`

Then `systemctl daemon-reload` and `systemctl enable --now streetsmart-accountability-team-lead-chat-eod.timer`.
The webhook secret is `accountability-team-lead-chat-webhook`. Optional env
`TEAM_LEAD_CHAT_WEBHOOK_SECRET` may name a different secret id. This unit
does not send leadership email.

## Evening source collection

`streetsmart-accountability-collect-evening.timer` stays Mon–Fri 17:00
America/New_York and must keep executing `run_source_collection_vm.sh` only.
Do not retarget it at the 17:05 Chat unit and do not add `--publish` or
`--deliver`.

`production_main` still defaults to the previous business day when `--date`
is omitted. The evening script passes today's America/New_York date so
`--skip-if-prepared` looks at that day's snapshot. The 09:00 unit keeps
`run_daily_accountability_vm.sh` with no `--date`.

Copy onto the VM before the next weekday 17:00, without replacing
`production_main.py`, `src/engine/date_utils.py`, or `src/engine/__init__.py`:

- `src/engine/collection_target.py`
- `scripts/run_source_collection_vm.sh` (mode `0755`)

Diff the live script before copy. Keep any local steps that are not date
selection. The date change is the added `--date` from
`src.engine.collection_target --evening`. Leave the installed evening timer
unit in place when its `ExecStart` is already that script. Magellan
session-file ownership is a separate fix.
