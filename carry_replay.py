#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recent OKX funding carry replay using local UTC daily candles and actual OKX funding.

This is a simplified historical simulation, not a record of executable returns. It uses
one completed daily candle per funding settlement and never writes the live carry ledger.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = DATA / "carry_replay"
CONFIG = json.loads((ROOT / "carry_config.json").read_text())
DAY_MS = 86_400_000


def annual_cost():
    return ((CONFIG["round_trip_cost_pct"] + CONFIG["basis_buffer_pct"])
            * 365 / CONFIG["expected_hold_days"])


def quote(candle, side):
    if side == "buy":
        return float(candle.open) * (1 + CONFIG["slippage"])
    return float(candle.open) * (1 - CONFIG["slippage"])


def load_inputs():
    spot = pd.read_parquet(DATA / f"okx_candles_{CONFIG['spot_inst']}.parquet").sort_values("ts")
    swap = pd.read_parquet(DATA / f"okx_candles_{CONFIG['swap_inst']}.parquet").sort_values("ts")
    funding = pd.read_parquet(DATA / f"okx_funding_{CONFIG['swap_inst']}.parquet").sort_values("ts")
    return spot, swap, funding


def replay():
    spot, swap, funding = load_inputs()
    OUT.mkdir(parents=True, exist_ok=True)
    capital = float(CONFIG["initial_equity"])
    initial = capital
    notional = min(
        capital * CONFIG["max_notional_pct"],
        capital / (1 + 1 / CONFIG["margin_leverage"]),
    )
    fee_rate = CONFIG["spot_fee"] + CONFIG["swap_fee"]
    position = None
    rows = []
    skipped = {"missing_price": 0, "edge": 0, "basis": 0}
    peak = capital
    max_drawdown = 0.0

    for fund in funding.itertuples():
        day = (int(fund.ts) // DAY_MS) * DAY_MS
        spot_day = spot[spot.ts == day]
        swap_day = swap[swap.ts == day]
        if spot_day.empty or swap_day.empty:
            skipped["missing_price"] += 1
            continue
        sp = spot_day.iloc[0]
        sw = swap_day.iloc[0]
        basis = float(sw.close) / float(sp.close) - 1
        annual = float(fund.funding_rate) * 3 * 365
        edge = annual - annual_cost()
        action = "hold"
        funding_cash = 0.0
        trade_pnl = 0.0

        if position is not None:
            funding_cash = position["perp_qty"] * float(sw.close) * float(fund.funding_rate)
            capital += funding_cash
            if (float(fund.funding_rate) < 0 or annual < CONFIG["min_funding_annual_pct"]
                    or abs(basis) > CONFIG["max_basis_pct"]):
                exit_spot = quote(sp, "sell")
                exit_swap = quote(sw, "buy")
                trade_pnl = (position["spot_qty"] * (exit_spot - position["spot_px"])
                             + position["perp_qty"] * (position["swap_px"] - exit_swap))
                close_fee = position["notional"] * fee_rate
                capital += trade_pnl - close_fee
                action = "close"
                position = None
        elif abs(basis) > CONFIG["max_basis_pct"]:
            skipped["basis"] += 1
        elif annual < CONFIG["min_funding_annual_pct"] or edge < CONFIG["min_net_edge_pct"]:
            skipped["edge"] += 1
        else:
            entry_spot = quote(sp, "buy")
            entry_swap = quote(sw, "sell")
            open_notional = min(
                capital * CONFIG["max_notional_pct"],
                capital / (1 + 1 / CONFIG["margin_leverage"]),
            )
            capital -= open_notional * fee_rate
            position = {"spot_qty": open_notional / entry_spot,
                        "perp_qty": open_notional / entry_swap,
                        "spot_px": entry_spot, "swap_px": entry_swap,
                        "notional": open_notional}
            action = "open"

        if position is not None:
            marked = (position["spot_qty"] * (float(sp.close) - position["spot_px"])
                      + position["perp_qty"] * (position["swap_px"] - float(sw.close)))
        else:
            marked = 0.0
        equity = capital + marked
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        rows.append({"ts": int(fund.ts), "funding_rate": float(fund.funding_rate),
                     "annual_funding": annual, "net_edge": edge, "basis": basis,
                     "action": action, "funding_cash": funding_cash,
                     "trade_pnl": trade_pnl, "equity": equity,
                     "position_open": position is not None})

    if position is not None:
        last_sp = spot.iloc[-1]
        last_sw = swap.iloc[-1]
        exit_spot = quote(last_sp, "sell")
        exit_swap = quote(last_sw, "buy")
        capital += (position["spot_qty"] * (exit_spot - position["spot_px"])
                    + position["perp_qty"] * (position["swap_px"] - exit_swap)
                    - position["notional"] * fee_rate)
        rows.append({"ts": int(max(last_sp.ts, last_sw.ts)), "funding_rate": None,
                     "annual_funding": None, "net_edge": None,
                     "basis": float(last_sw.close) / float(last_sp.close) - 1,
                     "action": "close_end_of_sample", "funding_cash": 0.0,
                     "trade_pnl": 0.0, "equity": capital, "position_open": False})

    result = pd.DataFrame(rows)
    result.to_csv(OUT / "settlement_replay.csv", index=False)
    first = datetime.fromtimestamp(int(funding.ts.min()) / 1000, timezone.utc).strftime("%F")
    last = datetime.fromtimestamp(int(funding.ts.max()) / 1000, timezone.utc).strftime("%F")
    eligible = len(result[result.action.isin(["open", "close", "hold"])])
    lines = ["# OKX 近期 Funding Carry 历史回放", "",
             "## 口径",
             f"- 实际 OKX funding 结算记录：{first} 至 {last}，共 {len(funding)} 条；价格使用本地 UTC 日K。",
             "- 每个 funding 时点只可使用当日可获得的日线开盘报价模拟成交；日K分辨率无法复原盘口、逐笔基差与真实成交。",
             "- 这是近三个月 OKX 数据的简化历史模拟，不是 OKX 已实现收益，也不使用 Deribit 代理。", "",
             "## 结果",
             f"- 初始 ${initial:.2f} | 期末 ${capital:.2f} | 净收益 ${capital - initial:+.2f} ({capital / initial - 1:+.2%})",
             f"- 最大回撤 {max_drawdown:.2%} | 开仓 {(result.action == 'open').sum()} 次 | 平仓 {(result.action.str.startswith('close')).sum()} 次",
             f"- 结算覆盖 {eligible}/{len(funding)} | 跳过：edge {skipped['edge']}，基差 {skipped['basis']}，缺价 {skipped['missing_price']}",
             f"- 年化成本/缓冲 {annual_cost() * 100:.2f}%（固定 30 天预期持有期口径）。", "",
             "## 限制",
             "- 回放不包含订单簿容量、动态手续费等级、强平、利息、API 延迟或交易所信用风险。",
             "- 只有连续实时 paper ledger 与更高频可执行价格验证后，才可评估任何实盘准备。"]
    (OUT / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return result


def main():
    if len(sys.argv) > 1 and sys.argv[1] not in {"run", "report"}:
        raise SystemExit("usage: python carry_replay.py [run|report]")
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        print((OUT / "report.md").read_text())
        return
    replay()


if __name__ == "__main__":
    main()
