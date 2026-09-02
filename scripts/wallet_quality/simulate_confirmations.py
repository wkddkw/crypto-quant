#!/usr/bin/env python3
"""M4 / M4b / M4c replay over buys.jsonl + clusters.csv (spec §2b, §M4).

Confirmation model per (mint, anchor buy, window):
  For each wallet in the window, its confirmation is its max qualifying
  single buy; wallets map to clusters via clusters.csv — universe wallets
  share their cluster id, every other wallet is its own singleton (a shadow
  never ADDS votes beyond its cluster; same-cluster wallets count once).
  - L1 vote: cluster where the max qualifying buy >= l1_min_usd (unpriced
    buys never count).
  - L2 vote: any cluster with >= 1 buy, no amount floor.

Signal fires at the k-th distinct-cluster arrival when, over the full
arrival list (sorted first->last):
  (a) gap last-first >= min_gap_min*60                      [accumulation]
  (b) max share of arrivals in any 60s block <= max_share_60s [bundling]
  (c) with reject_burst: kth arrival - first >= 60s          [same-second volley]

Return model v1 (self-contained until M2 tracks land): implied price curve
per mint from wallet fills p = trade_usd/token_amount; entry at kth
arrival + entry_delay_sec, exit +24h, net of cost_roundtrip_pct.

Outputs: m4_summary.json (+ m4_grid.csv)
"""
import json
from bisect import bisect_right
from collections import Counter, defaultdict

from wq_common import CONFIG, DATA, read_jsonl


def load():
    buys = [b for b in read_jsonl(DATA / "buys.jsonl") if b.get("token_amount")]
    clusters = {}
    path = DATA / "clusters.csv"
    if path.exists():
        lines = path.open().read().strip().splitlines()
        header = lines[0].split(",")
        for line in lines[1:]:
            row = dict(zip(header, line.split(",")))
            clusters[row["wallet_sha"]] = row["cluster"]
    return buys, clusters


def spread_ok(arrivals):
    s = CONFIG["spread"]
    if len(arrivals) < 2:
        return False
    if (arrivals[-1] - arrivals[0]) < s["min_gap_min"] * 60:
        return False
    blocks = Counter(t // 60 for t in arrivals)
    return max(blocks.values()) / len(arrivals) <= s["max_share_60s"]


def main():
    buys, clusters = load()
    cost = CONFIG["cost_roundtrip_pct"]
    delay = CONFIG["entry_delay_sec"]
    exit_h = 24 * 3600
    l1_min = CONFIG["l1_min_usd"]
    reject_burst = CONFIG["spread"]["reject_burst"]

    by_mint = defaultdict(list)
    for b in buys:
        by_mint[b["asset_mint"]].append(b)

    grid = defaultdict(lambda: {"n": 0, "rets": [], "wins": 0, "sizes": []})
    notify_days = defaultdict(Counter)     # k_notify -> day -> count

    for mint, rows in by_mint.items():
        rows.sort(key=lambda r: r["executed_at"])
        curve_ts, curve_p = [], []
        for r in rows:
            if r.get("trade_usd"):
                curve_ts.append(r["executed_at"])
                curve_p.append(r["trade_usd"] / r["token_amount"])
        if len(curve_ts) < int(CONFIG.get("min_curve_points", 5)):
            continue  # thin fill curve: price model unreliable, skip mint
        for window in CONFIG["windows_min"]:
            wsec = window * 60
            for i, anchor in enumerate(rows):
                if i > 0 and rows[i]["executed_at"] - rows[i - 1]["executed_at"] < 1:
                    continue  # same-second anchors duplicate the window
                t0 = anchor["executed_at"]
                per_cluster = {}
                for r in rows[i:]:
                    if r["executed_at"] > t0 + wsec:
                        break
                    if not r.get("trade_usd"):
                        continue
                    cl = clusters.get(r["wallet_sha"], r["wallet_sha"])
                    info = per_cluster.setdefault(
                        cl, {"usd": None, "first_any": r["executed_at"],
                             "first_l1": None})
                    if info["usd"] is None or r["trade_usd"] > info["usd"]:
                        info["usd"] = r["trade_usd"]
                    if r["trade_usd"] >= l1_min and info["first_l1"] is None:
                        info["first_l1"] = r["executed_at"]
                if not per_cluster:
                    continue
                # L1 arrivals: first qualifying (>= threshold) buy per cluster
                l1_arr = sorted(info["first_l1"] for info in per_cluster.values()
                                if info["first_l1"] is not None)
                # L2 arrivals: first buy of any size per cluster (no floor)
                l2_arr = sorted(info["first_any"] for info in per_cluster.values())
                for tier, arr, ks in (("L1", l1_arr, CONFIG["k_l1"]),
                                      ("L2", l2_arr, CONFIG["k_notify"])):
                    if len(arr) == 0:
                        continue
                    if not spread_ok(arr):
                        continue
                    for k in ks:
                        if len(arr) < k:
                            continue
                        if reject_burst and arr[k - 1] - arr[0] < 60:
                            continue
                        fire = arr[k - 1]
                        j = bisect_right(curve_ts, fire) - 1
                        if j < 0:
                            continue
                        p_in_idx = bisect_right(curve_ts, fire + delay) - 1
                        p_out_idx = bisect_right(curve_ts, fire + delay + exit_h) - 1
                        if p_in_idx < 0 or p_out_idx < 0:
                            continue
                        net = curve_p[p_out_idx] / curve_p[p_in_idx] - 1 - cost
                        key = (tier, window, k)
                        g = grid[key]
                        first_key = "first_l1" if tier == "L1" else "first_any"
                        g["n"] += 1
                        g["rets"].append(net)
                        g["wins"] += 1 if net > 0 else 0
                        g["sizes"].append(sum(info["usd"] for info in
                                              per_cluster.values()
                                              if info[first_key] is not None
                                              and info[first_key] <= fire))
                        if tier == "L2":
                            notify_days[k][fire // 86400] += 1
    report(grid, notify_days)


def report(grid, notify_days):
    out = {"grid": {}, "m4b": {}, "m4c": {}}
    for (tier, w, k), g in sorted(grid.items()):
        n = g["n"]
        if n < 1:
            continue
        rets = sorted(g["rets"])
        med = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
        out["grid"][f"{tier}_w{w}_k{k}"] = {
            "n": n, "median_net": round(med, 4),
            "winrate": round(g["wins"] / n, 3),
            "mean_size_usd": round(sum(g["sizes"]) / n, 0)}
        # M4b: split cell at median total size, compare winrates
        pairs = sorted(zip(g["sizes"], g["rets"]))
        half = len(pairs) // 2
        if half >= 5:
            lo, hi = pairs[:half], pairs[half:]
            wr = lambda seg: sum(1 for _, r in seg if r > 0) / len(seg)
            out["m4b"][f"{tier}_w{w}_k{k}"] = {
                "lo_win": round(wr(lo), 3), "hi_win": round(wr(hi), 3),
                "gap_pp": round((wr(hi) - wr(lo)) * 100, 1)}
    for k, days in sorted(notify_days.items()):
        if days:
            span = max(days) - min(days) + 1
            out["m4c"][f"k{k}"] = {"alerts_per_day": round(
                sum(days.values()) / max(span, 1), 2), "active_days": len(days)}
    json.dump(out, (DATA / "m4_summary.json").open("w"), indent=1)
    with (DATA / "m4_grid.csv").open("w") as h:
        h.write("tier,window_min,k,n,median_net,winrate,mean_size_usd\n")
        for name, r in out["grid"].items():
            tier, w, k = name.split("_")[0], name.split("_w")[1].split("_")[0], \
                name.split("_k")[1]
            h.write(f"{tier},{w},{k},{r['n']},{r['median_net']},"
                    f"{r['winrate']},{r['mean_size_usd']}\n")
    print(f"cells={len(out['grid'])} m4b={len(out['m4b'])} m4c={len(out['m4c'])}")


if __name__ == "__main__":
    main()
