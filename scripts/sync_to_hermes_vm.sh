#!/usr/bin/env bash
# ==============================================================================
# Helper to sync local repository to hermes-poc-01 and trigger deployment
# ==============================================================================

set -e

PROJECT="streetsmart-hermes-poc"
ZONE="us-east1-b"
INSTANCE="hermes-poc-01"

echo "============================================================"
echo "📤 Syncing code to GCP VM: ${INSTANCE} (${ZONE})..."
echo "============================================================"

# Tar and upload clean repository excluding .venv, venv, and .git
TEMP_ARCHIVE="/tmp/renewal-automation.tar.gz"
echo "📦 Packaging local source..."
COPYFILE_DISABLE=1 tar --exclude='.venv' --exclude='venv' --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='data/renewals.db' --exclude='data/manual_renewals.db' -czf "${TEMP_ARCHIVE}" .

echo "🚀 Uploading to VM..."
gcloud compute scp "${TEMP_ARCHIVE}" "${INSTANCE}:/tmp/renewal-automation.tar.gz" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --tunnel-through-iap

echo "🔧 Extracting files and verifying on VM..."
gcloud compute ssh "${INSTANCE}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --tunnel-through-iap \
  --command="
    sudo mkdir -p /opt/renewal-automation-system && \
    sudo tar -xzf /tmp/renewal-automation.tar.gz -C /opt/renewal-automation-system && \
    sudo chown -R \$USER:\$USER /opt/renewal-automation-system && \
    cd /opt/renewal-automation-system && \
    chmod +x scripts/*.py scripts/*.sh && \
    echo '🔍 Running EZLynx session check on VM...' && \
    PYTHONPATH=. ./venv/bin/python3 scripts/ezlynx_cli.py sessions && \
    echo '🧪 Running unit test suite on VM...' && \
    PYTHONPATH=. ./venv/bin/pytest tests/test_ezlynx_discussions.py tests/test_ezlynx_api_client.py tests/test_voice_*.py
  "

echo "============================================================"
echo "🎉 Deployment to ${INSTANCE} complete and verified!"
echo "============================================================"

