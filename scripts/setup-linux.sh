#!/usr/bin/env bash
# Linux setup helper.
# Per REVIEW.md §8.1: register agent as systemd service.

set -euo pipefail

ORCHESTRATOR_URL="${1:-}"
AGENT_ID="${2:-}"
ROLES="${3:-}"

if [[ -z "$ORCHESTRATOR_URL" || -z "$AGENT_ID" || -z "$ROLES" ]]; then
    echo "Usage: $0 <orchestrator-url> <agent-id> <roles>"
    echo "Example: $0 http://192.168.1.10:8765 linux-a-01 data-analyst,backtest-runner,report-writer,mt5-automation"
    exit 1
fi

echo "=== Hermes Orchestrator Agent — Linux Setup ==="
echo "Orchestrator: $ORCHESTRATOR_URL"
echo "Agent ID:     $AGENT_ID"
echo "Roles:        $ROLES"
echo ""

# 1. Install
echo "[1/4] Installing hermes-orchestrator..."
pip3 install --user hermes-orchestrator

export PATH="$HOME/.local/bin:$PATH"

# 2. Register
echo ""
echo "[2/4] Registering agent..."
hermes-orch-agent register \
    --orchestrator "$ORCHESTRATOR_URL" \
    --agent-id "$AGENT_ID" \
    --roles "$ROLES"

# 3. Install systemd unit
echo ""
echo "[3/4] Installing systemd service..."

SERVICE_FILE="$HOME/.config/systemd/user/hermes-orch-agent.service"
mkdir -p "$(dirname "$SERVICE_FILE")"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Hermes Orchestrator Agent ($AGENT_ID)
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/hermes-orch-agent start
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "hermes-orch-agent.service"
systemctl --user start "hermes-orch-agent.service"

# 4. Lingering so service runs even when user logs out
echo ""
echo "[4/4] Enabling lingering (service runs after logout)..."
loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" || true

echo ""
echo "✅ Setup complete."
echo "   Status:  systemctl --user status hermes-orch-agent"
echo "   Logs:    journalctl --user -u hermes-orch-agent -f"
