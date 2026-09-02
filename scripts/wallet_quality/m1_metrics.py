#!/usr/bin/env python3
"""M1 per-buy return distribution + M2 reachable-delay discount.

Horizons are measured against the mint's implied price curve built from
ALL wallet fills in buys.jsonl (entry curve is a proxy until
collect_price_tracks.py provides GeckoTerminal candles; when a
raw/price_{mint}.json exists it is used instead — exact mode).

  ret_h        = price(t0+h)/price(t0) - 1 - cost
  ret_h_delayed= price(max(t0+entry_delay, t0)+h)/price(max(t0+entry_delay,t0)) - 1 - cost
      (t0+entry_delay is the M2 "reachable" entry: signal visible + slippage budget)

Stratification per spec: rank strata, size strata (incl. the <$1.5k bucket
that feeds L2/opens the threshold for debate), per-wallet Sharpe-like.

Outputs: m1_events.csv.gz-equivalent (jsonl), m1_report.md, m1_stats.json
"""
import argparse
import json
import math
import statistics
from bisect import bisect_right
from collections import defaultdict

from wq_common import CONFIG, DATA, RAW, read_jsonl


def load_curves():
    """mint -> (ts_list, price_list, exact:bool). Exact = from GT candles.

    Fill-derived curves need >= min_curve_points trades to be usable; mints
    below that (one-shot meme buys) are dropped so they can't pollute the
    median with a fake "exactly-cost" return. Coverage is reported separately.
    """
    minpts = int(CONFIG.get("min_curve_points", 5))
    raw = defaultdict(list)
    for b in read_jsonl(DATA / "buys.jsonl"):
        if b.get("trade_usd") and b.get("token_amount"):
            raw[b["asset_mint"]].append(
                (b["executed_at"], b["trade_usd"] / b["token_amount"]))
    curves = {}
    for mint, pts in raw.items():
        pts.sort()
        if len(pts) >= minpts:
            curves[mint] = ([p[0] for p in pts], [p[1] for p in pts], False)
    for path in RAW.glob("price_*.json"):
        d = json.loads(path.read_text())
        if not d.get("candles"):
            continue
        ts = [c[0] for c in d["candles"]]
        cl = [c[4] for c in d["candles"]]
        curves[d["mint"]] = (ts, cl, True)   # candle close curve wins
    return curves


def price_at(curve, t):
    ts, pr, _ = curve
    i = bisect_right(ts, t) - 1
    return pr[i] if i >= 0 else None


def size_bucket(usd):
    strata = CONFIG["size_strata_usd"]
    if usd is None:
        return "unpriced"
    if usd < strata[0]:
        return "<1.5k"
    for lo, hi in zip(strata, strata[1:] + [float("inf")]):
        if lo <= usd < hi:
            return f"{lo/1000:g}-{hi/1000:g}k" if hi != float("inf") else f">{lo/1000:g}k"
    return "?"


def rank_bucket(rank):
    strata = CONFIG["rank_strata"]
    if rank is None:
        return "unknown"
    for lo, hi in zip([1] + strata, strata):
        if lo <= rank <= hi:
            return f"{lo}-{hi}"
    return f">{strata[-1]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args()
    curves = load_curves()
    cost = CONFIG["cost_roundtrip_pct"]
    delay = CONFIG["entry_delay_sec"]
    horizons = [h * 60 for h in CONFIG["horizons_min"]]

    rows = []
    dropped = {"no_curve": 0, "unpriced": 0}
    for b in read_jsonl(DATA / "buys.jsonl"):
        curve = curves.get(b["asset_mint"])
        t0 = b["executed_at"]
        if b.get("trade_usd") is None:
            dropped["unpriced"] += 1
            continue
        if not curve:
            dropped["no_curve"] += 1
            continue
        p0 = price_at(curve, t0)
        t_entry = t0 + delay
        pd_ = price_at(curve, t_entry)
        row = {"wallet_sha": b["wallet_sha"], "mint": b["asset_mint"][:10],
               "ts": t0, "trade_usd": b.get("trade_usd"),
               "usd_method": b.get("usd_method"),
               "rank_bucket": rank_bucket(b.get("rank_at_capture")),
               "size_bucket": size_bucket(b.get("trade_usd")),
               "exact_curve": curve[2], "ret": {}}
        for h in horizons:
            tag = f"{h//60}m" if h < 3600 else f"{h//3600}h"
            for mode, base, t in (("raw", p0, t0), ("delayed", pd_, t_entry)):
                ph = price_at(curve, t + h) if base else None
                v = (ph / base - 1 - cost) if (ph and base) else None
                row["ret"][f"{tag}_{mode}"] = round(v, 5) if v is not None else None
        rows.append(row)

    stats = {"n": len(rows), "exact_share": round(
        sum(1 for r in rows if r["exact_curve"]) / max(len(rows), 1), 3),
        "coverage": {"usable": len(rows), **dropped}}
    for tag_key, field in (("overall", None), ("rank", "rank_bucket"),
                           ("size", "size_bucket")):
        stats[tag_key] = {}
        for r in rows:
            key = r[field] if field else "all"
            stats[tag_key].setdefault(key, []).append(r)
        for key, group in stats[tag_key].items():
            per = {}
            for hk in ("1m_delayed", "10m_delayed", "1h_delayed", "24h_delayed",
                       "24h_raw"):
                vals = [r["ret"].get(hk) for r in group if r["ret"].get(hk) is not None]
                if len(vals) < 5:
                    continue
                med = statistics.median(vals)
                win = sum(1 for v in vals if v > 0) / len(vals)
                sd = statistics.pstdev(vals) or 1e-9
                p90 = sorted(vals)[int(len(vals) * 0.1)]
                per[hk] = {"n": len(vals), "median": round(med, 4),
                           "winrate": round(win, 3),
                           "sharpe_like": round(statistics.mean(vals) / sd, 3),
                           "p90_loss": round(p90, 4)}
            stats[tag_key][key] = per

    with (DATA / "m1_events.jsonl").open("w", encoding="utf-8") as h:
        for r in rows:
            h.write(json.dumps(r) + "\n")
    json.dump(stats, (DATA / "m1_stats.json").open("w"), indent=1)

    if not args.no_report:
        lines = ["# M1/M2 wallet-quality return distribution", "",
                 f"events={stats['n']} exact_curve_share={stats['exact_share']}",
                 f"coverage: usable={stats['coverage']['usable']} "
                 f"dropped_no_curve={stats['coverage']['no_curve']} "
                 f"dropped_unpriced={stats['coverage']['unpriced']}",
                 "(usable requires >= "
                 f"{CONFIG.get('min_curve_points', 5)} fill points on the mint "
                 "or a GeckoTerminal candle track; sub-cost medians with high "
                 "dropped counts are NOT yet a strategy verdict — they mean "
                 "price coverage is insufficient)", "",
                 "| stratum | n@1h | med@1h | win@1h | med@24h(delayed) | p90@24h |",
                 "|---|---|---|---|---|---|"]
        for dim in ("overall", "rank", "size"):
            for key, per in sorted(stats[dim].items()):
                h1, h24 = per.get("1h_delayed", {}), per.get("24h_delayed", {})
                lines.append(f"| {dim}:{key} | {h1.get('n','-')} | "
                             f"{h1.get('median','-')} | {h1.get('winrate','-')} | "
                             f"{h24.get('median','-')} | {h24.get('p90_loss','-')} |")
        (DATA / "m1_report.md").write_text("\n".join(lines) + "\n")
    print(f"rows={stats['n']} dims={len(stats['rank'])+len(stats['size'])} "
          f"exact_share={stats['exact_share']}")


if __name__ == "__main__":
    main()
