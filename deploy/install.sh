#!/bin/sh
# pibackup installer.
#
#   curl -fsSL https://raw.githubusercontent.com/kevinmcaleer/pibackup/main/deploy/install.sh | sh
#
# Optional flags (after `| sh -s --`):
#   --server URL --name NAME --token TOKEN   enroll this Pi straight away
#   --timer                                   install + enable the daily backup timer
set -eu

REPO="https://github.com/kevinmcaleer/pibackup"
RAW="https://raw.githubusercontent.com/kevinmcaleer/pibackup/main/deploy"
SERVER=""; NAME=""; TOKEN=""; TIMER=0

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --name)   NAME="$2";   shift 2 ;;
    --token)  TOKEN="$2";  shift 2 ;;
    --timer)  TIMER=1;     shift ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

echo "Installing pibackup…"
if command -v pipx >/dev/null 2>&1; then
  pipx install "git+$REPO" 2>/dev/null || pipx upgrade pibackup
else
  python3 -m pip install --user "git+$REPO"
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

if [ "$TIMER" -eq 1 ]; then
  echo "Installing the daily backup timer…"
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  curl -fsSL "$RAW/pibackup-backup.service" -o "$UNIT_DIR/pibackup-backup.service"
  curl -fsSL "$RAW/pibackup-backup.timer"   -o "$UNIT_DIR/pibackup-backup.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now pibackup-backup.timer
fi

echo "Done. Try: pibackup status"
