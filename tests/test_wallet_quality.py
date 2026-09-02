"""Pure-logic tests for the wallet_quality research pipeline.

No network, no Helius key required: derivation, clustering, and the
spread/replay rules are tested on synthetic transactions.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WQ = ROOT / "scripts" / "wallet_quality"
if str(WQ) not in sys.path:
    sys.path.insert(0, str(WQ))

os.environ.setdefault("WQ_PSEUDO_SALT", "test-salt")
os.environ.setdefault("HELIUS_API_KEY", "test-key-not-real")

import wq_common  # noqa: E402
import derive_buys  # noqa: E402
import cluster_analysis  # noqa: E402
import simulate_confirmations as sim  # noqa: E402


def _tf(mint, frm, to, amount):
    return {"mint": mint, "fromUserAccount": frm, "toUserAccount": to,
            "tokenAmount": amount, "tokenDecimal": 6}


USDC = wq_common.CONFIG["stables"][0]
WSOL = wq_common.CONFIG["wsol_mint"]
MINT = "TokenA11111111111111111111111111111111111pump"


class DeriveBuysTests(unittest.TestCase):
    def test_usdc_buy_becomes_usd_event(self):
        tx = {"signature": "s1", "timestamp": 100, "type": "SWAP",
              "source": "PUMP_FUN",
              "tokenTransfers": [_tf(USDC, "W1", "PoolX", 2000),
                                  _tf(MINT, "PoolX", "W1", 1_000_000)]}
        events = derive_buys.buys_from_tx(tx, "W1", rank=5)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["trade_usd"], 2000.0)
        self.assertEqual(e["usd_method"], "stable")
        self.assertEqual(e["wallet_sha"], wq_common.pseudo("W1"))
        self.assertEqual(e["rank_at_capture"], 5)

    def test_all_buys_recorded_regardless_of_size(self):
        tx = {"signature": "s2", "timestamp": 100,
              "tokenTransfers": [_tf(USDC, "W1", "PoolX", 50),
                                  _tf(MINT, "PoolX", "W1", 10)]}
        events = derive_buys.buys_from_tx(tx, "W1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["trade_usd"], 50.0)  # <$1500 kept (L2 feeds)

    def test_sol_buy_uses_fallback_price_and_marks_method(self):
        tx = {"signature": "s3", "timestamp": 100,
              "tokenTransfers": [_tf(WSOL, "W1", "PoolX", 10),
                                  _tf(MINT, "PoolX", "W1", 100)]}
        events = derive_buys.buys_from_tx(tx, "W1")
        self.assertEqual(events[0]["usd_method"], "sol_fallback")
        self.assertEqual(events[0]["trade_usd"],
                         10 * derive_buys.SOL_PRICE_USD)

    def test_incoming_transfer_is_not_a_buy(self):
        tx = {"signature": "s4", "timestamp": 100,
              "tokenTransfers": [_tf(MINT, "W2", "W1", 1000)]}
        self.assertEqual(derive_buys.buys_from_tx(tx, "W1"), [])

    def test_multi_mint_tx_is_honestly_unpriced(self):
        tx = {"signature": "s5", "timestamp": 100,
              "tokenTransfers": [_tf(USDC, "W1", "PoolX", 1000),
                                  _tf(MINT, "PoolX", "W1", 100),
                                  _tf("TokenB22222222222222222222222222222222222222",
                                      "PoolX", "W1", 5)]}
        events = derive_buys.buys_from_tx(tx, "W1")
        self.assertEqual(len(events), 2)
        for e in events:
            self.assertIsNone(e["trade_usd"])
            self.assertEqual(e["usd_method"], "multi_mint")


class ClusterTests(unittest.TestCase):
    def test_funding_edge_clusters_wallets(self):
        wallet_a, wallet_b = "WA", "WB"
        dumps = {wq_common.pseudo(wallet_a): [
            {"signature": "t1", "timestamp": 500, "feePayer": wallet_a,
             "tokenTransfers": [_tf(USDC, wallet_b, wallet_a, 5000)]}]}
        universe_map = {wq_common.pseudo(wallet_a): wallet_a,
                        wq_common.pseudo(wallet_b): wallet_b}
        e1, cobuy, received, shadow, cex = cluster_analysis.scan(dumps, universe_map)
        uf, edges, _ = cluster_analysis.build_edges(e1, cobuy, received, universe_map)
        self.assertEqual(uf.find(wq_common.pseudo(wallet_a)),
                         uf.find(wq_common.pseudo(wallet_b)))
        self.assertTrue(any(e[2] == "E1" for e in edges))

    def test_sub_threshold_funding_does_not_link(self):
        dumps = {wq_common.pseudo("WA"): [
            {"signature": "t1", "timestamp": 500,
             "tokenTransfers": [_tf(USDC, "WB", "WA", 10)]}]}
        universe_map = {wq_common.pseudo("WA"): "WA"}
        e1, *_ = cluster_analysis.scan(dumps, universe_map)
        self.assertEqual(e1, [])

    def test_unknown_counterparty_becomes_shadow_candidate(self):
        dumps = {wq_common.pseudo("WA"): [
            {"signature": "t1", "timestamp": 500,
             "tokenTransfers": [_tf(USDC, "WA", "UnknownSub", 9000)]}]}
        universe_map = {wq_common.pseudo("WA"): "WA"}
        e1, cobuy, received, shadow, cex = cluster_analysis.scan(dumps, universe_map)
        self.assertIn("UnknownSub", shadow)
        self.assertGreaterEqual(shadow["UnknownSub"]["edge_usd"], 9000)

    def test_cex_hot_wallet_never_links(self):
        m3 = wq_common.CONFIG["m3"]
        old = m3["cex_exclusion_min_wallets"]
        m3["cex_exclusion_min_wallets"] = 2
        try:
            # "CEXHOT" is counterparty for 2 universe wallets -> exchange-like
            dumps = {
                wq_common.pseudo("WA"): [{"signature": "t1", "timestamp": 500,
                                          "tokenTransfers": [_tf(USDC, "CEXHOT", "WA", 50000)]}],
                wq_common.pseudo("WB"): [{"signature": "t2", "timestamp": 600,
                                          "tokenTransfers": [_tf(USDC, "WB", "CEXHOT", 20000)]}],
            }
            universe_map = {wq_common.pseudo("WA"): "WA",
                            wq_common.pseudo("WB"): "WB"}
            e1, _, _, shadow, cex = cluster_analysis.scan(dumps, universe_map)
            self.assertIn("CEXHOT", cex)
            self.assertEqual(e1, [])
            self.assertNotIn("CEXHOT", shadow)
        finally:
            m3["cex_exclusion_min_wallets"] = old


class SpreadTests(unittest.TestCase):
    def test_burst_rejected_by_gap(self):
        arrivals = [1000, 1010, 1020, 1030]  # <15min gap -> fail
        self.assertFalse(sim.spread_ok(arrivals))

    def test_spread_accepts_accumulation(self):
        arrivals = [1000, 1000 + 20 * 60, 1000 + 50 * 60]
        self.assertTrue(sim.spread_ok(arrivals))

    def test_60s_block_share_cap(self):
        # total gap fine but 5/6 arrivals inside one 60s block -> fail
        arrivals = [1000, 1001, 1002, 1003, 1004, 1000 + 20 * 60]
        self.assertFalse(sim.spread_ok(arrivals))

    def test_share_at_cap_passes(self):
        # 4/5 in one block == 0.8 == max_share_60s -> allowed (<=)
        arrivals = [1000, 1001, 1002, 1003, 1000 + 20 * 60]
        self.assertTrue(sim.spread_ok(arrivals))

    def test_single_arrival_never_fires(self):
        self.assertFalse(sim.spread_ok([1000]))


class ReplayEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = sim.DATA
        sim.DATA = Path(self.tmp.name)
        buys = []
        # 4 independent wallets buy MINT spread over 40 min, all >= l1_min
        for i, w in enumerate(["W1", "W2", "W3", "W4"]):
            buys.append({"event_id": f"e{i}", "wallet_sha": wq_common.pseudo(w),
                         "executed_at": 10000 + i * 600, "asset_mint": MINT,
                         "trade_usd": 2000.0 + i, "token_amount": 1000.0})
        # price rises 2x over the window so exit > entry after delay
        for i in range(10):
            buys.append({"event_id": f"x{i}", "wallet_sha": "PUMP",
                         "executed_at": 20000 + i * 300, "asset_mint": MINT,
                         "trade_usd": 1000.0 * (2 ** (i / 5.0)),
                         "token_amount": 1000.0})
        with (sim.DATA / "buys.jsonl").open("w") as h:
            for b in buys:
                h.write(json.dumps(b) + "\n")

    def tearDown(self):
        sim.DATA = self.old_data
        self.tmp.cleanup()

    def test_cluster_replay_produces_l1_and_l2_cells(self):
        sim.main()
        out = json.loads((sim.DATA / "m4_summary.json").read_text())
        l1_cells = [c for c in out["grid"] if c.startswith("L1_")]
        self.assertTrue(l1_cells)
        k3 = out["grid"].get("L1_w30_k3") or out["grid"].get("L1_w120_k3")
        self.assertIsNotNone(k3)
        self.assertGreaterEqual(k3["n"], 1)
        self.assertTrue(any(c.startswith("L2_") for c in out["grid"]))

    def test_burst_never_fires(self):
        # 5 wallet-buys in the SAME second + one curve provider whose 2 fills
        # are also same-second -> every arrival shares one 60s block (>80%)
        with (sim.DATA / "buys.jsonl").open("w") as h:
            for i, w in enumerate(["W1", "W2", "W3", "W4", "W5"]):
                h.write(json.dumps({
                    "event_id": f"b{i}", "wallet_sha": wq_common.pseudo(w),
                    "executed_at": 10000, "asset_mint": MINT,
                    "trade_usd": 5000.0, "token_amount": 1000.0}) + "\n")
            for i, ts in enumerate([10000, 90000]):
                h.write(json.dumps({
                    "event_id": f"x{i}", "wallet_sha": wq_common.pseudo("PUMP"),
                    "executed_at": ts, "asset_mint": MINT,
                    "trade_usd": 1000.0 * (2 + 2 * i), "token_amount": 1000.0}) + "\n")
        sim.main()
        out = json.loads((sim.DATA / "m4_summary.json").read_text())
        self.assertEqual(out["grid"], {})


if __name__ == "__main__":
    unittest.main()
