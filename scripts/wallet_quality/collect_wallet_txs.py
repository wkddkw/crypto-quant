#!/usr/bin/env python3
"""Fetch full-history enhanced transactions for each universe wallet.

Input:  data/wallet_quality/universe.json  [{address, rank, exposed?}, ...]
Output: data/wallet_quality/raw/txs_{sha10}.json  (raw, gitignored, keys absent)

Read-only. Rate-limited via wq_common. Resumable: existing complete files
are skipped unless --force. Pagination walks `before` backwards until the
oldest timestamp passes --days or a page comes back empty.
"""
import argparse
import json
from datetime import datetime, timezone

from wq_common import (CONFIG, RAW, DATA, ensure_dirs, http_get_json,
                       pseudo, require_key)


def fetch_wallet(address, key, days, page_size, max_pages):
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    base = f"{CONFIG['enhanced_url']}/{address}/transactions"
    rows, before, complete = [], None, True
    for _ in range(max_pages):
        params = {"api-key": key, "limit": page_size}
        if before:
            params["before"] = before
        batch = http_get_json(base, params=params, bucket="helius")
        if not isinstance(batch, list):
            batch = batch.get("transactions") or []
        if not batch:
            break
        rows.extend(batch)
        oldest = min(t.get("timestamp") or 0 for t in batch)
        before = batch[-1]["signature"]
        if oldest < cutoff:
            break
    else:
        complete = False
    return rows, complete


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=CONFIG["ingest"]["days"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max wallets (smoke)")
    args = ap.parse_args()

    key = require_key()
    ensure_dirs()
    universe = json.loads((DATA / "universe.json").read_text())
    if args.limit:
        universe = universe[:args.limit]
    done = skipped = 0
    for row in universe:
        addr = row["address"]
        out = RAW / f"txs_{pseudo(addr)}.json"
        if out.exists() and not args.force:
            skipped += 1
            continue
        txs, complete = fetch_wallet(addr, key, args.days,
                                     CONFIG["ingest"]["page_size"],
                                     CONFIG["ingest"]["max_pages_per_wallet"])
        json.dump({"address_sha": pseudo(addr), "fetched_at": int(datetime.now(
            timezone.utc).timestamp()), "days": args.days, "complete": complete,
            "transactions": txs}, out.open("w"))
        done += 1
        print(f"{pseudo(addr)} rank={row.get('rank')} txs={len(txs)} complete={complete}")
    print(f"universe={len(universe)} fetched={done} skipped={skipped}")


if __name__ == "__main__":
    main()
