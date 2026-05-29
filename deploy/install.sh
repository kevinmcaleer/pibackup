#!/bin/sh
# pibackup installer — run as root so scheduled backups can read every file
# (e.g. root-only paths under /etc), not just the ones the invoking user owns.
#
#   curl -fsSL https://raw.githubusercontent.com/kevinmcaleer/pibackup/main/deploy/install.sh | sudo sh
#
# Enroll this Pi with a server in one go (env vars, easier to copy-paste):
#   curl -fsSL https://raw.githubusercontent.com/kevinmcaleer/pibackup/main/deploy/install.sh \
#     | sudo SERVER=https://hub.local TOKEN=abc123 NAME=kitchen-pi TIMER=1 AGENT=1 sh
#
# Or the long form with flags (after `| sudo sh -s --`):
#   --server URL --name NAME --token TOKEN   enroll this Pi straight away
#   --timer                                   install + enable the daily backup timer
#   --agent                                   install + enable the agent poller
#                                             (acts on server start/stop commands)
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "pibackup installs as root so backups can read every file." >&2
  echo "Re-run piped to sudo, e.g.:" >&2
  echo "  curl -fsSL <url> | sudo sh -s -- --server <url> --name <pi> --token <tok> --timer" >&2
  exit 1
fi

REPO="https://github.com/kevinmcaleer/pibackup"
RAW="https://raw.githubusercontent.com/kevinmcaleer/pibackup/main/deploy"

# Env-var defaults — flags below still win if both are given.
SERVER="${SERVER:-}"
NAME="${NAME:-}"
TOKEN="${TOKEN:-}"
TIMER="${TIMER:-0}"
AGENT="${AGENT:-0}"

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --name)   NAME="$2";   shift 2 ;;
    --token)  TOKEN="$2";  shift 2 ;;
    --timer)  TIMER=1;     shift ;;
    --agent)  AGENT=1;     shift ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

echo "Installing pibackup…"

# Ensure rsync is available — required for all backup transfers.
if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync not found — installing…"
  if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    sudo apt-get install -y rsync
  else
    echo "ERROR: rsync is not installed and could not be installed automatically." >&2
    echo "Please install rsync manually and re-run this script." >&2
    exit 1
  fi
fi

# The [crypto] extra (pyrage + zstandard) lets this client run encrypted jobs
# and restore encrypted snapshots; harmless for plaintext-only clients.
SPEC="pibackup[crypto] @ git+$REPO"

install_with_pipx() {
  pipx install "$SPEC" 2>/dev/null || pipx upgrade pibackup
}

install_with_venv() {
  # PEP 668-safe fallback: a self-contained venv, symlinked onto PATH. Used
  # when pipx isn't available and we can't apt-install it.
  venv="$HOME/.local/share/pibackup/venv"
  python3 -m venv "$venv"
  "$venv/bin/pip" install --quiet --upgrade pip
  "$venv/bin/pip" install "$SPEC"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$venv/bin/pibackup" "$HOME/.local/bin/pibackup"
}

# Raspberry Pi OS / Debian block system-wide pip (PEP 668), so install via
# pipx — apt-installing it if needed — and fall back to a private venv.
# We're already root here, so no sudo is required for apt.
if command -v pipx >/dev/null 2>&1; then
  install_with_pipx
elif command -v apt-get >/dev/null 2>&1 && apt-get install -y pipx >/dev/null 2>&1; then
  install_with_pipx
else
  install_with_venv
fi

BIN="$(command -v pibackup || echo "$HOME/.local/bin/pibackup")"
echo "Installed: $BIN"

if [ -n "$TOKEN" ] && [ -n "$SERVER" ]; then
  echo "Enrolling with $SERVER…"
  if [ -n "$NAME" ]; then
    "$BIN" connect "$SERVER" --name "$NAME" --token "$TOKEN"
  else
    "$BIN" connect "$SERVER" --token "$TOKEN"
  fi
fi

if [ "$TIMER" = "1" ] || [ "$TIMER" = "true" ]; then
  echo "Installing the daily backup timer (system service, runs as root)…"
  curl -fsSL "$RAW/pibackup-backup.service" -o /etc/systemd/system/pibackup-backup.service
  curl -fsSL "$RAW/pibackup-backup.timer"   -o /etc/systemd/system/pibackup-backup.timer
  systemctl daemon-reload
  systemctl enable --now pibackup-backup.timer
fi

if [ "$AGENT" = "1" ] || [ "$AGENT" = "true" ]; then
  echo "Installing the agent poller (system service, runs as root)…"
  curl -fsSL "$RAW/pibackup-agent.service" -o /etc/systemd/system/pibackup-agent.service
  systemctl daemon-reload
  systemctl enable --now pibackup-agent.service
fi

echo "Done. Try: pibackup status"
