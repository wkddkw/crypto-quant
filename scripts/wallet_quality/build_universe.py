#!/usr/bin/env python3
"""Build the top-100 wallet universe without GMGN (spec §6 fallback A).

GMGN's public quote API sits behind Cloudflare, so leaderboard = self-built:
  1. discover hot Solana meme mints (Dexscreener search by 24h volume);
  2. largest token accounts of those mints (Helius getTokenLargestAccounts)
     resolved to OWNER wallets (getTokenAccountsInfo, batched);
  3. drop accounts appearing across too many mints (pools/LPs/MMs);
  4. score owners by 30d realized proxy = sum(sell_usd) - sum(buy_usd)
     over their own enhanced-tx history (mirror of derive_buys logic);
  5. rank desc -> universe.json [{address, rank, realized_proxy_usd,
     exposed:false, source:"helius_fallback_A"}].

When a GMGN readonly contract gets configured, GMGN ranks replace this;
keep this as the fallback and shadow-extension seeding path.

Usage: python3 build_universe.py [--dry]
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone

from wq_common import (CONFIG, DATA, ensure_dirs, http_get_json,
                       http_post_json, is_stable_or_wsol, pseudo, require_key)

DAY = 86400


def hot_mints(count, key):
    d = http_get_json(f"{CONFIG['dexscreener_url']}/latest/dex/search",
                      params={"q": "solana"}, bucket="dexscreener", retries=2)
    pairs = [p for p in (d.get("pairs") or []) if p.get("chainId") == "solana"]
    pairs.sort(key=lambda p: ((p.get("volume") or {}).get("h24") or 0), reverse=True)
    seen = []
    for p in pairs:
        mint = (p.get("baseToken") or {}).get("address")
        if mint and not is_stable_or_wsol(mint) and mint not in seen:
            seen.append(mint)
        if len(seen) >= count:
            break
    return seen


def largest_token_accounts(mint, key, limit=20):
    r = http_post_json(f"{CONFIG['rpc_url']}?api-key={key}",
                       {"jsonrpc": "2.0", "id": 1,
                        "method": "getTokenLargestAccounts", "params": [mint]})
    out = []
    for acc in ((r.get("result") or {}).get("value") or [])[:limit]:
        addr = acc.get("address")
        try:
            amt = float(acc.get("uiAmountString") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if addr and amt > 0:
            out.append(addr)
    return out


def resolve_owners(token_accounts, key):
    """Batch getMultipleAccounts(jsonParsed) -> {token_account: owner}.

    getMultipleAccounts returns values positionally aligned with the input
    list (None for missing accounts). Token account parseds carry
    data.parsed.info.tokenAmount; mint accounts carry supply instead and
    are skipped.
    """
    owners = {}
    for i in range(0, len(token_accounts), 90):
        batch = token_accounts[i:i + 90]
        r = http_post_json(f"{CONFIG['rpc_url']}?api-key={key}",
                           {"jsonrpc": "2.0", "id": 1,
                            "method": "getMultipleAccounts",
                            "params": [batch, {"encoding": "jsonParsed"}]})
        values = (r.get("result") or {}).get("value") or []
        for addr, acc in zip(batch, values):
            if not acc:
                continue
            parsed = ((acc.get("data") or {}).get("parsed") or {}).get("info") or {}
            owner = parsed.get("owner")
            if owner and "tokenAmount" in parsed:
                owners[addr] = owner
    return owners


def wallet_value(tx, wallet):
    """(buy_usd, sell_usd): stable/SOL paid out vs received, gated on token flow."""
    paid_stable = paid_sol = got_stable = got_sol = 0.0
    sent_token = got_token = False
    for tf in tx.get("tokenTransfers") or []:
        mint = tf.get("mint") or ""
        try:
            amt = float(tf.get("tokenAmount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        src, dst = tf.get("fromUserAccount"), tf.get("toUserAccount")
        if src == wallet:
            if mint in CONFIG["stables"]:
                paid_stable += amt
            elif mint == CONFIG["wsol_mint"]:
                paid_sol += amt
            elif not is_stable_or_wsol(mint):
                sent_token = True
        if dst == wallet:
            if mint in CONFIG["stables"]:
                got_stable += amt
            elif mint == CONFIG["wsol_mint"]:
                got_sol += amt
            elif not is_stable_or_wsol(mint):
                got_token = True
    for nt in tx.get("nativeTransfers") or []:
        if nt.get("fromUserAccount") == wallet:
            paid_sol += (nt.get("amount") or 0) / 1e9
        if nt.get("toUserAccount") == wallet:
            got_sol += (nt.get("amount") or 0) / 1e9
    sol = float(CONFIG.get("sol_price_usd_fallback", 200.0))
    buy = (paid_stable + paid_sol * sol) if got_token else 0.0
    sell = (got_stable + got_sol * sol) if sent_token else 0.0
    return buy, sell


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mints", type=int, default=25)
    ap.add_argument("--pool", type=int, default=300, help="candidate wallets")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max-mint-share", type=int, default=0,
                    help="drop owners present in > this many mints (0 = auto)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    key = require_key()
    ensure_dirs()

    mints = hot_mints(args.mints, key)
    print(f"hot mints={len(mints)}")
    owner_mints = defaultdict(set)         # owner -> {mint}
    owner_amount = defaultdict(float)      # owner -> max share seen (unused v1)
    for mint in mints:
        accounts = largest_token_accounts(mint, key)
        owners = resolve_owners(accounts, key)
        for _ta, owner in owners.items():
            owner_mints[owner].add(mint)
    share_cap = args.max_mint_share or max(3, len(mints) // 3)
    candidates = sorted((o for o, ms in owner_mints.items() if len(ms) <= share_cap),
                        key=lambda o: -len(owner_mints[o]))[:args.pool]
    print(f"candidate owners={len(candidates)} (pool-filtered {len(owner_mints)})")
    if args.dry:
        for a in candidates[:10]:
            print(" ", pseudo(a), len(owner_mints[a]))
        return

    cutoff = datetime.now(timezone.utc).timestamp() - args.days * DAY
    scored = []
    for n, addr in enumerate(candidates, 1):
        rows = http_get_json(f"{CONFIG['enhanced_url']}/{addr}/transactions",
                             params={"api-key": key, "limit": 100},
                             bucket="helius")
        if not isinstance(rows, list):
            continue
        buys = sells = 0.0
        for tx in rows:
            if (tx.get("timestamp") or 0) < cutoff or tx.get("transactionError"):
                continue
            b, s = wallet_value(tx, addr)
            buys += b
            sells += s
        scored.append({"address": addr, "realized_proxy_usd": round(sells - buys, 2),
                       "buys_usd": round(buys, 2), "sells_usd": round(sells, 2)})
        if n % 25 == 0:
            print(f"  scored {n}/{len(candidates)}")
    scored.sort(key=lambda r: -r["realized_proxy_usd"])
    universe = [{"address": r["address"], "rank": i + 1,
                 "realized_proxy_usd": r["realized_proxy_usd"],
                 "exposed": False, "source": "helius_fallback_A"}
                for i, r in enumerate(scored[:args.top])]
    json.dump(universe, (DATA / "universe.json").open("w"), indent=1)
    print(f"universe.json written: {len(universe)} wallets, "
          f"top proxy={universe[0]['realized_proxy_usd'] if universe else 0}")


if __name__ == "__main__":
    main()
