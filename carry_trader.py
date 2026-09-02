#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OKX funding carry paper ledger: spot long + equal notional swap short.

No API keys and no orders are used. Public OKX ticker/funding only.
"""
import csv
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from runtime_provenance import provenance
from http_transport import request_get

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CARRY = DATA / "carry"
CARRY.mkdir(parents=True, exist_ok=True)
ACCOUNT = CARRY / "account.json"
EVENTS = CARRY / "events.jsonl"
SNAPSHOTS = CARRY / "snapshots.csv"
FUNDING_LEDGER = CARRY / "funding_ledger.csv"
REPORT = CARRY / "report.md"
LOCK = CARRY / ".run.lock"
CONFIG = json.loads((ROOT / "carry_config.json").read_text())


def now_ms():
    return int(time.time() * 1000)


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_jsonl(path, event):
    with path.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


@contextmanager
def run_lock():
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("carry run already in progress")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        yield
    finally:
        LOCK.unlink(missing_ok=True)


def append_funding_row(ev):
    fields = ["event_id", "ts", "rate", "notional", "cashflow", "src", "complete"]
    new = not FUNDING_LEDGER.exists()
    with FUNDING_LEDGER.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerow({k: ev[k] for k in fields})


def get(url, params):
    r = request_get(url, params=params, timeout=20,
                    headers={"User-Agent": "crypto-quant-carry-paper/0.1"},
                    proxy=CONFIG["proxy"])
    r.raise_for_status()
    j = r.json()
    if str(j.get("code")) not in ("0", "200", "None"):
        raise RuntimeError(f"OKX error {j.get('code')}: {j.get('msg')}")
    return j.get("data", [])


def market_snapshot():
    spot = get("https://www.okx.com/api/v5/market/ticker", {"instId": CONFIG["spot_inst"]})[0]
    swap = get("https://www.okx.com/api/v5/market/ticker", {"instId": CONFIG["swap_inst"]})[0]
    mark = get("https://www.okx.com/api/v5/public/mark-price", {"instType": "SWAP", "instId": CONFIG["swap_inst"]})[0]
    index = get("https://www.okx.com/api/v5/market/index-tickers", {"instId": CONFIG["index_inst"]})[0]
    funding = get("https://www.okx.com/api/v5/public/funding-rate", {"instId": CONFIG["swap_inst"]})[0]
    ts = now_ms()
    s = {
        "observed_at": ts,
        "spot_last": float(spot["last"]), "spot_bid": float(spot["bidPx"]), "spot_ask": float(spot["askPx"]),
        "swap_last": float(swap["last"]), "swap_bid": float(swap["bidPx"]), "swap_ask": float(swap["askPx"]),
        "swap_mark": float(mark["markPx"]),
        "swap_index": float(index["idxPx"]),
        "funding_rate": float(funding["fundingRate"]),
        "funding_time": int(funding.get("fundingTime") or 0),
        "next_funding_time": int(funding.get("nextFundingTime") or 0),
        "source": "okx",
    }
    s["basis"] = s["swap_mark"] / s["spot_last"] - 1
    s["spread_bps"] = (s["spot_ask"] - s["spot_bid"]) / s["spot_last"] * 10000
    s["age_ms"] = 0
    return s


def load_account():
    if ACCOUNT.exists():
        a = json.loads(ACCOUNT.read_text())
        a.setdefault("schema", 1)
        return a
    return {
        "schema": 1, "created_at": now_ms(), "initial_equity": CONFIG["initial_equity"],
        "cash": CONFIG["initial_equity"], "spot_qty": 0.0, "perp_qty": 0.0, "short_notional": 0.0,
        "entry_spot": None, "entry_swap": None, "margin": 0.0,
        "status": "flat", "halt_reason": None, "last_funding_ts": 0,
        "last_snapshot_ts": 0, "realized_funding": 0.0, "fees": 0.0,
        "equity_history": [], "trades": [], "funding_events": [],
    }


def mark(a, s):
    spot_pnl = a["spot_qty"] * (s["spot_last"] - (a["entry_spot"] or s["spot_last"]))
    swap_pnl = a.get("perp_qty", 0.0) * ((a["entry_swap"] or s["swap_mark"]) - s["swap_mark"])
    basis_pnl = spot_pnl + swap_pnl
    equity = a["cash"] + basis_pnl
    return spot_pnl, swap_pnl, basis_pnl, equity


def funding_events(a, s):
    path = DATA / ("okx_funding_" + CONFIG["swap_inst"] + ".parquet")
    if not path.exists() or a.get("spot_qty", 0.0) <= 0 or a.get("perp_qty", 0.0) <= 0:
        return 0.0, 0
    df = pd.read_parquet(path).sort_values("ts")
    rows = df[(df["ts"] > int(a.get("last_funding_ts", 0))) & (df["ts"] <= s["observed_at"])]
    total = 0.0
    count = 0
    last_processed = int(a.get("last_funding_ts", 0))
    settled = {int(x.get("ts", -1)) for x in a["funding_events"]}
    for row in rows.itertuples():
        rate = float(row.funding_rate)
        notional = a.get("perp_qty", 0.0) * s["swap_mark"]
        amount = notional * rate
        # Positive funding is paid by longs and received by a short.
        ev = {"event_id": f"funding:{CONFIG['swap_inst']}:{int(row.ts)}",
              "ts": int(row.ts), "rate": rate, "notional": notional,
              "cashflow": amount, "src": "okx", "complete": True}
        if int(row.ts) in settled:
            last_processed = max(last_processed, int(row.ts))
            continue
        a["cash"] += amount
        a["realized_funding"] += amount
        a["funding_events"].append(ev)
        append_jsonl(EVENTS, {"type": "funding", **ev})
        append_funding_row(ev)
        settled.add(int(row.ts))
        last_processed = max(last_processed, int(row.ts))
        total += amount
        count += 1
    # Advance only after every eligible settlement was reconciled. Advancing to the
    # newest parquet row would permanently skip an outage-delayed settlement.
    a["last_funding_ts"] = last_processed
    return total, count


def max_open_notional(equity):
    """Spot purchase plus isolated swap margin must fit within paper equity."""
    configured = equity * CONFIG["max_notional_pct"]
    capital_limited = equity / (1 + 1 / CONFIG["margin_leverage"])
    return min(configured, capital_limited)


def open_position(a, s, reason):
    equity = a["cash"]
    notional = max_open_notional(equity)
    spot_px = s["spot_ask"] * (1 + CONFIG["slippage"])
    swap_px = s["swap_bid"] * (1 - CONFIG["slippage"])
    fee = notional * (CONFIG["spot_fee"] + CONFIG["swap_fee"])
    a["cash"] -= fee
    a["fees"] += fee
    a["spot_qty"] = notional / spot_px
    a["perp_qty"] = notional / swap_px
    a["short_notional"] = notional
    a["entry_spot"] = spot_px
    a["entry_swap"] = swap_px
    a["margin"] = notional / CONFIG["margin_leverage"]
    a["last_funding_ts"] = max(int(a.get("last_funding_ts", 0)), int(s["observed_at"]))
    a["status"] = "open"
    a["halt_reason"] = None
    trade = {"ts": s["observed_at"], "action": "open", "notional": notional,
             "spot_px": spot_px, "swap_px": swap_px, "fee": fee, "reason": reason}
    a["trades"].append(trade)
    append_jsonl(EVENTS, {"type": "execution", **trade})


def close_position(a, s, reason):
    if a.get("spot_qty", 0.0) <= 0 or a.get("perp_qty", 0.0) <= 0:
        return
    spot_px = s["spot_bid"] * (1 - CONFIG["slippage"])
    swap_px = s["swap_ask"] * (1 + CONFIG["slippage"])
    fee = a["short_notional"] * (CONFIG["spot_fee"] + CONFIG["swap_fee"])
    spot_pnl = a["spot_qty"] * (spot_px - a["entry_spot"])
    swap_pnl = a["perp_qty"] * (a["entry_swap"] - swap_px)
    basis_pnl = spot_pnl + swap_pnl
    a["cash"] += basis_pnl - fee
    a["fees"] += fee
    trade = {"ts": s["observed_at"], "action": "close", "notional": a["short_notional"],
             "spot_px": spot_px, "swap_px": swap_px, "basis_pnl": basis_pnl,
             "fee": fee, "reason": reason}
    a["trades"].append(trade)
    append_jsonl(EVENTS, {"type": "execution", **trade})
    a["spot_qty"] = 0.0
    a["perp_qty"] = 0.0
    a["short_notional"] = 0.0
    a["entry_spot"] = None
    a["entry_swap"] = None
    a["margin"] = 0.0
    a["status"] = "flat"


def decision(a, s):
    age = s["age_ms"]
    if age > CONFIG["max_data_age_ms"]:
        return "halt", "stale_market_data"
    if abs(s["basis"]) > CONFIG["max_basis_pct"]:
        return ("close", "basis_out_of_range") if a.get("spot_qty", 0.0) > 0 else ("halt", "basis_out_of_range")
    annual = s["funding_rate"] * 3 * 365
    annual_cost = (CONFIG["round_trip_cost_pct"] + CONFIG["basis_buffer_pct"]) * 365 / CONFIG["expected_hold_days"]
    net_edge = annual - annual_cost
    if a.get("spot_qty", 0.0) > 0 and (s["funding_rate"] < 0 or annual < CONFIG["min_funding_annual_pct"]):
        return "close", "funding_below_minimum"
    if a.get("spot_qty", 0.0) > 0:
        return "hold", None
    if net_edge >= CONFIG["min_net_edge_pct"] and annual >= CONFIG["min_funding_annual_pct"]:
        return "open", f"net_edge_{net_edge:.4f}"
    return "hold", f"net_edge_{net_edge:.4f}_below_threshold"


def write_report(a, s, action, reason, funding_cash, funding_count):
    spot_pnl, swap_pnl, basis_pnl, equity = mark(a, s)
    peak = max([x["equity"] for x in a["equity_history"]] + [a["initial_equity"]])
    dd = equity / peak - 1
    annual = s["funding_rate"] * 3 * 365
    annual_cost = (CONFIG["round_trip_cost_pct"] + CONFIG["basis_buffer_pct"]) * 365 / CONFIG["expected_hold_days"]
    net_edge = annual - annual_cost
    lines = [f"# Carry 虚拟账本报告 {datetime.now(timezone.utc):%F %H:%M} UTC", "",
             "## 状态", f"- 状态 **{a['status']}** | 本轮动作 **{action}** | 原因 `{reason or '-'} `",
             f"- 现货多 {a['spot_qty']:.8f} BTC | 永续空名义 ${a['short_notional']:.2f} | 保证金 ${a['margin']:.2f}",
             f"- 现货 ${s['spot_last']:.2f} | 永续 mark ${s['swap_mark']:.2f} | 基差 {s['basis']*100:+.3f}%",
             f"- 当前 funding {s['funding_rate']*100:+.4f}%/8h | 简单年化 {annual*100:+.2f}% | 净 edge {net_edge*100:+.2f}%", "",
             "## PnL 分解", f"- 账本权益 **${equity:.2f}** / 初始 ${a['initial_equity']:.2f} | 累计 {equity/a['initial_equity']-1:+.2%}",
             f"- 基差/对冲残差 ${basis_pnl:+.4f} (现货 ${spot_pnl:+.4f} + 永续 ${swap_pnl:+.4f})",
             f"- 本轮已结算 funding ${funding_cash:+.4f} ({funding_count} 笔) | 累计 funding ${a['realized_funding']:+.4f}",
             f"- 累计手续费 ${a['fees']:.4f} | 当前回撤 {dd:.2%}", "",
             "## 风控", f"- 数据年龄 {age_text(s['age_ms'])} | 最大允许基差 {CONFIG['max_basis_pct']:.2%} | 最大名义仓位 {CONFIG['max_notional_pct']:.0%}",
             f"- 只使用 OKX 实时 funding 结算；Deribit 仅研究代理，不计入本账本。"]
    REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def age_text(ms):
    return f"{ms / 1000:.1f}s"


def run_once():
    prov = provenance("okx_funding_carry", ROOT / "carry_config.json")
    a = load_account()
    try:
        s = market_snapshot()
    except Exception as exc:
        # Do not invent an exit price when the public market snapshot is unavailable.
        # Existing simulated legs remain recorded and are re-evaluated on recovery.
        a["status"] = "halt"
        a["halt_reason"] = f"market_error:{exc}"
        atomic_json(ACCOUNT, a)
        REPORT.write_text(f"# Carry halted\n\n- 原因: `{exc}`\n")
        raise
    if a["status"] == "halt" and a.get("spot_qty", 0.0) > 0:
        a["status"] = "open"
        a["halt_reason"] = None
    elif (a["status"] == "halt" and str(a.get("halt_reason", "")).startswith("market_error")
          and a.get("spot_qty", 0.0) <= 0):
        a["status"] = "flat"
        a["halt_reason"] = None
    funding_cash, funding_count = funding_events(a, s)
    action, reason = decision(a, s)
    if action == "open":
        open_position(a, s, reason)
    elif action == "close":
        close_position(a, s, reason)
    elif action == "halt":
        a["status"] = "halt"
        a["halt_reason"] = reason
    elif a["status"] == "halt" and reason is None:
        a["status"] = "flat"
        a["halt_reason"] = None
    spot_pnl, swap_pnl, basis_pnl, equity = mark(a, s)
    event_key = f"mark:{s['observed_at']}"
    append_jsonl(EVENTS, {"type": "mark", "event_id": event_key, **prov, **s,
                          "status": a["status"], "equity": equity,
                          "basis_pnl": basis_pnl, "funding_cash": funding_cash})
    a["last_snapshot_ts"] = s["observed_at"]
    a["provenance"] = prov
    a["equity_history"].append({"ts": s["observed_at"], "equity": equity, "basis_pnl": basis_pnl,
                                 "funding": funding_cash, "status": a["status"]})
    a["equity_history"] = a["equity_history"][-5000:]
    atomic_json(ACCOUNT, a)
    if not SNAPSHOTS.exists():
        with SNAPSHOTS.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=list(s)).writeheader()
    with SNAPSHOTS.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=list(s)).writerow(s)
    write_report(a, s, action, reason, funding_cash, funding_count)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "init":
        if ACCOUNT.exists():
            print("carry 账户已存在，不覆盖")
        else:
            atomic_json(ACCOUNT, load_account())
            print("carry 账户已初始化: $%.2f" % CONFIG["initial_equity"])
        return
    if cmd == "report":
        if REPORT.exists():
            print(REPORT.read_text())
        else:
            print("尚无 carry 报告，请先运行 run")
        return
    if cmd == "status":
        a = load_account()
        print(json.dumps({k: a[k] for k in ("status", "cash", "spot_qty", "short_notional", "realized_funding", "fees", "last_snapshot_ts", "halt_reason")}, ensure_ascii=False, indent=2))
        return
    with run_lock():
        run_once()


if __name__ == "__main__":
    main()
