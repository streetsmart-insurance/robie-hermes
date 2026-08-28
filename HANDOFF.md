# Hermes handoff — Friday 28 Aug 2026 ~12:02pm ET

Start here if you are ChatGPT, Claude, Cursor, Grok, or Jake pasting into any of them. Finish the Test Ascend punch-list. Do not start a live PAWIVA / real-client Production job.

Tap-in card: [CLOUD_DESK.md](CLOUD_DESK.md)

Speak technical English only: GitHub, Test (`hermes-test-01`), Production (`hermes-poc-01`), `hermes-gateway`, Playwright, CDP, Job Engine.

Carlo does not SSH. His clicks stay Approve, money, bind, rare login. Jake Ferrara (`StreetSmartJake`) Approves PRs. Carlo said 2026-08-28: skip his Confirm and squash-merge as PRs land.

## What “live” means

1. GitHub `main` SHA matches the zip
2. Both pointers match that 12-char release
3. `hermes-gateway` `ActiveEnterTimestamp` is after the pointer flip
4. For a Chat job: a `checkpoints.kind=gateway_progress` row in `jobs.db`

Pointer-only is not live.

## Proven state when this was written

GitHub `main`: `b8f7689fabd882ef82b711d30f6d8f5d7a4f10bd` (PR 45). Merged today: 39–45. Stay out of leftover WIF drafts PR 2 and PR 16.

Production `hermes-poc-01` (~11:09am ET): pointers `8734440b57cc` (PR 43 action gate). tgz sha256 `8c5637d5d08444e9232ecb1c3aa0c15c11aaf4ba6e7bd46d1239eff0c125aa70`. Gateway `ActiveEnterTimestamp` 2026-08-28 15:09:45 UTC. Do not zip 44/45 to Production yet. Do not `@robie` a live PAWIVA to prove the gate.

Test `hermes-test-01` (last proven ~11:31am ET): pointers `6b12547739ec` (PR 44). Dusty started a Test zip of PR 45 (`b8f7689fabd8`) around 11:56am ET. Hostname first. If pointers are still `6b12547739ec`, finish that zip before the next audit.

## Carlo’s rules

1. Ascend login is Robie (`robie@streetsmart.insurance` + email 2SV). Never paste the code.
2. Producer/AM = PFA sender unique Name+email. Carlo → `Carlo Ferrara carlo@streetsmart.insurance`. Never ssinj, never Robie AI.
3. Carrier, State, Coverage type come from the imported Test quote. Do not invent names from the dropdown.
4. Production is never the first test. PR 43 refuses `ascend.create_program` on Production until a recorded Test punch-list PASS exists.
5. Never PAWIVA / `221398001` for the Test simulator.
6. Unique locator only. No `.first` / `.nth` / `.last`. Stop before Save / email / checkout / payment / bind.

## Finish this

1. Prove PR 45 on Test (`b8f7689fabd8`).
2. Re-run `ascend_locator_audit --live` with `--requested-by carlo@streetsmart.insurance` and `--quote` a Test-account file. CI fixture text (only if no real ROBIE Test LLC quote exists):

```
ROBIE Test LLC
Test account quote
Carrier: Philadelphia Indemnity Insurance Company
Coverage type: Commercial Package
State: Georgia
Quote number: TEST-ASCEND-AUDIT
```

3. A clean pass logs spinner seconds, unique New program, uploaded Import document, Carlo streetsmart Producer/AM, quote-matched Carrier/State/Coverage, Agency Fee default+$500, full job-id artifact folder, stop before Save.
4. Only then record the Test PASS and consider a Production zip of current main.

SSH from any browser: https://shell.cloud.google.com/?show=terminal&project=streetsmart-hermes-poc

```
gcloud compute ssh hermes-test-01 --zone=us-east1-b --tunnel-through-iap --ssh-key-file=$HOME/.ssh/hermes-nopass
```

Do not RETRY: `09d69760` `40ccc0d6` `da53765b` `6cf6f6ae` `468d1575` `de9c530a` `7f297e56` `66266d62` `c31f9c69` `8a11d28c` `6662f894` `eb96f620` `ffbfa109` `640834a4` `807f8920` `38c0fa79`.
