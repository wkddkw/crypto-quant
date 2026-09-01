#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 回测框架: v0 打分制 vs 基线策略, 同窗口同成本对比。

口径(与 paper_trader 一致):
  - 现货 long/flat, 目标仓位 = max(0, s), s = 总分/12
  - 成本: taker 0.1% + 滑点 0.03%(双边各计一次), 信号用已收盘日线
  - 防未来函数: 信号在 t 日收盘计算, t+1 日开盘成交; funding/F&G/MVRV 一律滞后 1 天
  - 调仓阈值: 目标权重偏离 ≥10 个百分点才动
  - 注意: 这是全样本内(2018-01→今)的初步校准, 不构成样本外结论; 样本外验证在 P2 walk-forward

用法: python backtest.py   → 输出对比表 + 落盘 data/backtest/
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "backtest"
DATA.mkdir(exist_ok=True, parents=True)

FEE = 0.001
SLIP = 0.0003
TH = 0.10


def band(x, hi2, hi1, lo1, lo2):
    """分位带打分: ≥hi2→-2, ≥hi1→-1, ≤lo2→+2, ≤lo1→+1, 其余 0(反指)。"""
    return np.select(
        [x >= hi2, x >= hi1, x <= lo2, x <= lo1],
        [-2.0, -1.0, 2.0, 1.0], default=0.0)


def build_targets():
    px = pd.read_parquet(DATA.parent / "okx_candles_BTC-USDT.parquet").sort_values("ts")
    px.index = pd.to_datetime(px.pop("ts"), unit="ms", utc=True)
    c, h = px["close"], px["high"]

    ma50 = c.rolling(50).mean()
    ma200 = c.rolling(200).mean()
    trend = pd.Series(np.where(c > ma200, 2.0, -2.0) + np.where(c > ma50, 1.0, -1.0), index=c.index)

    ret90 = c.pct_change(90)
    vol90 = np.log(c).diff().rolling(90).std() * np.sqrt(365)
    mom = (ret90 / vol90 * 2.0).clip(-2, 2)

    donch = (c >= h.rolling(20).max().shift(1)).astype(float)

    fnd = pd.read_parquet(DATA.parent / "deribit_funding_BTC-PERPETUAL.parquet").sort_values("ts")
    f = pd.Series(fnd["funding_rate"].values,
                  index=pd.to_datetime(fnd.pop("ts"), unit="ms", utc=True)).resample("1D").sum()
    f_pct = f.rolling(90, min_periods=50).rank(pct=True)
    funding = pd.Series(band(f_pct, 0.90, 0.75, 0.25, 0.10), index=f.index).shift(1)

    fg = pd.read_parquet(DATA.parent / "fng.parquet").sort_values("ts")
    fg_v = pd.Series(fg["value"].values, index=pd.to_datetime(fg.pop("ts"), unit="ms", utc=True))
    fg_s = pd.Series(np.select([fg_v >= 80, fg_v >= 70, fg_v <= 20, fg_v <= 30],
                               [-2.0, -1.0, 2.0, 1.0], default=0.0), index=fg_v.index).shift(1)

    cm = pd.read_parquet(DATA.parent / "cm_btc.parquet").sort_values("ts")
    mv = pd.Series(pd.to_numeric(cm["mvrv"], errors="coerce").values,
                   index=pd.to_datetime(cm.pop("ts"), unit="ms", utc=True)).dropna()
    mv_s = pd.Series(np.select([mv >= 2.5, mv >= 2.0, mv <= 0.8, mv <= 1.0],
                               [-2.0, -1.0, 2.0, 1.0], default=0.0), index=mv.index).shift(1)

    idx = c.index
    funding = funding.reindex(idx).ffill().fillna(0.0)
    fg_s = fg_s.reindex(idx).ffill().fillna(0.0)
    mv_s = mv_s.reindex(idx).ffill().fillna(0.0)

    sent = funding + fg_s + mv_s
    total_full = trend + mom + donch + sent
    total_trend = trend + mom + donch
    return px, {
        "v0_full(trend+情绪)": total_full.clip(-12, 12) / 12.0,
        "v0_trend_only(无情绪)": total_trend.clip(-6, 6) / 6.0,
        "ma200择时": (c > ma200).astype(float),
    }


def simulate(target_s, px, start_i, end_i=None, weekly=False):
    """信号 t 收盘 → t+1 开盘成交; 返回净值序列(手续费后)与统计。
    end_i/weekly 供 P2 滚动验证与降频实验复用。"""
    o, c = px["open"].values, px["close"].values
    idx_dt = px.index
    tgt = target_s.reindex(px.index).values
    cash, btc, w = 1.0, 0.0, 0.0
    eq, ws, trades, fees = [], [], 0, 0.0
    for t in range(start_i + 1, end_i if end_i is not None else len(c)):
        want = tgt[t - 1]
        if not np.isnan(want) and (not weekly or idx_dt[t].weekday() == 0):
            eq_open = cash + btc * o[t]
            if abs(want - w) >= TH:
                diff_val = want * eq_open - btc * o[t]
                qty = abs(diff_val) / o[t]
                p = o[t] * (1 + SLIP) if diff_val > 0 else o[t] * (1 - SLIP)
                if diff_val > 0:
                    qty = min(qty, cash / (p * (1 + FEE)))
                    spend = qty * p * (1 + FEE)
                    cash -= spend
                    btc += qty
                else:
                    qty = min(qty, btc)
                    cash += qty * p * (1 - FEE)
                    btc -= qty
                trades += 1
                fees += qty * p * FEE
        eq.append(cash + btc * c[t])
        eqv = eq[-1]
        w = btc * c[t] / eqv if eqv > 0 else 0.0
        ws.append(w)
    idx = px.index[start_i + 1: start_i + 1 + len(eq)]
    return pd.Series(eq, index=idx), trades, fees, pd.Series(ws, index=idx)


def metrics(eq, w):
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    mdd = (eq / eq.cummax() - 1).min()
    sharpe = float(r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else 0.0
    return {"总收益": "%+.0f%%" % ((eq.iloc[-1] / eq.iloc[0] - 1) * 100),
            "年化": "%+.1f%%" % (cagr * 100),
            "最大回撤": "%.1f%%" % (mdd * 100),
            "Sharpe": round(sharpe, 2),
            "在场率": "%.0f%%" % (float((w > 0.01).mean()) * 100)}


def main():
    px, targets = build_targets()
    start_i = 200  # MA200 预热后同窗口对比
    start = px.index[start_i]

    # 买入持有基线(同窗口, 一次性成本 0.13%)
    c = px["close"]
    bh = c.iloc[start_i:] / c.iloc[start_i] * (1 - 0.0013)
    rows, curves = {}, {"买入持有": bh}

    for name, t in targets.items():
        eq, n, fee, w = simulate(t, px, start_i)
        rows[name] = metrics(eq, w)
        rows[name]["交易次数"] = n
        rows[name]["手续费"] = "%.2f%%" % (fee * 100)
        curves[name] = eq
    rows["买入持有"] = metrics(bh, pd.Series(1.0, index=bh.index))
    rows["买入持有"]["交易次数"] = 1
    rows["买入持有"]["手续费"] = "0.13%"

    cols = ["总收益", "年化", "最大回撤", "Sharpe", "在场率", "交易次数", "手续费"]
    df = pd.DataFrame(rows).T[cols]
    df.index.name = "策略"
    print("回测窗口: %s → %s (%.1f 年)\n" % (start.date(), px.index[-1].date(),
                                             (px.index[-1] - start).days / 365.25))
    print(df.to_string())
    (DATA / "summary.csv").write_text(df.to_csv())
    for name, eq in curves.items():
        eq.to_csv(DATA / ("curve_%s.csv" % name.replace("/", "_").replace("(", "_").replace(")", "")),
                  header=["equity"])
    note = ("# P1 回测摘要 %s\n\n窗口: %s → %s | 成本: taker 0.1%% + 滑点 0.03%% | "
            "信号收盘计算/次日开盘成交/情绪因子滞后1天\n\n```\n%s\n```\n\n"
            "注意: 全样本内初步校准, 不构成样本外结论; walk-forward 在 P2。\n"
            % (pd.Timestamp.now(tz="UTC").strftime("%F"), start.date(), px.index[-1].date(), df.to_string()))
    (DATA / "report.md").write_text(note)
    print("\n已落盘 data/backtest/{summary.csv,report.md,curve_*.csv}")


if __name__ == "__main__":
    main()
