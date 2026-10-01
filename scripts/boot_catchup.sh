#!/usr/bin/env bash
# One persistent hourly timer invocation catches up once after restart and records skipped slots.
set -euo pipefail
APP_DIR="${CRYPTO_QUANT_APP_DIR:-$HOME/crypto-quant}"
if [[ "${1:-}" == "--boot" ]]; then
  if "$APP_DIR/.venv/bin/python" - "$APP_DIR/data/governance/observation_state.json" <<'BOOT_CHECK'
import json
import sys
from datetime import datetime
from pathlib import Path
state = Path(sys.argv[1])
if not state.exists():
    raise SystemExit(1)
last = datetime.fromisoformat(json.loads(state.read_text())["success_at"]).timestamp()
boot = int(next(line.split()[1] for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime ")))
raise SystemExit(0 if last >= boot else 1)
BOOT_CHECK
  then
    echo "Hourly observation already succeeded since boot; skip duplicate catchup."
    exit 0
  fi
fi
if [[ "${1:-}" == "--scheduled" ]]; then
  if "$APP_DIR/.venv/bin/python" - "$APP_DIR/data/governance/observation_state.json" <<'SLOT_CHECK'
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
state = Path(sys.argv[1])
if not state.exists():
    raise SystemExit(1)
zone = ZoneInfo("Asia/Shanghai")
now = datetime.now(zone)
slot = now.replace(minute=3, second=0, microsecond=0)
if now < slot:
    slot -= timedelta(hours=1)
last = datetime.fromisoformat(json.loads(state.read_text())["success_at"]).astimezone(zone)
raise SystemExit(0 if last >= slot else 1)
SLOT_CHECK
  then
    echo "Hourly observation already succeeded for this scheduled slot; skip duplicate."
    exit 0
  fi
fi
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
"$APP_DIR/scripts/hourly_observe.sh"
"$APP_DIR/.venv/bin/python" - "$APP_DIR" "$STARTED_AT" <<'PYTHON'
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

app = Path(sys.argv[1])
started = datetime.fromisoformat(sys.argv[2]).astimezone(timezone.utc)
finished = datetime.now(timezone.utc)
root = app / "data" / "governance"
root.mkdir(parents=True, exist_ok=True)
state_path = root / "observation_state.json"
status_path = app / "data" / "status.md"
match = re.search(r"okx_candles_BTC-USDT\.parquet.*?→\s*(\d{4}-\d{2}-\d{2})", status_path.read_text()) if status_path.exists() else None
latest_date = match.group(1) if match else None
previous = json.loads(state_path.read_text()) if state_path.exists() else None
if previous:
    zone = ZoneInfo("Asia/Shanghai")
    last = datetime.fromisoformat(previous["success_at"]).astimezone(zone)
    current = started.astimezone(zone)
    slot = last.replace(minute=3, second=0, microsecond=0)
    if slot <= last:
        slot += timedelta(hours=1)
    cutoff = current - timedelta(minutes=5)
    first = last_slot = None
    count = 0
    while slot <= cutoff:
        first = first or slot.isoformat()
        last_slot = slot.isoformat()
        count += 1
        slot += timedelta(hours=1)
    if count:
        event = {
            "kind": "missed_hourly_observation",
            "previous_success_at": previous["success_at"],
            "catchup_started_at": started.isoformat(),
            "catchup_finished_at": finished.isoformat(),
            "missed_slots_count": count,
            "first_missed_slot": first,
            "last_missed_slot": last_slot,
            "latest_btc_candle_date_before": previous.get("latest_btc_candle_date"),
            "latest_btc_candle_date_after": latest_date,
            "catchup_runs": 1,
            "historical_paper_trades_replayed": False,
        }
        with (root / "gap_log.jsonl").open("a") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
# Check report slots only after a ten-minute grace period. Never create a late slot snapshot.
check_from = datetime.fromisoformat(previous.get("report_check_until", previous["success_at"])) if previous else started
check_until = started - timedelta(minutes=10)
audit_path = root / "delivery_audit.jsonl"
seen = set()
if audit_path.exists():
    for line in audit_path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("outcome") == "missed" and row.get("error_class") == "missed_slot":
                seen.add(row.get("sync_id"))
zone = ZoneInfo("Asia/Shanghai")
day = check_from.astimezone(zone).date()
last_day = check_until.astimezone(zone).date()
while day <= last_day:
    for hour, label in ((6, "0600"), (18, "1800")):
        slot = datetime(day.year, day.month, day.day, hour, tzinfo=zone)
        if check_from < slot <= check_until:
            sync_id = f"{day.isoformat()}T{label}+0800"
            if not (app / "data" / "sync" / f"{sync_id}.json").exists() and sync_id not in seen:
                event = {"delivered_at": finished.isoformat(), "channel_alias": "local-only",
                         "sync_id": sync_id, "outcome": "missed", "message_id": None,
                         "error_class": "missed_slot"}
                with audit_path.open("a") as output:
                    output.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                seen.add(sync_id)
    day += timedelta(days=1)
state = {"success_at": finished.isoformat(), "latest_btc_candle_date": latest_date,
         "report_check_until": check_until.isoformat()}
temporary = state_path.with_suffix(".tmp")
temporary.write_text(json.dumps(state, ensure_ascii=False) + "\n")
temporary.replace(state_path)
PYTHON
