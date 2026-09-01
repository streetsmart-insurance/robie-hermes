#!/usr/bin/env bash
# Non-destructive restore drill: snapshot -> new disk -> mount on hermes-test-01 -> verify -> cleanup.
set -euo pipefail

PROJECT="${ROBIE_DR_PROJECT:-streetsmart-hermes-poc}"
ZONE="${ROBIE_DR_ZONE:-us-east1-b}"
TEST_VM="${ROBIE_DR_TEST_VM:-hermes-test-01}"
SNAPSHOT="${1:?usage: $0 <snapshot-name>}"
STAMP=$(date -u +%Y%m%d)
DRILL_DISK="hermes-poc-dr-drill-${STAMP}"
MOUNT=/mnt/hermes-poc-dr-drill

cleanup() {
  gcloud compute instances detach-disk "${TEST_VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --disk="${DRILL_DISK}" >/dev/null 2>&1 || true
  gcloud compute disks delete "${DRILL_DISK}" \
    --project="${PROJECT}" --zone="${ZONE}" --quiet >/dev/null 2>&1 || true
}
trap cleanup EXIT

gcloud compute disks create "${DRILL_DISK}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --source-snapshot="${SNAPSHOT}"

gcloud compute instances attach-disk "${TEST_VM}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --disk="${DRILL_DISK}" \
  --mode=rw

gcloud compute ssh "${TEST_VM}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --tunnel-through-iap \
  --command="
set -euo pipefail
DEV=\$(lsblk -ndo NAME,TYPE | awk '\$2==\"disk\" && \$1!=\"sda\" {print \$1; exit}')
test -n \"\$DEV\"
PART=\"\${DEV}1\"
sudo mkdir -p ${MOUNT}
sudo mount /dev/\${PART} ${MOUNT}
test -d ${MOUNT}/opt/streetsmart-hermes
ls -ld ${MOUNT}/opt/streetsmart-hermes
echo RESTORE_DRILL_OK snapshot=${SNAPSHOT} disk=${DRILL_DISK}
sudo umount ${MOUNT}
"
