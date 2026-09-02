#!/usr/bin/env python3
"""M2 price tracks via GeckoTerminal hourly OHLCV (free, no key).

Per (mint, pair): resolve the highest-liquidity pool through Dexscreener
tokens endpoint, then fetch hourly candles over [min_ts, max_ts] in pages.
Precision note: hour candles give t0/24h horizons well, but the 1/10-minute
horizons cannot be resolved here — simulate_confirmations.py's fill-derived
curves remain the v1 source for sub-hour points; this file upgrades M1/M2
at >= 1h and the entry/exit legs of M2 (entry at buy_ts + entry_delay_sec
snaps to the candle close containing that minute).

Direct routes (Helius, and GT/Dexscreener on the Grok node) need no proxy;
on the local Mac run with CRYPTO_QUANT_NO_PROXY unset so Dexscreener goes
through 127.0.0.1:10808 as probed.

Output: raw/price_{mint[:12]}.json  {mint, pair, candles:[[ts,o,h,l,c,v]], ...}
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone

from wq_common import CONFIG, DATA, RAW, ensure_dirs, http_get_json, read_jsonl

PAGE_HOURS = 999  # GT v2 hourly cap


def resolve_pair(mint):
    d = http_get_json(f"{CONFIG['dexscreener_url']}/latest/dex/tokens/{mint}",
                      bucket="dexscreener", retries=2)
    pairs = [p for p in (d.get("pairs") or []) if p.get("chainId") == "solana"]
    if not pairs:
        return None
    pairs.sort(key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    return pairs[0].get("pairAddress")


def fetch_ohlcv(pair, start_ts, end_ts):
    url = (f"{CONFIG['geckoterminals_url']}/networks/solana/pools/{pair}"
           f"/ohlcv/hour")
    candles, start = [], start_ts
    while start < end_ts:
        stop = min(start + PAGE_HOURS * 3600, end_ts)
        d = http_get_json(url, params={"aggregate": 1, "before_timestamp": stop,
                                       "limit": PAGE_HOURS},
                          bucket="dexscreener", retries=2)
        batch = (d.get("attributes") or {}).get("ohlcv_list") or []
        if not batch:
            break
        batch.sort(key=lambda c: c[0])
        candles = [c for c in batch if c[0] >= start] + candles
        oldest = batch[0][0]
        if oldest <= start:
            break
        start = oldest
    dedup = {}
    for c in candles:
        dedup[c[0]] = c
    return sorted(dedup.values(), key=lambda c: c[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mints", type=int, default=0, help="cap mints (smoke)")
    args = ap.parse_args()
    ensure_dirs()

    buys = read_jsonl(DATA / "buys.jsonl")
    spans = defaultdict(lambda: [None, None])
    for b in buys:
        ts = b["executed_at"]
        lo, hi = spans[b["asset_mint"]]
        spans[b["asset_mint"]] = [(min(lo, ts) if lo else ts),
                                  (max(hi, ts + 86400 + CONFIG["entry_delay_sec"])
                                   if hi else ts + 86400)]
    mints = sorted(spans, key=lambda m: -1)
    if args.mints:
        mints = mints[:args.mints]
    resolved = failed = 0
    for mint in mints:
        out = RAW / f"price_{mint[:12]}.json"
        if out.exists():
            resolved += 1
            continue
        lo, hi = spans[mint]
        try:
            pair = resolve_pair(mint)
            candles = fetch_ohlcv(pair, lo, hi) if pair else []
        except RuntimeError as exc:
            # GT may be unreachable from some egresses; keep going, the
            # analysis falls back to fill-derived curves for this mint.
            print(f"skip {mint[:10]}: {str(exc)[:120]}")
            candles = []
        if not candles:
            failed += 1
            continue
        json.dump({"mint": mint, "pair": pair,
                   "fetched_at": int(datetime.now(timezone.utc).timestamp()),
                   "candles": candles}, out.open("w"))
        resolved += 1
    print(f"mints={len(mints)} resolved={resolved} failed={failed}")


if __name__ == "__main__":
    main()
