# Test installation with the gateway kept stopped

This is the deliberately smaller alternative to a lease-renewal controller.
It installs the exact archive without starting Test Chat. It does not establish
an outage, drain a running gateway, perform live smoke tests, or authorize QA or
Production promotion. Buster's pilot remains blocked. No lease is acquired,
renewed, expired, or abandoned by this mode.

A running gateway plus a renewable reservation is not sufficient protection
against controller death: an expired reservation can be reconciled and the
backlog reopened. Instead this mode requires the gateway and identified
producers to be **already inactive and persistently masked in /etc/systemd/system**.
PID1 refuses starts even after installer/runner death and after reboot. No
cleanup trap, heartbeat, or surviving SSH session is needed. A privileged
operator can of course deliberately remove a mask; that is outside this hold.

## Prerequisites — separate approved operator action

Use an authorized privileged Test access route. Ralph's non-sudo account cannot
perform these steps; a read-only diagnostic workflow is not a mutation route.
The deployment workflow still requires its separately approved WIF/IAP access
and temporary SSH registration. This change does not grant or create access.

1. Obtain the outage approval and serialize Test operators. Record the actual
   prior active/enabled state of every unit in `scripts/test_stopped_install.py`
   before changing anything. Preserve exact original unit files, symlinks and
   permissions if local unit definitions must be moved to establish masks;
   ordinary `systemctl mask` can refuse a locally defined unit. Never overwrite
   such a definition or remove a preexisting mask.
2. Fence the identified scheduled and external/direct producers. The guarded
   unit list includes the Test gateway (and alternate gateway name), scheduler,
   Test keepalive, email watcher, and cron daemons. Masking cron holds **all Test
   cron tasks**, including PFA checks, and therefore belongs in the approved
   outage. Browser service and global driver are not changed. Coordinate manual
   runners separately: masks do not prevent a shell from invoking Python.
3. Let inflight work finish before stopping the gateway. Do not use a single idle
   snapshot as an intake fence. If the current gateway cannot be drained safely
   using the already verified existing reservation and fenced intake, do not
   proceed. Persistently mask and stop the gateway only through the approved
   outage procedure; check no remaining workers, active/expired unresolved runs,
   queue claims, or reply sends. Preserve the database and all queue records.
4. Write `/opt/streetsmart-hermes-test/deployments/stopped-install-hold.json`
   as a regular root-owned mode-0600 file. It must contain:

   ```json
   {
     "version": 1,
     "host": "hermes-test-01",
     "release_sha256": "<exact archive SHA-256>",
     "external_producers_fenced": true,
     "approved_outage_reference": "<approval/evidence reference>",
     "prior_units": {
       "<every unit from UNITS>": {
         "active": "active",
         "enabled": "enabled",
         "mask_preexisting": false,
         "unit_backup": "<exact preserved unit path or empty if no replacement>"
       }
     }
   }
   ```

   The example is a schema, not a ready-to-run receipt. Populate actual values,
   including `not-found` for absent units. The receipt is an operator attestation
   of external fencing, not independently inferred proof that no direct runner
   exists. The installer independently checks persistent masks, inactive units
   and database idleness; a receipt alone never passes those checks.

## Install and inspect without starting anything

On protected main, use existing `deploy-test.yml`, operation `install-stopped`,
confirmation `INSTALL_TEST_KEPT_STOPPED`. Its installer command is the existing
exact-archive path plus `--skip-policy-setup --keep-stopped`.

The archive digest, candidate verification, dependency-import smoke checks,
official overlay installation and rollback paths remain in use. Before these
operations, the stopped guard saves unit states, the exact hold receipt, and
hashes of existing durable rows to
`releases/<short>/stopped-install-before.json`. This file is exclusive: a retry
cannot overwrite earlier evidence. After installation it checks those rows
again. Official installation may add its own proof job/checkpoints; existing
rows must not change. Unknown/missing required schemas and any active run,
including an expired reservation, fail closed. No job repair is attempted.

The result is `TEST INSTALLED STOPPED`, never `TEST VERIFIED`. The stopped
package is retained for 30 days as `stopped-test-release-<commit>`. It is not
accepted as an installed/live-certified package by the existing promotion
validator. Do not rebuild it for later QA. No live-runtime or Chat-reply smoke
check can be claimed while the gateway is stopped.

If installation fails, ordinary rollback paths restore pointers/configuration
where already supported but do not restart the gateway. If the installer is
killed between operations, pointers/overlays may be partially changed: masks
remain, and an authorized operator must inspect/recover before resume. Never
remove masks merely to make the deployment job green. Do not rerun against an
existing snapshot without reviewing and archiving that attempt's evidence.

## Explicit recovery/resume — not automatic

There is intentionally no workflow input that releases this hold.

1. Read the saved snapshot and hold receipt. Verify the exact current pointers,
   archive digest and durable-row preservation using the saved guard snapshot.
   For a failed/partial install, use the existing reviewed rollback helper with
   its ninth argument `false` (do not restart), and restore the saved runtime
   drop-in. Inspect overlay consistency before any start.
2. Obtain explicit approval for the exact release to resume and for the backlog
   that would execute. Starting the gateway can immediately consume the existing
   note and other queued work. **Do not resume the candidate just to run Buster.**
   A selected-job isolation control is not included here.
3. Through authorized privileged access, restore only masks/unit definitions
   changed for this outage, from the recorded prior states/backups. Keep
   preexisting masks; never enable a previously disabled timer. Restore the
   gateway only if its prior state and the approved plan call for it; keep other
   producers stopped until gateway health and queue behavior are verified.
4. Run the existing official `prove` step against the same installed archive,
   then independent runtime QA. Restore only previously active timers/services
   when explicitly authorized. Record actual restoration and remaining holds.
   The current certification workflow remains blocked until normal live-install
   evidence and independent QA have been assembled for those exact bytes.

This fallback solves the installer's automatic restart, not the separate
privileged-access or safe initial-drain problem. Until those prerequisites are
met, do not dispatch it. It never changes Production, driver metadata, browser
login, or policy setup.
