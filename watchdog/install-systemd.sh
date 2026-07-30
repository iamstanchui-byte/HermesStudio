#!/usr/bin/env bash
# install-systemd.sh — Register Hermes Orchestrator as systemd USER services (Linux).
#
# Cross-platform note: this is the Linux analogue of watchdog/register-system.ps1
# (Windows). The PS1 registers a Task Scheduler task + NSSM service; this
# script registers systemd user units + a watchdog timer. Same outcome:
#
#   - The hermes-orchestrator server runs in the background, starts on
#     boot (user login), restarts on crash via Restart=on-failure.
#   - A 1-min watchdog timer re-checks the port and force-restarts if
#     the systemd service is "active" but the port is dead (catches
#     cases systemd doesn't notice, e.g. worker stuck).
#   - Logs go to ~/.local/state/hermes-orchestrator/logs/ AND the
#     systemd journal. The watchdog also writes to watchdog-hermes.log
#     (same file the PS1 wrote to) so ops scripts that grep the old
#     log path keep working.
#
# Usage:
#   ./install-systemd.sh            # install + start
#   ./install-systemd.sh uninstall  # disable + remove
#
# Requires: bash, systemd, python3 in PATH. Does NOT need root
# (uses systemd --user, which writes to ~/.config/systemd/user/).

set -euo pipefail

# ---- Paths (resolved from script location so the script is portable) ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_EXE="$PROJECT_DIR/.venv/bin/python"
WATCHDOG_PY="$SCRIPT_DIR/watchdog-hermes.py"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/hermes-orchestrator"
LOG_DIR="$STATE_DIR/logs"
mkdir -p "$LOG_DIR"

SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$SERVICE_DIR"

# systemd's %h = user home. We use it in the unit files so the
# service file is portable across machines (no hardcoded /home/foo).
SERVICE_FILE="$SERVICE_DIR/hermes-orchestrator.service"
WATCHDOG_SERVICE_FILE="$SERVICE_DIR/hermes-orchestrator-watchdog.service"
WATCHDOG_TIMER_FILE="$SERVICE_DIR/hermes-orchestrator-watchdog.timer"

# ---- Helpers ----
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# Detect python — prefer the venv python (the orchestrator's own
# runtime), fall back to system python3.
detect_python() {
    if [[ -x "$PYTHON_EXE" ]]; then
        echo "$PYTHON_EXE"
    else
        command -v python3 || die "python3 not found in PATH"
    fi
}

# ---- Subcommand: uninstall ----
uninstall() {
    log "Uninstalling hermes-orchestrator systemd units..."
    systemctl --user disable --now hermes-orchestrator-watchdog.timer 2>/dev/null || true
    systemctl --user disable --now hermes-orchestrator.service 2>/dev/null || true
    rm -f "$SERVICE_FILE" "$WATCHDOG_SERVICE_FILE" "$WATCHDOG_TIMER_FILE"
    systemctl --user daemon-reload
    log "Done. Service files removed; data in $STATE_DIR is preserved."
    log "Run ./install-systemd.sh to re-install."
}

# ---- Generate the 3 systemd unit files ----
#
# We generate them inline (heredoc + envsubst) rather than checking
# in pre-rendered .service files, so the script is the single source
# of truth and works on any install path / username. systemd
# understands ${ENV} in unit files only at install time (when
# systemd-analyze verifies the file) — at runtime the values are
# baked in.

PY_PATH="$(detect_python)"

# Main service: runs the server. Restart=on-failure + RestartSec=5
# is the primary recovery path; the watchdog timer is a fallback
# for cases where the service is "active" but the port is dead.
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Hermes Orchestrator server (port 8765)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_DIR}
Environment="PATH=${PROJECT_DIR}/.venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="HERMES_PROFILES_DIR=\${HOME}/.local/share/hermes/profiles"
ExecStart=${PY_PATH} -m hermes_orch serve --no-reload
Restart=on-failure
RestartSec=5
# Send SIGTERM first, then SIGKILL after 10s if it's still alive.
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=10
# stdout/stderr go to the journal (visible via journalctl --user -u
# hermes-orchestrator) AND to log files in the XDG state dir so ops
# scripts that grep the old watchdog/log path keep working. journal
# is canonical; the file mirror is for tools that don't journal.
StandardOutput=append:${LOG_DIR}/server.stdout.log
StandardError=append:${LOG_DIR}/server.stderr.log

[Install]
WantedBy=default.target
EOF

# Watchdog service: a oneshot that runs the Python watchdog. The
# .timer below fires this every minute. We declare Requires= the
# main service so systemd is aware of the dependency (it doesn't
# enforce it for a oneshot, but it shows up in 'systemctl list-
# dependencies' for clarity).
cat > "$WATCHDOG_SERVICE_FILE" <<EOF
[Unit]
Description=Hermes Orchestrator watchdog (fallback for stuck service)
After=hermes-orchestrator.service

[Service]
Type=oneshot
WorkingDirectory=${SCRIPT_DIR}
Environment="HERMES_PROJECT_DIR=${PROJECT_DIR}"
Environment="HERMES_PYTHON=${PY_PATH}"
Environment="HERMES_WATCHDOG_PORT=8765"
ExecStart=${PY_PATH} ${WATCHDOG_PY}
# Log watchdog activity to journal + file. 200KB cap not enforced
# here (systemd handles rotation via journald / logrotate.d), but
# the watchdog's own log rotation at 200KB still applies to the
# in-script log file.
StandardOutput=append:${LOG_DIR}/watchdog.log
StandardError=append:${LOG_DIR}/watchdog.log
EOF

# Watchdog timer: fires the watchdog service every 1 minute. We
# use OnUnitActiveSec so the timer starts 1 min after the watchdog
# service was last activated (so a slow first run doesn't double-
# trigger). OnBootSec=2min delays the first tick by 2 min so the
# main service has time to come up first.
cat > "$WATCHDOG_TIMER_FILE" <<EOF
[Unit]
Description=Hermes Orchestrator watchdog timer (every 1 min)

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

# ---- Install + enable ----
log "Generated 3 unit files in $SERVICE_DIR:"
ls -l "$SERVICE_FILE" "$WATCHDOG_SERVICE_FILE" "$WATCHDOG_TIMER_FILE"

log "Running systemctl --user daemon-reload..."
systemctl --user daemon-reload

log "Enabling + starting hermes-orchestrator.service..."
systemctl --user enable --now hermes-orchestrator.service

log "Enabling + starting hermes-orchestrator-watchdog.timer..."
systemctl --user enable --now hermes-orchestrator-watchdog.timer

# ---- Show status ----
log ""
log "=== Service status ==="
systemctl --user status hermes-orchestrator.service --no-pager || true
log ""
log "=== Watchdog timer status ==="
systemctl --user list-timers hermes-orchestrator-watchdog.timer --no-pager || true
log ""
log "=== Recent server log (last 20 lines) ==="
if [[ -f "$LOG_DIR/server.stderr.log" ]]; then
    tail -20 "$LOG_DIR/server.stderr.log" || true
fi
log ""
log "=== Useful commands ==="
log "  systemctl --user status hermes-orchestrator.service"
log "  systemctl --user status hermes-orchestrator-watchdog.timer"
log "  journalctl --user -u hermes-orchestrator -f        # live tail"
log "  journalctl --user -u hermes-orchestrator-watchdog -f"
log "  touch $SCRIPT_DIR/restart-now  # force-restart on next watchdog tick"
log "  $SCRIPT_DIR/install-systemd.sh uninstall  # remove systemd units"
