#!/usr/bin/env bash
# update-agent.sh — Lightweight upgrade of an already-installed hermes-orch-agent.
#
# Unlike install-agent.sh, this does NOT:
#   - create a venv
#   - register the agent
#   - re-detect paths
#
# It ONLY:
#   1. Overwrite /etc/systemd/system/hermes-orch-agent.service (from local file)
#   2. Force-reinstall the wheel into the existing venv
#   3. Restart the daemon
#
# Use this after editing source code on Windows + rebuilding the wheel +
# dropping the new wheel + service file into this script's dir.
#
# Usage:
#   ./update-agent.sh                       # uses ./hermes_orchestrator-*.whl + ./hermes-orch-agent.service
#   ./update-agent.sh -w wheel.whl          # custom wheel path
#   ./update-agent.sh -u unit.service       # custom unit file path
#   ./update-agent.sh -h                    # help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHEEL_FILE=""
UNIT_FILE=""

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

  -w, --wheel PATH    Path to hermes_orchestrator-*.whl (default: ./hermes_orchestrator-*.whl)
  -u, --unit PATH     Path to hermes-orch-agent.service (default: ./hermes-orch-agent.service)
  -h, --help          Show this help
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -w|--wheel)  WHEEL_FILE="$2"; shift 2 ;;
        -u|--unit)   UNIT_FILE="$2"; shift 2 ;;
        -h|--help)   usage ;;
        *)           usage ;;
    esac
done

log() { echo "[update] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

# Auto-detect wheel if not given
if [[ -z "$WHEEL_FILE" ]]; then
    WHEEL_FILE=$(ls "$SCRIPT_DIR"/hermes_orchestrator-*.whl 2>/dev/null | head -1 || true)
    [[ -z "$WHEEL_FILE" ]] && fail "wheel not found. Pass -w or place hermes_orchestrator-*.whl next to this script."
fi
log "Wheel: $WHEEL_FILE"

# Auto-detect unit if not given
if [[ -z "$UNIT_FILE" ]]; then
    UNIT_FILE="$SCRIPT_DIR/hermes-orch-agent.service"
    [[ ! -f "$UNIT_FILE" ]] && fail "unit not found at $UNIT_FILE. Pass -u."
fi
log "Unit:  $UNIT_FILE"

# Detect install dir (the venv)
INSTALL_DIR="${INSTALL_DIR:-$HOME/.hermes-orchestrator}"
VENV_PIP="$INSTALL_DIR/venv/bin/pip"
[[ ! -x "$VENV_PIP" ]] && fail "pip not found at $VENV_PIP. Run install-agent.sh first."
log "Install: $INSTALL_DIR"

# Detect current user
TARGET_USER="${SUDO_USER:-${USER}}"
log "User: $TARGET_USER"

# ---------- upgrade wheel ----------
log "Force-reinstalling wheel"
"$VENV_PIP" install --force-reinstall "$WHEEL_FILE" 2>&1 | tail -5

# ---------- upgrade systemd unit ----------
log "Overwriting systemd unit"
if ! command -v systemctl >/dev/null; then
    log "  WARN: systemctl not found, skipping systemd upgrade"
else
    # Render with current user/home
    UNIT_TMP="/tmp/hermes-orch-agent.service"
    # Find the target user's home (in case running via sudo)
    if [[ "$TARGET_USER" == "root" ]]; then
        TARGET_HOME="/root"
    else
        TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6) || fail "cannot find home for $TARGET_USER"
    fi
    sed -e "s|/home/stanley|$TARGET_HOME|g" \
        -e "s|^User=.*|User=$TARGET_USER|" \
        -e "s|^Group=.*|Group=$TARGET_USER|" \
        "$UNIT_FILE" > "$UNIT_TMP"

    sudo cp "$UNIT_TMP" /etc/systemd/system/hermes-orch-agent.service
    sudo systemctl daemon-reload
    log "  unit overwritten"
fi

# ---------- restart daemon ----------
if command -v systemctl >/dev/null; then
    log "Restarting daemon"
    sudo systemctl restart hermes-orch-agent.service
    sleep 2
    log "Status:"
    sudo systemctl status hermes-orch-agent.service --no-pager | head -8
    log ""
    log "Recent log (last 15 lines):"
    sudo journalctl -u hermes-orch-agent -n 15 --no-pager
else
    log "No systemctl — you'll need to restart the daemon manually"
fi

log ""
log "=== Update complete ==="
log "Wheel:    $WHEEL_FILE"
log "Service:  /etc/systemd/system/hermes-orch-agent.service"
