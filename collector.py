#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-quant P0 数据采集器

数据源(全部免费公开接口, 无需 API key):
  - OKX 现货/永续 日K线 + 永续资金费率历史(走本机代理)
  - alternative.me 恐惧贪婪指数(走代理)
  - Coin Metrics community 链上指标 MVRV/活跃地址(直连, 不走代理)

用法:
  python collector.py full     # 全量重建(首次, 或怀疑数据有缺口时)
  python collector.py update   # 增量更新(日常, 挂 cron)
  python collector.py status   # 查看各数据集状态与缺口
"""
import json
import sys
import time
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CFG = json.loads((ROOT / "config.json").read_text())

PROXIES = {"http": CFG["proxy"], "https": CFG["proxy"]}
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
PAUSE = float(CFG.get("pause_sec", 0.18))
DAY_MS = 86_400_000
H8_MS = 28_800_000

OKX_CANDLES = "https://www.okx.com/api/v5/market/candles"
OKX_HIST = "https://www.okx.com/api/v5/market/history-candles"
OKX_FUNDING = "https://www.okx.com/api/v5/public/funding-rate-history"
DERIBIT_URL = "https://www.deribit.com/api/v2/public/get_funding_rate_history"
FNG_URL = "https://api.alternative.me/fng/"
CM_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"


def http_get(url, params=None, use_proxy=True):
    last = None
    for i in range(5):
        try:
            r = requests.get(url, params=params, timeout=25,
                             proxies=PROXIES if use_proxy else None,
                             headers=HEADERS)
            if r.status_code == 200:
                return r
            last = "HTTP %s %s" % (r.status_code, r.text[:150])
        except requests.RequestException as e:
            last = repr(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError("GET %s failed: %s" % (url, last))


def okx_get(url, params):
    j = http_get(url, params).json()
    if str(j.get("code")) != "0":
        raise RuntimeError("OKX %s: %s (%s)" % (j.get("code"), j.get("msg"), params))
    return j["data"]


def load(name):
    p = DATA / name
    return pd.read_parquet(p) if p.exists() else None


def save(name, df):
    df = df.sort_values("ts").drop_duplicates(subset="ts").reset_index(drop=True)
    df.to_parquet(DATA / name, index=False)
    return df


# ---------- OKX K线 ----------

def fetch_candles(inst, full):
    """返回 DataFrame(ts, inst, open, high, low, close, vol); 丢弃未收盘K线。"""
    rows = {}
    if full:
        page = okx_get(OKX_CANDLES, {"instId": inst, "bar": "1Dutc", "limit": 300})
        for r in page:
            rows[int(r[0])] = r
        prev_min = min(rows)
        stall = 0
        while True:
            page = okx_get(OKX_HIST, {"instId": inst, "bar": "1Dutc",
                                      "limit": 100, "after": prev_min - 1})
            if not page:
                break
            for r in page:
                rows[int(r[0])] = r
            m = min(rows)
            if m >= prev_min:      # 翻页无进展, 防死循环
                stall += 1
                if stall >= 2:
                    break
            else:
                stall = 0
                prev_min = m
            if len(page) < 100:
                break
            time.sleep(PAUSE)
    else:
        after = None
        for _ in range(2):         # 增量拉最近 ~600 根, 覆盖日常更新绰绰有余
            params = {"instId": inst, "bar": "1Dutc", "limit": 300}
            url = OKX_CANDLES
            if after is not None:
                params["after"] = after - 1
                url = OKX_HIST
            page = okx_get(url, params)
            if not page:
                break
            for r in page:
                rows[int(r[0])] = r
            after = min(rows)
            time.sleep(PAUSE)
    recs = []
    for ts, r in sorted(rows.items()):
        if len(r) > 8 and str(r[8]) == "0":
            continue
        recs.append({"ts": ts, "inst": inst,
                     "open": float(r[1]), "high": float(r[2]),
                     "low": float(r[3]), "close": float(r[4]), "vol": float(r[5])})
    return pd.DataFrame(recs)


# ---------- OKX 资金费率 ----------

def fetch_funding(inst, full, have_max=None):
    """返回 DataFrame(ts, inst, funding_rate); funding 每 8h 一条。"""
    out = {}
    if full:
        page = okx_get(OKX_FUNDING, {"instId": inst, "limit": 100})
        for r in page:
            out[str(r["fundingTime"])] = r
        if len(page) >= 100:
            oldest = min(int(t) for t in out)
            # 运行时探测翻页方向, 不依赖文档记忆
            direction = None
            pb = okx_get(OKX_FUNDING, {"instId": inst, "limit": 5, "before": oldest})
            if pb and min(int(r["fundingTime"]) for r in pb) < oldest:
                direction = "before"
            else:
                pa = okx_get(OKX_FUNDING, {"instId": inst, "limit": 5, "after": oldest})
                if pa and min(int(r["fundingTime"]) for r in pa) < oldest:
                    direction = "after"
            if direction:
                cursor = oldest
                while True:
                    page = okx_get(OKX_FUNDING,
                                   {"instId": inst, "limit": 100, direction: cursor})
                    if not page:
                        break
                    for r in page:
                        out[str(r["fundingTime"])] = r
                    m = min(int(r["fundingTime"]) for r in page)
                    if m >= cursor:
                        break
                    cursor = m
                    if len(page) < 100:
                        break
                    time.sleep(PAUSE)
    else:
        cursor = int(have_max)
        for _ in range(400):
            page = okx_get(OKX_FUNDING, {"instId": inst, "limit": 100, "after": cursor})
            if not page:
                break
            for r in page:
                out[str(r["fundingTime"])] = r
            pmin = min(int(r["fundingTime"]) for r in page)
            pmax = max(int(r["fundingTime"]) for r in page)
            if pmin <= cursor or len(page) < 100:
                break
            cursor = pmax
            time.sleep(PAUSE)
    recs = [{"ts": int(t), "inst": inst, "funding_rate": float(r["fundingRate"]),
             "src": "okx"} for t, r in out.items()]
    return pd.DataFrame(recs)


# ---------- Deribit 深历史资金费率(OKX funding 接口只给最近 ~3 个月) ----------
# Deribit 永续每小时结算, interest_1h 为小时费率; 聚合到 8h 桶, 与 OKX funding 同构。
# 注: 不同交易所 funding 数值有微小差异, 研究层把 deribit/okx 当作同因子不同代理源。
# 实测接口行为: 给定窗口返回其中最新的一段(≤~750条) → 从 now 向旧线性回溯翻页。

def fetch_deribit_funding(inst, have_max=None):
    out = {}
    now = int(time.time() * 1000)
    start = 0 if have_max is None else int(have_max) + 1
    end = now
    empty_streak = 0
    calls = 0
    while start < end:
        calls += 1
        j = http_get(DERIBIT_URL, {"instrument_name": inst, "count": 1000,
                                   "start_timestamp": start, "end_timestamp": end}).json()
        res = (j.get("result") or [])
        if not res:
            empty_streak += 1
            if empty_streak >= 2:
                break
            end = (start + end) // 2     # 空窗口: 二分逼近数据起点
            continue
        empty_streak = 0
        for r in res:
            t = int(r["timestamp"])
            if t not in out:
                v = r.get("interest_1h")
                out[t] = float(v) if v is not None else 0.0
        pmin = min(int(r["timestamp"]) for r in res)
        if pmin <= start:                # 已覆盖到窗口起点
            break
        if end - pmin <= 0:              # 无进展, 防死循环
            break
        end = pmin - 1
        if calls % 20 == 0:
            print("  [deribit] %s: %d 条, 已翻到 %s" % (
                inst, len(out),
                dt.datetime.fromtimestamp(pmin / 1000, dt.timezone.utc).strftime("%F")),
                flush=True)
        time.sleep(PAUSE)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(sorted(out.items()), columns=["ts", "h1"])
    df["bucket"] = (df["ts"] // H8_MS) * H8_MS
    agg = df.groupby("bucket", as_index=False)["h1"].sum()
    agg = agg.rename(columns={"bucket": "ts", "h1": "funding_rate"})
    agg = agg[agg["ts"] < (now // H8_MS) * H8_MS]   # 丢弃未收满的当前 8h 桶
    agg["inst"] = inst
    agg["src"] = "deribit"
    return agg[["ts", "inst", "funding_rate", "src"]]


# ---------- 恐惧贪婪指数 ----------

def fetch_fng():
    j = http_get(FNG_URL, {"limit": 0}).json()
    recs = [{"ts": int(r["timestamp"]) * 1000, "value": int(r["value"]),
             "label": r["value_classification"]} for r in j["data"]]
    df = pd.DataFrame(recs)
    utc0 = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    df = df[df["ts"] < int(utc0.timestamp() * 1000)]   # 丢掉当天未定型读数
    return df


# ---------- Coin Metrics 链上指标 ----------

def fetch_cm(asset):
    rows, params = [], {"assets": asset, "metrics": "CapMVRVCur,AdrActCnt",
                        "frequency": "1d", "page_size": 1000}
    while True:
        j = http_get(CM_URL, params, use_proxy=False).json()
        rows += j.get("data", [])
        tok = j.get("next_page_token")
        if not tok:
            break
        params["next_page_token"] = tok
        time.sleep(PAUSE)
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["time"], utc=True).astype("int64") // 10 ** 6
    df = df.rename(columns={"CapMVRVCur": "mvrv", "AdrActCnt": "adr_act"})
    df["mvrv"] = pd.to_numeric(df["mvrv"], errors="coerce")
    df["adr_act"] = pd.to_numeric(df["adr_act"], errors="coerce")
    return df[["ts", "asset", "mvrv", "adr_act"]]


# ---------- 状态与主流程 ----------

def status():
    lines = ["# 数据状态 %s (UTC)" % dt.datetime.now(dt.timezone.utc).strftime("%F %T"), ""]
    for p in sorted(DATA.glob("*.parquet")):
        df = pd.read_parquet(p)
        step = H8_MS if "funding" in p.name else DAY_MS
        u = np.sort(pd.unique(df["ts"]))
        gaps = int((np.diff(u) != step).sum())
        tmin = dt.datetime.fromtimestamp(df.ts.min() / 1000, dt.timezone.utc).strftime("%F")
        tmax = dt.datetime.fromtimestamp(df.ts.max() / 1000, dt.timezone.utc).strftime("%F")
        lines.append("- **%s**: %d 行, %s → %s, 缺口 %d" % (p.name, len(df), tmin, tmax, gaps))
    text = "\n".join(lines)
    (DATA / "status.md").write_text(text + "\n")
    print(text)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "update"
    if cmd == "status":
        status()
        return
    errs = []

    def run(label, name, fn):
        try:
            old = load(name)
            df = fn(old)
            if df is None or len(df) == 0:
                raise RuntimeError("empty result")
            old_n = 0 if old is None else len(old)
            merged = save(name, df if old is None else pd.concat([old, df], ignore_index=True))
            print("[ok]   %-38s %6d 行 (新增 %d)" % (name, len(merged), len(merged) - old_n))
        except Exception as e:
            errs.append("%s: %r" % (label, e))
            print("[FAIL] %-38s %r" % (name, e))

    full = cmd == "full"
    for inst in CFG["spot"] + CFG["swap"]:
        name = "okx_candles_%s.parquet" % inst
        run(inst, name, lambda old, i=inst: fetch_candles(i, full or old is None))
    for inst in CFG["swap"]:
        name = "okx_funding_%s.parquet" % inst

        def fnd(old, i=inst):
            if full or old is None:
                return fetch_funding(i, True)
            return fetch_funding(i, False, have_max=int(old["ts"].max()))

        run(inst, name, fnd)
    for inst in CFG.get("deribit", []):
        name = "deribit_funding_%s.parquet" % inst

        def dbt(old, i=inst):
            have = None if (full or old is None) else int(old["ts"].max())
            return fetch_deribit_funding(i, have)

        run(inst, name, dbt)
    run("fng", "fng.parquet", lambda old: fetch_fng())
    for a in CFG["cm_assets"]:
        run(a, "cm_%s.parquet" % a, lambda old, x=a: fetch_cm(x))
    print()
    status()
    if errs:
        print("\n%d 个数据集失败, 其余已落库; 重新执行 full 可修复" % len(errs))
        for e in errs:
            print(" -", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
