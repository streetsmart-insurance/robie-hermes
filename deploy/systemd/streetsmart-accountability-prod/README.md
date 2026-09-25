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
