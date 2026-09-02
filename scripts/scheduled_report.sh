#!/usr/bin/env bash
# Scheduled governance + slot report + half-day sync package.
# Read-only over ledgers: waits on the observation lock, never trades, never edits config.
set -euo pipefail

APP_DIR="${CRYPTO_QUANT_APP_DIR:-/opt/crypto-quant/app}"
PYTHON="$APP_DIR/.venv/bin/python"
LOCK_FILE="$APP_DIR/data/.hourly-observe.lock"
SLOT="${1:-}"

cd "$APP_DIR"
mkdir -p "$APP_DIR/data"

# Serialize against the hourly observation cycle so reports never read
# half-written ledgers. Exit 75 on contention, treated as success by systemd.
/usr/bin/flock -n -E 75 "$LOCK_FILE" /bin/bash -c '
  set -euo pipefail
  "$1" governance.py review
  if [ -n "$2" ]; then
    "$1" daily_report.py --slot "$2"
  else
    "$1" daily_report.py
  fi
' _ "$PYTHON" "$SLOT"
