#!/usr/bin/env bash
# ==============================================================================
# Robie Automated Daily Backup Script
# Performs safe online transactional SQLite backups, captures .env and cookies,
# and enforces a rolling 14-day archive retention.
# ==============================================================================
set -euo pipefail

BACKUP_ROOT="/var/backups/robie"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_ARCHIVE="${BACKUP_ROOT}/robie_backup_${TIMESTAMP}.tar.gz"
STAGE_DIR=$(mktemp -d /tmp/robie_backup_stage_XXXXXX)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

cleanup() {
    if [ -d "${STAGE_DIR}" ]; then
        rm -rf "${STAGE_DIR}"
    fi
}
trap cleanup EXIT

log "Starting Robie automated backup..."

# Ensure destination and staging directories
mkdir -p "${BACKUP_ROOT}"
mkdir -p "${STAGE_DIR}/databases"
mkdir -p "${STAGE_DIR}/config"
mkdir -p "${STAGE_DIR}/browser_profiles"
mkdir -p "${STAGE_DIR}/systemd"

# 1. Safe Transactional SQLite Backups
log "Backing up SQLite databases via online .backup API..."
DB_COUNT=0
for db_pattern in "/opt/renewal-automation-system/data/*.db" "/opt/renewal-automation-system/data/*.sqlite" "/opt/streetsmart-hermes/data/*.db"; do
    for db in $db_pattern; do
        if [ -f "$db" ] && [[ ! "$(basename "$db")" =~ ^\._ ]]; then
            db_name=$(basename "$db")
            sqlite3 "$db" ".backup '${STAGE_DIR}/databases/${db_name}'"
            DB_COUNT=$((DB_COUNT + 1))
        fi
    done
done
log "Backed up ${DB_COUNT} database(s)."

# 2. Back up Environment Configurations (sensitive - permissions 600)
log "Backing up configuration files..."
if [ -f "/opt/renewal-automation-system/.env" ]; then
    cp "/opt/renewal-automation-system/.env" "${STAGE_DIR}/config/renewal-automation-system.env"
fi
if [ -f "/opt/streetsmart-hermes/.env" ]; then
    cp "/opt/streetsmart-hermes/.env" "${STAGE_DIR}/config/streetsmart-hermes.env"
fi

# 3. Back up Carrier & EZLynx Session Tokens / Cookies
log "Backing up persistent session credentials..."
if [ -d "/home/carlo_streetsmart_insurance/.robie_carrier_profiles" ]; then
    cp -r "/home/carlo_streetsmart_insurance/.robie_carrier_profiles" "${STAGE_DIR}/browser_profiles/"
elif [ -d "/root/.robie_carrier_profiles" ]; then
    cp -r "/root/.robie_carrier_profiles" "${STAGE_DIR}/browser_profiles/"
fi

EZLYNX_COOKIE_FILE="/opt/streetsmart-hermes/.hermes/browser-profiles/ezlynx/Default/Network/Cookies"
if [ -f "$EZLYNX_COOKIE_FILE" ]; then
    cp "$EZLYNX_COOKIE_FILE" "${STAGE_DIR}/browser_profiles/ezlynx_network_cookies"
fi

# 4. Back up Systemd Unit Files
log "Backing up Robie systemd unit definitions..."
for unit in /etc/systemd/system/robie* /etc/systemd/system/hermes*; do
    if [ -f "$unit" ]; then
        cp "$unit" "${STAGE_DIR}/systemd/"
    fi
done

# 5. Compress into tar.gz Archive
log "Compressing backup archive to ${BACKUP_ARCHIVE}..."
tar -czf "${BACKUP_ARCHIVE}" -C "${STAGE_DIR}" .
chmod 600 "${BACKUP_ARCHIVE}"

# Verify archive integrity
tar -tzf "${BACKUP_ARCHIVE}" > /dev/null
ARCHIVE_SIZE=$(du -h "${BACKUP_ARCHIVE}" | cut -f1)
log "Archive successfully created and verified (${ARCHIVE_SIZE})."

# 6. Retention: Prune Archives Older Than 14 Days
log "Pruning archives older than 14 days..."
PRUNED_COUNT=0
while IFS= read -r old_file; do
    if [ -n "$old_file" ]; then
        rm -f "$old_file"
        PRUNED_COUNT=$((PRUNED_COUNT + 1))
    fi
done < <(find "${BACKUP_ROOT}" -name "robie_backup_*.tar.gz" -type f -mtime +14)

log "Pruned ${PRUNED_COUNT} old backup archive(s)."
log "Backup complete! Current backups in ${BACKUP_ROOT}:"
ls -lh "${BACKUP_ROOT}"
