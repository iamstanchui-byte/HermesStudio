#!/usr/bin/env bash
# install-agent.sh — Install hermes-orch-agent (wrapper daemon) on a Linux box.
#
# Usage:
#   ./install-agent.sh                       # interactive (asks for orchestrator URL, agent_id, user)
#   ./install-agent.sh -o URL -a ID -u USER  # non-interactive
#   ./install-agent.sh -h                    # help
#
# What it does:
#   1. Verify Python 3.10+ is available
#   2. Create venv at ~/.hermes-orchestrator/venv
#   3. Install hermes-orchestrator wheel + requirements from same dir
#   4. Run `hermes-orch-agent register` to register this host with the orchestrator
#   5. (sudo) Install systemd unit + enable + start
#
# Requires:
#   - Python 3.10+
#   - pip
#   - sudo (only for systemd install; can be skipped with --no-systemd)

set -euo pipefail

# ---------- args ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCHESTRATOR_URL=""
AGENT_ID=""
TARGET_USER="${SUDO_USER:-${USER}}"
INSTALL_SYSTEMD=1
WHEEL_FILE=""
REQUIREMENTS_FILE=""

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

  -o, --orchestrator URL    Orchestrator URL (e.g. http://192.168.1.10:8765)
  -a, --agent-id ID         Agent ID for this host (e.g. linux-a-01)
  -u, --user USER           User to run the daemon as (default: $USER)
  -s, --skip-systemd        Skip systemd install (use this if you don't have sudo)
  -w, --wheel PATH          Path to hermes_orchestrator-*.whl (default: auto-detect)
  -r, --requirements PATH   Path to requirements-agent.txt (default: auto-detect)
  -h, --help                Show this help
EOF
    exit 1
}

# Use getopt for long-option support. Some minimal systems only have
# getopts (no long opts); fall back to getopts if getopt unavailable.
if command -v getopt >/dev/null 2>&1; then
    PARSED=$(getopt -o o:a:u:sw:r:h --long orchestrator:,agent-id:,user:,skip-systemd,wheel:,requirements:,help -n "$0" -- "$@" 2>/dev/null) || usage
    eval set -- "$PARSED"
    while true; do
        case "$1" in
            -o|--orchestrator) ORCHESTRATOR_URL="$2"; shift 2 ;;
            -a|--agent-id)     AGENT_ID="$2"; shift 2 ;;
            -u|--user)         TARGET_USER="$2"; shift 2 ;;
            -s|--skip-systemd) INSTALL_SYSTEMD=0; shift ;;
            -w|--wheel)        WHEEL_FILE="$2"; shift 2 ;;
            -r|--requirements) REQUIREMENTS_FILE="$2"; shift 2 ;;
            -h|--help)         usage ;;
            --)                shift; break ;;
            *)                 usage ;;
        esac
    done
else
    # Fallback: getopts (only short options)
    while getopts "o:a:u:sw:r:h" opt; do
        case $opt in
            o) ORCHESTRATOR_URL="$OPTARG" ;;
            a) AGENT_ID="$OPTARG" ;;
            u) TARGET_USER="$OPTARG" ;;
            s) INSTALL_SYSTEMD=0 ;;
            w) WHEEL_FILE="$OPTARG" ;;
            r) REQUIREMENTS_FILE="$OPTARG" ;;
            h) usage ;;
            *) usage ;;
        esac
    done
fi

# ---------- helpers ----------
log() { echo "[install] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

# ---------- preflight ----------
log "Preflight checks"

command -v python3 >/dev/null || fail "python3 not found in PATH"
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log "  Python version: $PY_VERSION"
# Check version via a single python call that returns 0 or 1
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" "$PY_VERSION" 2>/dev/null; then
    # Fallback: parse PY_VERSION manually
    if ! python3 -c "import sys; v=tuple(int(x) for x in '$PY_VERSION'.split('.')); sys.exit(0 if v >= (3, 10) else 1)" 2>/dev/null; then
        fail "Python 3.10+ required (you have $PY_VERSION)"
    fi
fi

# Check ensurepip is available (Debian/Ubuntu may need python3-venv pkg)
if ! python3 -c 'import ensurepip' 2>/dev/null; then
    # Detect distro
    DISTRO=""
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        DISTRO="${ID:-}${VERSION_ID:+/$VERSION_ID}"
    fi
    case "$DISTRO" in
        ubuntu*|debian*)
            fail "python3-venv not installed. Run: sudo apt install python3.${PY_VERSION%%.*}-venv" ;;
        fedora*|rhel*|centos*)
            fail "python3-venv not installed. Run: sudo dnf install python3-venv" ;;
        arch*|manjaro*)
            fail "python3-venv not installed. Run: sudo pacman -S python-virtualenv" ;;
        *)
            fail "python3-venv not installed. Install your distro's python3-venv package." ;;
    esac
fi

# Find wheel + requirements if not specified
if [[ -z "$WHEEL_FILE" ]]; then
    WHEEL_FILE=$(ls "$SCRIPT_DIR"/hermes_orchestrator-*.whl 2>/dev/null | head -1 || true)
fi
if [[ -z "$WHEEL_FILE" ]]; then
    fail "wheel not found. Run with -w /path/to/hermes_orchestrator-*.whl or place it next to this script."
fi
log "  Wheel: $WHEEL_FILE"

if [[ -z "$REQUIREMENTS_FILE" ]]; then
    REQUIREMENTS_FILE="$SCRIPT_DIR/requirements-agent.txt"
fi
if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
    fail "requirements not found at $REQUIREMENTS_FILE. Run with -r /path/to/requirements-agent.txt"
fi
log "  Requirements: $REQUIREMENTS_FILE"

# Get home dir of target user
if [[ "$TARGET_USER" == "root" ]]; then
    TARGET_HOME="/root"
else
    TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6) || fail "cannot find home for user $TARGET_USER"
fi
log "  Target user: $TARGET_USER (home=$TARGET_HOME)"

INSTALL_DIR="$TARGET_HOME/.hermes-orchestrator"
log "  Install dir: $INSTALL_DIR"

# ---------- create venv ----------
log "Creating venv at $INSTALL_DIR/venv"
mkdir -p "$INSTALL_DIR"
# Run as target user so venv is owned by them
if [[ "$TARGET_USER" != "$(id -un)" ]]; then
    sudo -u "$TARGET_USER" python3 -m venv "$INSTALL_DIR/venv"
else
    python3 -m venv "$INSTALL_DIR/venv"
fi

# ---------- install deps + wheel ----------
log "Installing dependencies"
sudo -u "$TARGET_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip 2>&1 | tail -3 || true
sudo -u "$TARGET_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$REQUIREMENTS_FILE" 2>&1 | tail -5

log "Installing hermes-orchestrator wheel"
sudo -u "$TARGET_USER" "$INSTALL_DIR/venv/bin/pip" install "$WHEEL_FILE" 2>&1 | tail -3

# Verify CLI works
log "Verifying hermes-orch-agent CLI"
sudo -u "$TARGET_USER" "$INSTALL_DIR/venv/bin/hermes-orch-agent" --help | head -3

# ---------- register with orchestrator ----------
# Only register if both URL and agent_id provided
SHOULD_REGISTER=1
if [[ -z "$ORCHESTRATOR_URL" ]]; then
    log "No --orchestrator-url given; skipping register (run hermes-orch-agent register manually later)"
    SHOULD_REGISTER=0
elif [[ -z "$AGENT_ID" ]]; then
    log "No --agent-id given; skipping register"
    SHOULD_REGISTER=0
fi

if [[ "$SHOULD_REGISTER" == "1" ]]; then
    # Auto-detect IP
    if [[ -z "${IP:-}" ]]; then
        IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
    fi
    log "Registering $AGENT_ID with $ORCHESTRATOR_URL (ip=$IP, os=linux)"
    log "  (If --roles not given, auto-detects from HERMES_PROFILES_DIR or ~/.local/share/hermes/profiles)"
    sudo -u "$TARGET_USER" "$INSTALL_DIR/venv/bin/hermes-orch-agent" register \
        --orchestrator "$ORCHESTRATOR_URL" \
        --agent-id "$AGENT_ID" \
        --ip "$IP" \
        --os-type linux \
        || log "  WARN: register failed (you can run it manually later with --roles)"
fi

# ---------- systemd ----------
if [[ "$INSTALL_SYSTEMD" == "1" ]]; then
    log "Installing systemd unit (needs sudo)"
    if ! command -v systemctl >/dev/null; then
        fail "systemctl not found. Use -s to skip systemd install."
    fi

    # Render unit with correct paths (replace /home/stanley with $TARGET_HOME)
    # Look in the bundle dir first (where install-agent.sh is), then fall back
    # to the source repo path.
    if [[ -f "$SCRIPT_DIR/hermes-orch-agent.service" ]]; then
        UNIT_SRC="$SCRIPT_DIR/hermes-orch-agent.service"
    elif [[ -f "$SCRIPT_DIR/../packaging/systemd/hermes-orch-agent.service" ]]; then
        UNIT_SRC="$SCRIPT_DIR/../packaging/systemd/hermes-orch-agent.service"
    else
        fail "systemd unit not found. Place hermes-orch-agent.service next to install-agent.sh."
    fi

    UNIT_TMP="/tmp/hermes-orch-agent.service"
    sed -e "s|/home/stanley|$TARGET_HOME|g" \
        -e "s|^User=.*|User=$TARGET_USER|" \
        -e "s|^Group=.*|Group=$TARGET_USER|" \
        "$UNIT_SRC" > "$UNIT_TMP"

    sudo cp "$UNIT_TMP" /etc/systemd/system/hermes-orch-agent.service
    sudo systemctl daemon-reload
    sudo systemctl enable hermes-orch-agent.service

    if sudo systemctl is-active --quiet hermes-orch-agent.service; then
        log "Daemon already running; restarting with new unit"
        sudo systemctl restart hermes-orch-agent.service
    else
        log "Starting daemon"
        sudo systemctl start hermes-orch-agent.service
    fi

    log "Status:"
    sudo systemctl status hermes-orch-agent.service --no-pager | head -10
else
    log "Skipped systemd install (no sudo). To start manually:"
    log "  $INSTALL_DIR/venv/bin/hermes-orch-agent start"
fi

log ""
log "=== Install complete ==="
log "Wheel: $WHEEL_FILE"
log "Venv:  $INSTALL_DIR/venv"
log "CLI:   $INSTALL_DIR/venv/bin/hermes-orch-agent"
log ""
log "Next steps:"
log "  1. Check wrapper-config.json at $INSTALL_DIR/wrapper-config.json (set hermes profile roots)"
log "  2. Verify registration: $INSTALL_DIR/venv/bin/hermes-orch-agent status"
log "  3. Watch logs:    journalctl -u hermes-orch-agent -f  (if systemd)"
log "                     tail -f $INSTALL_DIR/daemon.log          (if no systemd)"
