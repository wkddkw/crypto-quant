#!/usr/bin/env python3
"""Independent daily BTC trend shadow ledger; public prices, no real orders."""
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from paper_trader import FEE, INIT_CASH, MIN_TRADE_NOTIONAL, REBAL_TH, SLIP, live_price
from runtime_provenance import canonical_hash, provenance

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LEDGER = DATA / "trend_paper"
DAY_MS = 86_400_000
RULES = {"ma": [50, 200], "momentum_days": 90, "vol_ddof": 1,
         "donchian_days": 20, "score_max": 6, "fee": FEE, "slippage": SLIP,
         "rebalance_threshold": REBAL_TH, "min_notional": MIN_TRADE_NOTIONAL,
         "initial_equity": INIT_CASH, "rebalance": "once_per_closed_utc_day"}


def build_signal(now_ms):
    frame = pd.read_parquet(DATA / "okx_candles_BTC-USDT.parquet").sort_values("ts")
    frame = frame[frame["ts"] + DAY_MS <= now_ms].tail(201)
    if len(frame) < 201 or frame["ts"].diff().dropna().ne(DAY_MS).any():
        raise ValueError("incomplete_daily_candles")
    if int(frame["ts"].iloc[-1]) != (now_ms // DAY_MS - 1) * DAY_MS:
        raise ValueError("stale_daily_candles")
    values = frame[["close", "high"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("invalid_daily_prices")
    close = frame["close"].to_numpy(dtype=float)
    trend = (2 if close[-1] > close[-200:].mean() else -2)
    trend += 1 if close[-1] > close[-50:].mean() else -1
    vol = np.std(np.diff(np.log(close[-91:])), ddof=1) * np.sqrt(365)
    momentum = float(np.clip((close[-1] / close[-91] - 1) / vol * 2, -2, 2)) if vol else 0.0
    breakout = float(close[-1] >= frame["high"].iloc[-21:-1].max())
    signal = float(np.clip((trend + momentum + breakout) / 6, -1, 1))
    return {"candle_ts": int(frame["ts"].iloc[-1]), "s": signal,
            "target": max(0.0, signal), "trend": trend,
            "momentum": momentum, "breakout": breakout}


def advance(account, signal, price, now_ms):
    """Update both portfolios in one account transaction, with identical marks."""
    if not math.isfinite(price) or price <= 0:
        raise ValueError("invalid_ticker")
    if account is None:
        qty = INIT_CASH / (price * (1 + SLIP) * (1 + FEE))
        account = {"schema": 1, "strategy_id": "btc_trend_only_shadow",
                   "rules_sha256": canonical_hash(RULES), "initial_equity": INIT_CASH,
                   "created_at": now_ms, "cash": INIT_CASH, "btc": 0.0,
                   "fees": 0.0, "slippage_cost": 0.0, "last_candle_ts": None,
                   "trades": [], "decisions": [], "equity_history": [],
                   "daily_observations": [],
                   "benchmark": {"cash": 0.0, "btc": qty,
                                 "fees": qty * price * (1 + SLIP) * FEE,
                                 "slippage_cost": qty * price * SLIP}}
    if account.get("rules_sha256") != canonical_hash(RULES):
        raise ValueError("rules_changed_requires_new_ledger")
    if now_ms <= account.get("updated_at", -1):
        raise ValueError("non_increasing_observation_time")
    last_candle = account["last_candle_ts"]
    if last_candle is not None and signal["candle_ts"] < last_candle:
        raise ValueError("out_of_order_signal")
    new_day = signal["candle_ts"] != last_candle
    action = "mark"
    if new_day:
        action = "hold"
        equity = account["cash"] + account["btc"] * price
        weight = account["btc"] * price / equity
        if abs(signal["target"] - weight) >= REBAL_TH:
            diff = signal["target"] * equity - account["btc"] * price
            buy = diff > 0
            fill = price * (1 + SLIP if buy else 1 - SLIP)
            qty = min(abs(diff) / price,
                      account["cash"] / (fill * (1 + FEE)) if buy else account["btc"])
            if qty * fill >= MIN_TRADE_NOTIONAL:
                fee = qty * fill * FEE
                account["cash"] += -qty * fill - fee if buy else qty * fill - fee
                account["btc"] += qty if buy else -qty
                account["fees"] += fee
                account["slippage_cost"] += qty * abs(fill - price)
                action = "buy" if buy else "sell"
                account["trades"].append({"observed_at": now_ms, "side": action,
                                          "qty": qty, "px": fill, "usd": qty * fill,
                                          "fee": fee, "signal": signal["s"]})
        account["last_candle_ts"] = signal["candle_ts"]
    equity = account["cash"] + account["btc"] * price
    benchmark = account["benchmark"]["cash"] + account["benchmark"]["btc"] * price
    mark = {"observed_at": now_ms, "price": price, "equity": equity,
            "benchmark_equity": benchmark, "action": action, **signal}
    account["equity_history"].append(mark)
    if new_day:
        account["decisions"].append(mark)
        account["daily_observations"].append(mark)
    account.update(updated_at=now_ms, status="long" if account["btc"] > 0 else "flat")
    return account


def run_once():
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    signal = build_signal(now_ms)
    price = live_price()
    path = LEDGER / "account.json"
    account = json.loads(path.read_text()) if path.exists() else None
    account = advance(account, signal, price, now_ms)
    account["provenance"] = provenance("btc_trend_only_shadow")
    LEDGER.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(account, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
    print(json.dumps(account["equity_history"][-1]))


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["run"]):
        raise SystemExit("usage: trend_paper.py [run]")
    run_once()
