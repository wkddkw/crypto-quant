#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""虚拟操盘: $500 模拟账户 + v0 打分制策略 (现货 long/flat, 只做 BTC)

严格按 DESIGN.md v0: 因子打分 → 信号 s ∈ [-1,1] → 目标仓位 [0,1](现货不许做空)。
成交按 OKX 现货 taker 0.1% + 滑点 0.03% 计。信号只用已收盘日线(防未来函数),
账户净值用实时 ticker 逐小时标记。

用法:
  python paper_trader.py run      # 跑一轮: 刷新信号 → 必要时调仓 → 记录 → 报告(默认)
  python paper_trader.py report   # 只出报告
  python paper_trader.py reset    # 清空账户重新开始(虚拟盘, 无需确认)

产物:
  data/paper/account.json     账户状态(现金/持仓/净值曲线/成交/决策)
  data/paper/report.md        最新报告
  data/paper/journal.md       决策日志(学习用)
  data/paper/predictions.csv  预测 vs 24h 实际涨跌(验证用)
"""
import csv
import json
import sys
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PAPER = DATA / "paper"
PAPER.mkdir(exist_ok=True)
ACCOUNT = PAPER / "account.json"

CFG = json.loads((ROOT / "config.json").read_text())
PROXIES = {"http": CFG["proxy"], "https": CFG["proxy"]}

INIT_CASH = 500.0
FEE = 0.001          # OKX 现货 taker
SLIP = 0.0003        # 滑点假设
REBAL_TH = 0.10      # 权重偏离 ≥10 个百分点才调仓, 防频繁摩擦
SCORE_MAX = 12.0     # 打分满分分母(趋势3+动量2+突破1+funding2+F&G2+MVRV2)
MIN_TRADE_NOTIONAL = 1.0


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def fmt_ts(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).strftime("%F %H:%M")


def live_price():
    j = requests.get("https://www.okx.com/api/v5/market/ticker",
                     params={"instId": "BTC-USDT"}, proxies=PROXIES, timeout=20).json()
    return float(j["data"][0]["last"])


def load_account():
    if ACCOUNT.exists():
        return json.loads(ACCOUNT.read_text())
    return {"cash": INIT_CASH, "btc": 0.0,
            "created": now_utc().strftime("%F %T"),
            "equity_history": [], "trades": [], "decisions": []}


def save_account(a):
    ACCOUNT.write_text(json.dumps(a, indent=1, ensure_ascii=False))


# ---------- v0 因子(与 DESIGN.md §3.1 一致, 规则写死便于复盘) ----------

def build_signal():
    df = pd.read_parquet(DATA / "okx_candles_BTC-USDT.parquet").sort_values("ts")
    c = df["close"].values
    price_close = float(c[-1])           # 最近已收盘日线
    ma50 = float(np.mean(c[-50:]))
    ma200 = float(np.mean(c[-200:]))

    trend = (2 if price_close > ma200 else -2) + (1 if price_close > ma50 else -1)

    ret90 = price_close / c[-90] - 1
    v90 = float(np.std(np.diff(np.log(c[-91:]))) * np.sqrt(365))
    mom = max(-2.0, min(2.0, (ret90 / v90) * 2)) if v90 > 0 else 0.0

    hh20 = float(df["high"].values[-21:-1].max())
    donch = 1.0 if price_close >= hh20 else 0.0

    fnd = pd.read_parquet(DATA / "deribit_funding_BTC-PERPETUAL.parquet").sort_values("ts")
    win = fnd[fnd["ts"] > int(fnd["ts"].max()) - 90 * 86_400_000]
    latest_f = float(fnd["funding_rate"].values[-1])
    f_pct = float((win["funding_rate"] < latest_f).mean())
    if f_pct >= 0.90:
        funding = -2.0
    elif f_pct >= 0.75:
        funding = -1.0
    elif f_pct <= 0.10:
        funding = 2.0
    elif f_pct <= 0.25:
        funding = 1.0
    else:
        funding = 0.0

    fg = int(pd.read_parquet(DATA / "fng.parquet").sort_values("ts")["value"].values[-1])
    fg_s = -2 if fg >= 80 else -1 if fg >= 70 else 2 if fg <= 20 else 1 if fg <= 30 else 0

    mv = pd.read_parquet(DATA / "cm_btc.parquet").sort_values("ts")["mvrv"].dropna()
    mvr = float(mv.values[-1])
    mv_s = -2 if mvr >= 2.5 else -1 if mvr >= 2.0 else 2 if mvr <= 0.8 else 1 if mvr <= 1.0 else 0

    total = trend + mom + donch + funding + fg_s + mv_s
    total_trend = trend + mom + donch
    s = max(-1.0, min(1.0, total / SCORE_MAX))
    return {
        "price_close": price_close, "ma50": ma50, "ma200": ma200,
        "factors": {"trend": trend, "mom90": round(mom, 2), "donchian20": donch,
                    "funding_pct": funding, "fng": fg_s, "mvrv": mv_s},
        "context": {"fng": fg, "mvrv": round(mvr, 3), "funding_8h": latest_f,
                    "funding_ann_pct": round(latest_f * 3 * 365 * 100, 1),
                    "funding_pctile": round(f_pct, 2)},
        "total": round(total, 2), "s": round(s, 3),
        "s_trend": round(max(-1.0, min(1.0, total_trend / 6.0)), 3),  # 对照信号(P1 回测结论的验证用)
        "target": round(max(0.0, s), 3),   # 现货 long/flat
        "ret_1d": round(price_close / c[-2] - 1, 4),
        "ret_7d": round(price_close / c[-8] - 1, 4),
        "ret_30d": round(price_close / c[-31] - 1, 4),
    }


# ---------- 预测验证(学习闭环) ----------

def record_prediction(s, price, s_trend=0.0):
    """每小时记一条预测; 已满 24h 的行用当前价结算命中率。"""
    pfile = PAPER / "predictions.csv"
    rows = []
    if pfile.exists():
        with open(pfile) as f:
            rows = list(csv.DictReader(f))
    for r in rows:
        r.setdefault("s_trend", "")
    now_ms = int(now_utc().timestamp() * 1000)
    # 结算到期预测
    n_hit = n_scored = 0
    for r in rows:
        if r["scored"] == "0" and now_ms - int(r["ts"]) >= 86_400_000:
            realized = price / float(r["price"]) - 1
            s0 = float(r["s"])
            pred = "up" if s0 >= 0.10 else "down" if s0 <= -0.10 else "flat"
            real = "up" if realized >= 0.003 else "down" if realized <= -0.003 else "flat"
            r["realized"] = round(realized, 4)
            r["pred"] = pred
            r["real_dir"] = real
            r["hit"] = "1" if (pred != "flat" and pred == real) else "0"
            r["scored"] = "1"
            n_scored += 1
            n_hit += int(r["hit"])
    # 新预测(30 分钟内不重复记)
    if not rows or now_ms - int(rows[-1]["ts"]) >= 1_800_000:
        rows.append({"ts": now_ms, "price": round(price, 2), "s": s, "s_trend": s_trend,
                     "scored": "0", "realized": "", "pred": "", "real_dir": "", "hit": ""})
    with open(pfile, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "price", "s", "s_trend", "scored",
                                          "realized", "pred", "real_dir", "hit"])
        w.writeheader()
        w.writerows(rows)
    scored_all = [r for r in rows if r["scored"] == "1"]
    dir_rows = [r for r in scored_all if r["pred"] != "flat"]
    hits = sum(int(r["hit"]) for r in dir_rows)
    return {"n_scored": n_scored, "n_hit": n_hit,
            "accuracy": (round(hits / len(dir_rows), 2) if dir_rows else None),
            "n_dir": len(dir_rows)}


# ---------- 主流程 ----------

def run_once():
    a = load_account()
    sig = build_signal()
    tick = live_price()
    equity = a["cash"] + a["btc"] * tick
    cur_w = (a["btc"] * tick / equity) if equity > 0 else 0.0
    tgt_w = sig["target"]

    action = "hold"
    if abs(tgt_w - cur_w) >= REBAL_TH:
        diff_val = tgt_w * equity - a["btc"] * tick
        side = "buy" if diff_val > 0 else "sell"
        qty = abs(diff_val) / tick
        px = tick * (1 + SLIP) if side == "buy" else tick * (1 - SLIP)
        if side == "buy":
            max_qty = a["cash"] / (px * (1 + FEE))
            qty = min(qty, max_qty)
        else:
            qty = min(qty, a["btc"])
        if qty * px >= MIN_TRADE_NOTIONAL:  # 忽略灰尘单，且不修改账本
            if side == "buy":
                a["cash"] -= qty * px * (1 + FEE)
                a["btc"] += qty
            else:
                a["cash"] += qty * px * (1 - FEE)
                a["btc"] -= qty
            action = side.upper()
            a["trades"].append({"ts": fmt_ts(int(now_utc().timestamp() * 1000)),
                                "side": side, "qty": round(qty, 6), "px": round(px, 2),
                                "usd": round(qty * px, 2), "signal": sig["s"]})
        else:
            action = "hold(dust)"

    ts_ms = int(now_utc().timestamp() * 1000)
    equity = a["cash"] + a["btc"] * tick
    decision = {"ts": fmt_ts(ts_ms), "s": sig["s"], "total": sig["total"],
                "target_w": tgt_w, "cur_w": round(cur_w, 3),
                "action": action, "price": round(tick, 2), "equity": round(equity, 2)}
    a["decisions"].append(decision)
    a["decisions"] = a["decisions"][-200:]
    a["equity_history"].append(decision)
    a["equity_history"] = a["equity_history"][-3000:]
    save_account(a)

    with open(PAPER / "journal.md", "a") as f:
        f.write("- **%s** | 信号 s=%+.2f (总分 %+.1f/12) → 目标仓位 %.0f%% | 价 %.2f | 动作 %s | 因子: %s\n"
                % (decision["ts"], sig["s"], sig["total"], tgt_w * 100, tick, action,
                   ", ".join("%s=%s" % kv for kv in sig["factors"].items())))

    val = record_prediction(sig["s"], tick, sig.get("s_trend", 0.0))
    write_report(a, sig, tick, val, action)
    return decision


def write_report(a, sig, tick, val, action):
    eq = a["cash"] + a["btc"] * tick
    cur_w = (a["btc"] * tick / eq) if eq > 0 else 0.0
    pnl = eq - INIT_CASH
    hist = a["equity_history"]
    day_ago = [h for h in hist if h["ts"] <= fmt_ts(int(now_utc().timestamp() * 1000) - 86_400_000)]
    since_open = (tick / hist[0]["price"] - 1) if hist else 0.0
    val_lines = []
    scored = []
    pf = PAPER / "predictions.csv"
    if pf.exists():
        with open(pf) as f:
            scored = [r for r in csv.DictReader(f) if r["scored"] == "1"]
    last3 = scored[-3:]
    acc = val["accuracy"]
    lines = [
        "# 虚拟操盘报告 %s (UTC)" % fmt_ts(int(now_utc().timestamp() * 1000)),
        "",
        "## 账户",
        "- 净值 **$%.2f** / 初始 $%.0f | 盈亏 **%+.2f (%+.2f%%)**" % (eq, INIT_CASH, pnl, pnl / INIT_CASH * 100),
        "- 现金 $%.2f | BTC %.6f (≈$%.2f, 权重 %.0f%%)" % (a["cash"], a["btc"], a["btc"] * tick, cur_w * 100),
        "- BTC 实时价 **%.2f** | 建户以来涨跌 %+.2f%%" % (tick, since_open * 100),
        "",
        "## 当前信号 (基于最近已收盘日线, 防未来函数)",
        "- 总分 **%+.1f / ±12** → s = %+.3f → 目标仓位 **%.0f%%** (本次动作: %s)"
        % (sig["total"], sig["s"], sig["target"] * 100, action),
        "- 因子: trend=%+d (价 %.0f vs MA50 %.0f / MA200 %.0f) | mom90=%+.2f | donchian20=%+.0f | 对照信号 s_trend=%+.2f"
        % (sig["factors"]["trend"], sig["price_close"], sig["ma50"], sig["ma200"],
           sig["factors"]["mom90"], sig["factors"]["donchian20"], sig.get("s_trend", 0)),
        "- 情绪: funding=%+.0f (当前 8h 费率年化 %.1f%%, 90日分位 %.0f%%) | F&G=%d → %+d | MVRV=%.2f → %+d"
        % (sig["factors"]["funding_pct"], sig["context"]["funding_ann_pct"],
           sig["context"]["funding_pctile"] * 100, sig["context"]["fng"],
           sig["factors"]["fng"], sig["context"]["mvrv"], sig["factors"]["mvrv"]),
        "- 行情: 日线 1d %+.2f%% / 7d %+.2f%% / 30d %+.2f%%"
        % (sig["ret_1d"] * 100, sig["ret_7d"] * 100, sig["ret_30d"] * 100),
        "",
        "## 验证(预测 vs 24h 实际)",
        "- 方向预测命中: %s (样本 %d 条, 排除中性; 每条需 24h 后才能结算)"
        % ("%.0f%%" % (acc * 100) if acc is not None else "待积累", val["n_dir"]),
    ]
    for r in last3:
        lines.append("  - %s: 预测 %s → 实际 %s (%+.2f%%) %s"
                     % (fmt_ts(int(r["ts"])), r["pred"] or "?", r["real_dir"],
                        float(r["realized"]) * 100 if r["realized"] else 0,
                        "✓" if r["hit"] == "1" else "✗"))
    lines.append("")
    lines.append("## 最近成交")
    if a["trades"]:
        for t in a["trades"][-5:]:
            lines.append("- %s %s %.6f BTC @ %.2f ($%.2f) | 信号 %+.2f"
                         % (t["ts"], t["side"].upper(), t["qty"], t["px"], t["usd"], t["signal"]))
    else:
        lines.append("- (尚无成交)")
    (PAPER / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "reset":
        ACCOUNT.unlink(missing_ok=True)
        for p in ("journal.md", "predictions.csv"):
            (PAPER / p).unlink(missing_ok=True)
        print("账户已重置")
        return
    if cmd == "report":
        a = load_account()
        sig = build_signal()
        tick = live_price()
        write_report(a, sig, tick, record_prediction(sig["s"], tick), "report-only")
        return
    run_once()


if __name__ == "__main__":
    main()
