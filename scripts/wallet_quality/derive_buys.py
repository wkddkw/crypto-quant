#!/usr/bin/env python3
"""Derive normalized buy events from raw enhanced-tx dumps (all sizes kept).

Input:  raw/txs_{sha}.json (+ universe.json for rank/exposed)
Output: buys.jsonl — one row per (wallet, mint, tx) buy, per spec §2:
      event_id wallet_sha executed_at asset_mint side trade_usd price_usd
      rank_at_capture usd_method

USD attribution (documented v1 limitation):
  - stable outflow (USDC/USDT) -> exact, usd_method="stable";
  - WSOL outflow priced by a config fallback constant -> usd_method="sol_fallback";
  - mixed txs sum both. Events without any priced outflow are kept with
    trade_usd=null so M1 can report coverage rather than silently drop them.
"""
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from wq_common import (CONFIG, DATA, RAW, is_stable_or_wsol, pseudo,
                       read_jsonl, append_jsonl)

SOL_PRICE_USD = float(CONFIG.get("sol_price_usd_fallback", 200.0))


def _dec(value):
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _pump_price_usd(amt):
    """Pump.fun bonding-curve spot price: p = amt^2 / 3e11 (v1, Raydium later)."""
    return (amt * amt) / 3e11 if amt else None


def buys_from_tx(tx, wallet, rank=None, exposed=None):
    """Return buy events where `wallet` received a non-stable token mint."""
    wsha = pseudo(wallet)
    events = []
    sig = tx.get("signature")
    ts = tx.get("timestamp") or 0
    transfers = tx.get("tokenTransfers") or []

    paid_stable = 0.0
    paid_sol = 0.0
    for tf in transfers:
        if tf.get("fromUserAccount") != wallet:
            continue
        mint = tf.get("mint")
        amt = _dec(tf.get("tokenAmount")) or 0.0
        if mint in CONFIG["stables"]:
            paid_stable += amt
        elif mint == CONFIG["wsol_mint"]:
            paid_sol += amt
    for nt in tx.get("nativeTransfers") or []:
        if nt.get("fromUserAccount") == wallet:
            paid_sol += (nt.get("amount") or 0) / 1e9

    received = {}
    for tf in transfers:
        if tf.get("toUserAccount") != wallet:
            continue
        mint = tf.get("mint")
        if not mint or is_stable_or_wsol(mint):
            continue
        received[mint] = received.get(mint, 0.0) + (_dec(tf.get("tokenAmount")) or 0.0)

    if not received:
        return events
    if paid_stable == 0 and paid_sol == 0:
        # wallet received tokens without paying anything in this tx:
        # inbound transfer / airdrop / payout, not a buy event (spec §2)
        return events

    usd = None
    method = None
    if paid_stable and not paid_sol:
        usd, method = paid_stable, "stable"
    elif paid_sol and not paid_stable:
        usd, method = paid_sol * SOL_PRICE_USD, "sol_fallback"
    elif paid_sol and paid_stable:
        usd, method = paid_stable + paid_sol * SOL_PRICE_USD, "mixed"

    for mint, amt in received.items():
        if usd is not None and len(received) > 1:
            # multi-mint txs: per-token attribution is unknowable from
            # transfers alone; keep the event but unpriced (honest null)
            share, method_row = None, "multi_mint"
        else:
            share = usd
            method_row = method
        events.append({
            "event_id": f"{sig}:{mint}",
            "wallet_sha": wsha,
            "executed_at": ts,
            "asset_mint": mint,
            "side": "buy",
            "token_amount": amt,
            "trade_usd": round(share, 2) if share is not None else None,
            "price_usd": _pump_price_usd(amt) if mint.endswith("pump") else None,
            "usd_method": method_row,
            "rank_at_capture": rank,
            "exposed": bool(exposed) if exposed is not None else None,
            "tx_type": tx.get("type"),
            "tx_source": tx.get("source"),
        })
    return events


def main():
    universe = {r["address"]: r for r in json.loads((DATA / "universe.json").read_text())}
    by_sha = {pseudo(a): (a, r) for a, r in universe.items()}
    out = DATA / "buys.jsonl"
    if out.exists():
        out.unlink()
    n = 0
    for path in sorted(RAW.glob("txs_*.json")):
        sha = path.stem.split("_", 1)[1]
        addr, meta = by_sha.get(sha, (None, {}))
        if not addr:
            continue
        dump = json.loads(path.read_text())
        txs = dump.get("transactions") or []
        events = []
        for tx in txs:
            if tx.get("transactionError"):
                continue
            events.extend(buys_from_tx(tx, addr, rank=meta.get("rank"),
                                       exposed=meta.get("exposed")))
        append_jsonl(out, events)
        n += len(events)
    print(f"buys={n} wallets={len(by_sha)} -> {out}")


if __name__ == "__main__":
    main()
