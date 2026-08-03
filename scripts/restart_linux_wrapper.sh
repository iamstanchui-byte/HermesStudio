#!/bin/bash
# Restart the linux-a-01 wrapper daemon.
#
# Why this exists: when agent_cli.py is updated (e.g. via scp), the wrapper's
# self-restart watchdog (in start()) detects the mtime change and exits
# expecting a service manager to restart it. We use nohup, NOT systemd, so
# nothing else restarts it. This script does the manual restart.
#
# Usage:  bash scripts/restart_linux_wrapper.sh
# Or:     ssh stanley@192.168.2.161 'bash -s' < scripts/restart_linux_wrapper.sh
#
# Steps:
#   1. Kill any existing wrapper process (if alive)
#   2. Clear __pycache__ (stale .pyc was a v1.9.1 issue)
#   3. nohup-launch a new wrapper in the background
#   4. Verify heartbeat is reaching the server within 10s

set -e

ORCH_USER="${ORCH_USER:-stanley}"
ORCH_HOST="${ORCH_HOST:-192.168.2.161}"
ORCH_URL="${ORCH_URL:-http://192.168.2.152:8765}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="/home/stanley/.hermes-orchestrator"
CONFIG="$REMOTE_DIR/wrapper-config.json"
LOG="$REMOTE_DIR/agent.log"
PID_FILE="$REMOTE_DIR/agent.pid"

ssh -i "$SSH_KEY" "$ORCH_USER@$ORCH_HOST" bash -s <<EOF
set -e
echo "[1/4] Killing any existing wrapper process..."
if [ -f "$PID_FILE" ]; then
    OLD_PID=\$(cat "$PID_FILE")
    if kill -0 "\$OLD_PID" 2>/dev/null; then
        echo "    killing PID \$OLD_PID"
        kill "\$OLD_PID"
        # Give it 3s to die gracefully
        for i in 1 2 3; do
            if kill -0 "\$OLD_PID" 2>/dev/null; then sleep 1; else break; fi
        done
        # Force if still alive
        if kill -0 "\$OLD_PID" 2>/dev/null; then
            echo "    PID \$OLD_PID still alive, force killing"
            kill -9 "\$OLD_PID" 2>/dev/null || true
        fi
    else
        echo "    stale PID file (\$OLD_PID) — not alive"
    fi
    rm -f "$PID_FILE"
fi
# Also kill any orphan hermes-orch-agent processes
pkill -f "hermes_orch.agent_cli start" 2>/dev/null || true
sleep 1

echo "[2/4] Clearing __pycache__ (stale .pyc prevention)..."
find "$REMOTE_DIR/venv/lib/python3.12/site-packages/hermes_orch/__pycache__" \
    -name "agent_cli*.pyc" -delete 2>/dev/null || true

echo "[3/4] Launching wrapper in background (nohup)..."
cd "$REMOTE_DIR"
# v3.12.0: cert pin so the wrapper trusts the orchestrator's self-signed
# cert when the dashboard is served over HTTPS. Same file the user
# generated via `hermes-orch gen-cert` on the orchestrator host and
# scp'd to the agent host. Precedence (set in agent_http.py):
#   ORCHESTRATOR_CA_BUNDLE  >  INSECURE_SKIP_TLS_VERIFY  >  default verify=True
# Pin is the production-friendly choice — accepts ONLY the orchestrator's
# cert, not any cert signed by a system-trusted CA (defense vs MITM
# via a compromised root store).
export ORCHESTRATOR_CA_BUNDLE="$REMOTE_DIR/certs/server.crt"
nohup "$REMOTE_DIR/venv/bin/python" -m hermes_orch.agent_cli start \\
    --config "$CONFIG" --interval 5 \\
    >> "$LOG" 2>&1 &
NEW_PID=\$!
echo "\$NEW_PID" > "$PID_FILE"
echo "    started PID \$NEW_PID (logged to $LOG)"
echo "    ORCHESTRATOR_CA_BUNDLE=$ORCHESTRATOR_CA_BUNDLE"

echo "[4/4] Verifying heartbeat (waiting up to 15s)..."
HEALTHY=0
for i in \$(seq 1 15); do
    sleep 1
    if [ -s "$LOG" ] && tail -50 "$LOG" | grep -q "background heartbeat thread started"; then
        HEALTHY=1
        echo "    wrapper is heartbeating (after \${i}s)"
        break
    fi
done
if [ "\$HEALTHY" -eq 0 ]; then
    echo "    WARNING: wrapper did not start cleanly within 15s. Check $LOG:"
    tail -30 "$LOG"
    exit 1
fi

echo
echo "OK. Last 10 log lines:"
tail -10 "$LOG"
EOF
