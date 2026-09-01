#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 因子验证: walk-forward 选择测试 / ETH 跨币种 / 情绪极值减震器 / 降频降费。

对应 DESIGN.md §15 P2 议程。全部沿用 backtest.py 口径(成本 0.1%+滑点, 信号次日开盘成交,
情绪因子滞后 1 天)。输出: 控制台表格 + data/backtest/p2_report.md
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as bt  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


# ---------- 通用工具 ----------

def ann_sharpe(eq):
    r = eq.pct_change().dropna()
    if len(r) < 20 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * np.sqrt(365))


def window_metrics(eq):
    r = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    mdd = (eq / eq.cummax() - 1).min()
    sh = ann_sharpe(eq)
    return cagr * 100, mdd * 100, sh


def trend_signal(px):
    c, h = px["close"], px["high"]
    ma50 = c.rolling(50).mean()
    ma200 = c.rolling(200).mean()
    trend = pd.Series(np.where(c > ma200, 2.0, -2.0) + np.where(c > ma50, 1.0, -1.0), index=c.index)
    ret90 = c.pct_change(90)
    vol90 = np.log(c).diff().rolling(90).std() * np.sqrt(365)
    mom = (ret90 / vol90 * 2.0).clip(-2, 2)
    donch = (c >= h.rolling(20).max().shift(1)).astype(float)
    return (trend + mom + donch).clip(-6, 6) / 6.0


# ---------- 1) walk-forward: 每折用训练窗 Sharpe 选策略, 看验证年表现 ----------

def walk_forward():
    px, targets = bt.build_targets()
    curves = {"trend_only": bt.simulate(targets["v0_trend_only(无情绪)"], px, 200)[0],
              "ma200": bt.simulate(targets["ma200择时"], px, 200)[0]}
    c = px["close"]
    bh = c.iloc[200:] / c.iloc[200] * (1 - 0.0013)
    bh.index = px.index[200:]
    curves["buy_hold"] = bh

    rows, oos = [], []
    for y in range(2021, 2027):
        val0, val1 = pd.Timestamp("%d-01-01" % y, tz="UTC"), pd.Timestamp("%d-01-01" % (y + 1), tz="UTC")
        tr0 = val0 - pd.DateOffset(years=3)
        train_sh = {}
        for name, eq in curves.items():
            tr = eq[(eq.index >= tr0) & (eq.index < val0)]
            if len(tr) > 100:
                train_sh[name] = ann_sharpe(tr)
        if not train_sh:
            continue
        picked = max(train_sh, key=train_sh.get)
        val = curves[picked][(curves[picked].index >= val0) & (curves[picked].index < val1)]
        if len(val) < 50:
            continue
        cagr, mdd, sh = window_metrics(val)
        rows.append({"验证年": y, "训练窗最优": picked, "训练Sharpe": round(train_sh[picked], 2),
                     "验证年化%": round(cagr, 1), "验证回撤%": round(mdd, 1), "验证Sharpe": round(sh, 2)})
        oos.append((cagr, mdd, sh))

    df = pd.DataFrame(rows).set_index("验证年")
    agg = pd.DataFrame(oos, columns=["cagr", "mdd", "sh"])
    return df, agg


# ---------- 2) ETH 跨币种验证 ----------

def eth_check():
    px = pd.read_parquet(DATA / "okx_candles_ETH-USDT.parquet").sort_values("ts")
    px.index = pd.to_datetime(px.pop("ts"), unit="ms", utc=True)
    t = trend_signal(px)
    eq, n, fee, w = bt.simulate(t, px, 200)
    years = (px.index[-1] - px.index[200]).days / 365.25
    bh = px["close"].iloc[200:] / px["close"].iloc[200]
    per_year = []
    for y, g in eq.groupby(eq.index.year):
        r = g.pct_change().dropna()
        per_year.append("%d:%+.0f%%(S%.2f)" % (y, (g.iloc[-1] / g.iloc[0] - 1) * 100,
                                               ann_sharpe(g)))
    cagr, mdd, sh = window_metrics(eq)
    bh_cagr, bh_mdd, bh_sh = window_metrics(bh)
    # 与 BTC trend_only 日收益相关性
    btc_px, targets = bt.build_targets()
    btc_eq = bt.simulate(targets["v0_trend_only(无情绪)"], btc_px, 200)[0]
    j = pd.concat([eq.pct_change(), btc_eq.pct_change()], axis=1, join="inner").dropna()
    corr = float(j.iloc[:, 0].corr(j.iloc[:, 1]))
    return {"years": years, "cagr": cagr, "mdd": mdd, "sh": sh, "bh": (bh_cagr, bh_mdd, bh_sh),
            "per_year": per_year, "trades": n, "corr_btc": corr}


# ---------- 3) 情绪极值减震器变体 (BTC, 只压不加) ----------

def damper_variants():
    px, targets = bt.build_targets()
    base = targets["v0_trend_only(无情绪)"]

    fnd = pd.read_parquet(DATA / "deribit_funding_BTC-PERPETUAL.parquet").sort_values("ts")
    f = pd.Series(fnd["funding_rate"].values,
                  index=pd.to_datetime(fnd["ts"], unit="ms", utc=True)).resample("1D").sum()
    f_pct = f.rolling(90, min_periods=50).rank(pct=True)
    fg = pd.read_parquet(DATA / "fng.parquet").sort_values("ts")
    fg_v = pd.Series(fg["value"].values, index=pd.to_datetime(fg["ts"], unit="ms", utc=True))
    idx = px.index
    fp = f_pct.reindex(idx).ffill().shift(1)
    fv = fg_v.reindex(idx).ffill().shift(1)

    euph = ((fp >= 0.975) | (fv >= 90)).fillna(False)
    both = euph | ((fp <= 0.025) | (fv <= 10)).fillna(False)

    variants = {"base(无情绪)": base,
                "damp_euph(过热压半)": base.mask(euph, base * 0.5),
                "damp_both(双向极值压半)": base.mask(both, base * 0.5)}
    out = {}
    for name, t in variants.items():
        eq, n, fee, w = bt.simulate(t, px, 200)
        cagr, mdd, sh = window_metrics(eq)
        hit = int((euph if "euph" in name else both).sum())
        out[name] = {"cagr": cagr, "mdd": mdd, "sh": sh, "trades": n, "fee": fee * 100,
                     "extreme_days": hit}
    return out


# ---------- 4) 降频降费 (BTC trend_only) ----------

def fee_variants():
    px, targets = bt.build_targets()
    t = targets["v0_trend_only(无情绪)"]
    out = {}
    for label, th, wk in [("日频 TH=0.10", 0.10, False), ("日频 TH=0.15", 0.15, False),
                          ("日频 TH=0.20", 0.20, False), ("周频 TH=0.10", 0.10, True),
                          ("周频 TH=0.20", 0.20, True)]:
        bt.TH = th
        eq, n, fee, w = bt.simulate(t, px, 200, weekly=wk)
        cagr, mdd, sh = window_metrics(eq)
        out[label] = {"cagr": cagr, "mdd": mdd, "sh": sh, "trades": n, "fee": fee * 100}
    bt.TH = 0.10
    return out


def main():
    print("=" * 72)
    print("1) WALK-FORWARD 选择测试 (3年训练选Sharpe最优 → 次年验证)")
    wf, agg = walk_forward()
    print(wf.to_string())
    print("  选择出的策略 OOS 平均: 年化 %+.1f%% | 回撤 %.1f%% | Sharpe %.2f"
          % (agg["cagr"].mean(), agg["mdd"].mean(), agg["sh"].mean()))
    print("  各策略被选中次数:", dict(wf["训练窗最优"].value_counts()))

    print()
    print("=" * 72)
    eth = eth_check()
    print("2) ETH 跨币种验证 (同规则 trend_only, %.1f 年)" % eth["years"])
    print("  ETH trend_only: 年化 %+.1f%% | 回撤 %.1f%% | Sharpe %.2f | 交易 %d 次"
          % (eth["cagr"], eth["mdd"], eth["sh"], eth["trades"]))
    print("  ETH 买入持有:   年化 %+.1f%% | 回撤 %.1f%% | Sharpe %.2f" % eth["bh"])
    print("  分年:", " ".join(eth["per_year"]))
    print("  与 BTC trend_only 日收益相关性: %.2f" % eth["corr_btc"])

    print()
    print("=" * 72)
    print("3) 情绪极值减震器 (BTC trend_only + 极值压半仓, 永不加仓)")
    for name, m in damper_variants().items():
        print("  %-24s 年化 %+.1f%% | 回撤 %5.1f%% | Sharpe %.2f | 极值日 %d"
              % (name, m["cagr"], m["mdd"], m["sh"], m["extreme_days"]))

    print()
    print("=" * 72)
    print("4) 降频降费 (BTC trend_only)")
    for name, m in fee_variants().items():
        print("  %-14s 年化 %+.1f%% | 回撤 %5.1f%% | Sharpe %.2f | 交易 %4d 次 | 手续费 %.1f%%"
              % (name, m["cagr"], m["mdd"], m["sh"], m["trades"], m["fee"]))

    # 落盘报告
    lines = ["# P2 验证报告 %s\n" % pd.Timestamp.now(tz="UTC").strftime("%F"),
             "## 1. Walk-forward 选择测试\n", "```\n%s\n```" % wf.to_string(),
             "\nOOS 平均: 年化 %+.1f%% | 回撤 %.1f%% | Sharpe %.2f\n"
             % (agg["cagr"].mean(), agg["mdd"].mean(), agg["sh"].mean())]
    (DATA / "backtest" / "p2_report.md").write_text("\n".join(lines))
    print("\n报告已存 data/backtest/p2_report.md")


if __name__ == "__main__":
    main()
