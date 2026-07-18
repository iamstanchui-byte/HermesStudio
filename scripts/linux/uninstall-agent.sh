#!/usr/bin/env bash
# uninstall-agent.sh — Remove hermes-orch-agent from a Linux box.
#
# Usage:
#   ./uninstall-agent.sh                     # remove venv, dirs, systemd unit (if root)
#   ./uninstall-agent.sh -u USER             # uninstall for specific user
#   ./uninstall-agent.sh -k                 # keep install dir (just remove service)
#   ./uninstall-agent.sh -h                 # help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-${USER}}"
KEEP_DIR=0

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]
  -u USER   User whose install to remove (default: $USER)
  -k        Keep install dir (~/.hermes-orchestrator)
  -h        Show this help
EOF
    exit 1
}

while getopts "u:kh" opt; do
    case $opt in
        u) TARGET_USER="$OPTARG" ;;
        k) KEEP_DIR=1 ;;
        h) usage ;;
        *) usage ;;
    esac
done

# Resolve target home
if [[ "$TARGET_USER" == "root" ]]; then
    TARGET_HOME="/root"
else
    TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6) || {
        echo "user $TARGET_USER not found" >&2
        exit 1
    }
fi

INSTALL_DIR="$TARGET_HOME/.hermes-orchestrator"
echo "[uninstall] target: user=$TARGET_USER home=$TARGET_HOME dir=$INSTALL_DIR"

# Stop + disable service
if command -v systemctl >/dev/null; then
    if systemctl list-unit-files | grep -q hermes-orch-agent.service; then
        echo "[uninstall] stopping + disabling service (sudo)"
        sudo systemctl stop hermes-orch-agent.service 2>/dev/null || true
        sudo systemctl disable hermes-orch-agent.service 2>/dev/null || true
        sudo rm -f /etc/systemd/system/hermes-orch-agent.service
        sudo systemctl daemon-reload
    fi
fi

# Remove pip package
if [[ -x "$INSTALL_DIR/venv/bin/pip" ]]; then
    echo "[uninstall] pip uninstall hermes-orchestrator"
    sudo -u "$TARGET_USER" "$INSTALL_DIR/venv/bin/pip" uninstall -y hermes-orchestrator 2>&1 | tail -3 || true
fi

# Remove install dir
if [[ "$KEEP_DIR" == "0" && -d "$INSTALL_DIR" ]]; then
    echo "[uninstall] removing $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
else
    echo "[uninstall] kept $INSTALL_DIR (per -k or not found)"
fi

echo ""
echo "=== Uninstall complete ==="
echo "Note: the orchestrator still has this agent registered."
echo "Remove via dashboard or: curl -X DELETE http://<orchestrator>/api/agents/<agent_id>"
