#!/usr/bin/env bash
# Remote hourly public-data and paper-observation cycle. It never submits orders.
set -euo pipefail

APP_DIR="${CRYPTO_QUANT_APP_DIR:-/opt/crypto-quant/app}"
PYTHON="$APP_DIR/.venv/bin/python"
LOCK_FILE="$APP_DIR/data/.hourly-observe.lock"

cd "$APP_DIR"
mkdir -p "$APP_DIR/data"

exec /usr/bin/flock -n -E 75 "$LOCK_FILE" /bin/bash -c '
  set -euo pipefail
  "$1" collector.py update
  "$1" carry_trader.py run
  "$1" paper_trader.py run
  "$1" polymarket_data.py
  "$1" polymarket_paper.py
' _ "$PYTHON"
