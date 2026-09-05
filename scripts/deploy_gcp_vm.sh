#!/usr/bin/env bash
# ==============================================================================
# StreetSmart Insurance - Autonomous Renewal Automation System
# Production VM Deployment Script (Google Cloud Platform)
#
# Target Host: hermes-poc-01 (us-east1-b / streetsmart-hermes-poc)
# Target User: robie@streetsmart.insurance
# ==============================================================================

set -e

INSTALL_DIR="/opt/renewal-automation-system"
DATA_DIR="${INSTALL_DIR}/data"

echo "============================================================"
echo "🚀 Initializing Renewal Automation Engine Deployment..."
echo "============================================================"

# 1. Update Packages & Install Prerequisites
echo "📦 Verifying system packages (Docker, Git, Curl)..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    curl \
    git \
    ca-certificates \
    gnupg \
    lsb-release

# 2. Ensure Docker & Compose Plugin are installed
if ! command -v docker &> /dev/null; then
    echo "🐳 Installing Docker Engine..."
    sudo mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    sudo systemctl enable --now docker
else
    echo "✅ Docker is already installed."
fi

# 3. Create Application & Data Directories
echo "📂 Setting up installation directories at ${INSTALL_DIR}..."
sudo mkdir -p "${INSTALL_DIR}"
sudo chown -R $USER:$USER "${INSTALL_DIR}"

mkdir -p "${DATA_DIR}/downloads"
mkdir -p "${DATA_DIR}/input_reports"
mkdir -p "${DATA_DIR}/credentials"
mkdir -p "${DATA_DIR}/screenshots"

# 4. Copy / Link Repository
if [ ! -f "${INSTALL_DIR}/docker-compose.yml" ]; then
    echo "📥 Syncing application source code..."
    # If running from within repo directory, copy over
    if [ -f "./docker-compose.yml" ]; then
        cp -r ./* "${INSTALL_DIR}/"
    else
        echo "⚠️ Run this script from the repository root, or clone the repo into ${INSTALL_DIR}"
        exit 1
    fi
fi

cd "${INSTALL_DIR}"

# 5. Setup Environment File (.env) if missing
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    echo "⚙️ Creating default .env configuration..."
    cp .env.example .env
    sed -i 's/EZLYNX_USERNAME=.*/EZLYNX_USERNAME=robie@streetsmart.insurance/' .env
fi

# 6. Build and Start the Docker Container
echo "🏗️ Building and launching Docker container (Playwright Jammy)..."
docker compose down --remove-orphans || true
docker compose build
docker compose up -d

echo "============================================================"
echo "✅ Renewal Automation Engine is UP and RUNNING!"
echo "============================================================"
echo "Status check:"
docker compose ps
echo ""
echo "Next step: Run automated EZLynx login verification:"
echo "docker compose exec renewal-engine python -c \"import asyncio; from src.ezlynx.session_manager import EZLynxSessionManager; mgr = EZLynxSessionManager(); print(mgr.get_credentials())\""
