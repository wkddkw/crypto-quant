#!/usr/bin/env python3
"""M3 (dedup clusters) + M3b (shadow-wallet discovery) over raw tx dumps.

Edges (union-find, spec M3):
  E1 funding  : stable or SOL value moved between two wallets directly,
                >= funding_edge_min_usd, counterparty not a CEX hot wallet.
                Direction-agnostic (inbound or outbound both link).
  E2 behavior : >= e2_min_cobuys of same mint within e2_window_sec.
  E3 holdings : >= e3_min_shared same non-major mints each received in txs.
  Shadow (M3b): E1 counterparties outside the universe become candidates
                -> shadow_candidates.jsonl, never unioned (they are not
                counted as independent cluster votes).

CEX hot-wallet heuristic: a counterparty that is feePayer across >=
cex_exclusion_min_txns distinct wallets' tx dumps, or listed in config,
never links and never becomes a shadow candidate.

Outputs (gitignored data/): clusters.csv, edges.csv, shadow_candidates.jsonl
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from wq_common import CONFIG, DATA, RAW, is_stable_or_wsol, pseudo


class UnionFind:
    def __init__(self, items=()):
        self.p = {x: x for x in items}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra

    def groups(self):
        out = defaultdict(list)
        for x in self.p:
            out[self.find(x)].append(x)
        return out


def _amt(tf):
    try:
        return float(tf.get("tokenAmount") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def scan(dumps, universe_map):
    """Return (e1_edges, cobuy, received, shadow, cex).

    e1_edges: list of (wallet_a_addr, wallet_b_addr, usd, ts, reason)
    cobuy:    mint -> sorted [(ts, wallet_sha)]
    received: wallet_sha -> set of non-major mints it ever received
    shadow:   addr -> {reason, first_seen, edge_usd}
    cex:      set of exchange-like counterparties (excluded everywhere)
    """
    m3 = CONFIG["m3"]
    # CEX heuristic: an exchange hot wallet is a counterparty to many
    # distinct universe wallets (feePayer is the sender, usually not the CEX).
    counterparty_wallets = defaultdict(set)   # addr -> set of universe shas seen with it
    for sha, txs in dumps.items():
        wallet = universe_map.get(sha)
        for tx in txs:
            for tf in tx.get("tokenTransfers") or []:
                for other in (tf.get("fromUserAccount"), tf.get("toUserAccount")):
                    if other and other != wallet:
                        counterparty_wallets[other].add(sha)

    cex = set(m3.get("cex_list") or [])
    for addr, universes in counterparty_wallets.items():
        if len(universes) >= m3["cex_exclusion_min_wallets"]:
            cex.add(addr)

    tx_list = [(sha, tx) for sha, txs in dumps.items() for tx in txs]

    e1 = []
    cobuy = defaultdict(list)
    received = defaultdict(set)
    shadow = {}

    def note_shadow(addr, reason, ts, usd):
        cur = shadow.get(addr)
        if cur is None:
            shadow[addr] = {"reason": reason, "first_seen": ts, "edge_usd": usd}
        else:
            cur["edge_usd"] = max(cur["edge_usd"], usd)
            if ts and (not cur["first_seen"] or ts < cur["first_seen"]):
                cur["first_seen"] = ts

    for sha, tx in tx_list:
        wallet = universe_map.get(sha)
        ts = tx.get("timestamp") or 0
        if not wallet:
            continue
        for tf in tx.get("tokenTransfers") or []:
            mint = tf.get("mint")
            src = tf.get("fromUserAccount")
            dst = tf.get("toUserAccount")
            amt = _amt(tf)
            # E1 stable value movement between universe wallet and counterparty
            if mint in CONFIG["stables"] and amt >= m3["funding_edge_min_usd"]:
                other = None
                if src == wallet and dst:
                    other = dst
                elif dst == wallet and src:
                    other = src
                if other and other != wallet and other not in cex:
                    e1.append((wallet, other, amt, ts, "stable"))
                    if other not in universe_map.values():
                        note_shadow(other, "stable_" +
                                    ("out" if src == wallet else "in"), ts, amt)
            # E2/E3 inputs: universe wallet received a non-major token
            if dst == wallet and mint and not is_stable_or_wsol(mint):
                cobuy[mint].append((ts, sha))
                received[sha].add(mint)
        # E1 native SOL movement (lamports -> fallback USD)
        sol_usd = float(CONFIG.get("sol_price_usd_fallback", 200.0))
        for nt in tx.get("nativeTransfers") or []:
            amt_usd = (nt.get("amount") or 0) / 1e9 * sol_usd
            if amt_usd < m3["funding_edge_min_usd"]:
                continue
            src, dst = nt.get("fromUserAccount"), nt.get("toUserAccount")
            other = dst if src == wallet else (src if dst == wallet else None)
            if other and other != wallet and other not in cex:
                e1.append((wallet, other, amt_usd, ts, "sol"))
                if other not in universe_map.values():
                    note_shadow(other, "sol_" + ("out" if src == wallet else "in"),
                                ts, amt_usd)

    for mint in cobuy:
        cobuy[mint].sort()
    return e1, cobuy, received, shadow, cex


def build_edges(e1, cobuy, received, universe_map):
    """Union-find over universe wallets only; returns (uf, edge_rows)."""
    m3 = CONFIG["m3"]
    sha_of = {addr: pseudo(addr) for addr in universe_map.values()}
    uf = UnionFind(sha_of.values())
    edge_rows = []

    for a_addr, b_addr, usd, ts, kind in e1:
        if b_addr in sha_of:
            a_sha, b_sha = sorted((sha_of[a_addr], sha_of[b_addr]))
            uf.union(a_sha, b_sha)
            edge_rows.append((a_sha, b_sha, "E1", kind, round(usd, 2), ts))

    pair_co = Counter()
    for mint, rows in cobuy.items():
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                if rows[j][0] - rows[i][0] > m3["e2_window_sec"]:
                    break
                if rows[i][1] != rows[j][1]:
                    pair_co[tuple(sorted((rows[i][1], rows[j][1])))] += 1
    for (a, b), count in pair_co.items():
        if count >= m3["e2_min_cobuys"]:
            uf.union(a, b)
            edge_rows.append((a, b, "E2", "cobuy", count, 0))

    pair_shared = Counter()
    holders = defaultdict(set)
    for sha, owned in received.items():
        for mint in owned:
            holders[mint].add(sha)
    for mint, hs in holders.items():
        if len(hs) < 2:
            continue
        hs = sorted(hs)
        for i in range(len(hs)):
            for j in range(i + 1, len(hs)):
                pair_shared[(hs[i], hs[j])] += 1
    for (a, b), shared in pair_shared.items():
        if shared >= m3["e3_min_shared"]:
            uf.union(a, b)
            edge_rows.append((a, b, "E3", "holdings", shared, 0))

    return uf, edge_rows, sha_of


def main():
    universe = json.loads((DATA / "universe.json").read_text())
    universe_map = {pseudo(r["address"]): r["address"] for r in universe}
    rank_of = {pseudo(r["address"]): r.get("rank") for r in universe}

    dumps = {}
    for path in sorted(RAW.glob("txs_*.json")):
        dumps[path.stem.split("_", 1)[1]] = json.loads(path.read_text()).get("transactions") or []

    e1, cobuy, received, shadow, cex = scan(dumps, universe_map)
    uf, edge_rows, sha_of = build_edges(e1, cobuy, received, universe_map)

    groups = uf.groups()
    rows = []
    for root, members in groups.items():
        for m in members:
            rows.append({"wallet_sha": m, "cluster": root, "cluster_size": len(members),
                         "rank_at_capture": rank_of.get(m, "")})
    _write_csv(DATA / "clusters.csv",
               sorted(rows, key=lambda r: (-r["cluster_size"], r["wallet_sha"])),
               ["wallet_sha", "cluster", "cluster_size", "rank_at_capture"])
    _write_csv(DATA / "edges.csv", edge_rows,
               ["a_sha", "b_sha", "edge_type", "kind", "metric", "ts"])

    now = int(datetime.now(timezone.utc).timestamp())
    cands = []
    for addr, info in sorted(shadow.items(), key=lambda kv: -kv[1]["edge_usd"]):
        if addr in sha_of:
            continue
        cands.append({"wallet_sha": pseudo(addr), "reason": info["reason"],
                      "first_seen": info["first_seen"],
                      "edge_usd": round(info["edge_usd"], 2),
                      "exposed": False, "discovered_at": now})
    with (DATA / "shadow_candidates.jsonl").open("w", encoding="utf-8") as handle:
        for r in cands:
            handle.write(json.dumps(r) + "\n")

    n_universe = len(universe_map)
    independent = len([g for g in groups.values()])
    print(f"universe={n_universe} clusters={independent} "
          f"independent_ratio={independent/max(n_universe,1):.2f} "
          f"edges={len(edge_rows)} shadow={len(cands)} cex_excluded={len(cex)}")


def _write_csv(path, rows, cols):
    with path.open("w", encoding="utf-8") as handle:
        handle.write(",".join(cols) + "\n")
        for r in rows:
            handle.write(",".join(str(r.get(c, "")) for c in cols) + "\n")


if __name__ == "__main__":
    main()
