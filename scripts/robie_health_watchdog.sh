#!/usr/bin/env bash
# ==============================================================================
# StreetSmart Insurance - Robie Autonomous Health Watchdog
# Checks disk space, memory, critical systemd services, and EZLynx CDP health.
# Automatically recovers failed services and emits high-severity email alerts.
# ==============================================================================
set -euo pipefail

DISK_THRESHOLD=85
MEM_MIN_MB=1500
HOSTNAME=$(hostname)
ALERT_SCRIPT="/opt/renewal-automation-system/scripts/send_system_alert.py"
PYTHON="/opt/renewal-automation-system/venv/bin/python"

log_alert() {
    local level="$1"
    local subj="$2"
    local msg="$3"
    echo "[$level] $msg"
    logger -t "robie-watchdog" -p "local0.err" "$msg"
    
    # Send email notification for CRITICAL or WARNING
    if [ -f "$ALERT_SCRIPT" ] && [ -x "$PYTHON" ]; then
        $PYTHON "$ALERT_SCRIPT" "$subj" "$msg" >/dev/null 2>&1 || true
    fi
}

# 1. Check Root Disk Space
DISK_USAGE=$(df -h / | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK_USAGE" -ge "$DISK_THRESHOLD" ]; then
    log_alert "CRITICAL" "🚨 [ALERT] Disk Space Warning: ${DISK_USAGE}% on ${HOSTNAME}" "Root disk usage on ${HOSTNAME} is at ${DISK_USAGE}% (Threshold: ${DISK_THRESHOLD}%). Please investigate disk utilization."
else
    echo "✅ Disk usage healthy: ${DISK_USAGE}%"
fi

# 2. Check Available Memory
AVAIL_MEM_MB=$(free -m | awk '/^Mem:/ {print $7}')
if [ "$AVAIL_MEM_MB" -lt "$MEM_MIN_MB" ]; then
    log_alert "WARNING" "⚠️ [ALERT] Low Available Memory on ${HOSTNAME}" "Available memory on ${HOSTNAME} is ${AVAIL_MEM_MB}MB (Threshold: ${MEM_MIN_MB}MB)."
else
    echo "✅ Memory healthy: ${AVAIL_MEM_MB}MB available"
fi

# 3. Check & Auto-Heal Core Services
SERVICES=("robie-ezlynx-browser.service" "hermes-gateway.service")
for svc in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "$svc"; then
        echo "✅ Service '$svc' is active"
    else
        echo "⚠️ Service '$svc' is down! Attempting auto-restart..."
        if ! systemctl restart "$svc"; then
            log_alert "CRITICAL" "🚨 [CRITICAL] Service $svc Failed on ${HOSTNAME}" "Service '$svc' went down on ${HOSTNAME} and automated restart failed!"
        else
            echo "✅ Auto-restarted '$svc' successfully"
        fi
    fi
done

# 4. Check EZLynx Chrome CDP Endpoint (Port 9222)
if curl -s --max-time 3 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    echo "✅ EZLynx Chrome CDP (port 9222) is responsive"
else
    echo "⚠️ EZLynx Chrome CDP port 9222 unresponsive! Restarting robie-ezlynx-browser..."
    systemctl restart robie-ezlynx-browser.service || true
fi

echo "Watchdog sweep completed at $(date)"
