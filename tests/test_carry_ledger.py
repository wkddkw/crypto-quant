import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("carry_trader", ROOT / "carry_trader.py")
carry = importlib.util.module_from_spec(spec)
sys.modules["carry_trader"] = carry
spec.loader.exec_module(carry)


class CarryLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.paths = {name: getattr(carry, name) for name in
                      ("CARRY", "ACCOUNT", "EVENTS", "SNAPSHOTS", "FUNDING_LEDGER", "REPORT", "LOCK")}
        carry.CARRY = base
        carry.ACCOUNT = base / "account.json"
        carry.EVENTS = base / "events.jsonl"
        carry.SNAPSHOTS = base / "snapshots.csv"
        carry.FUNDING_LEDGER = base / "funding_ledger.csv"
        carry.REPORT = base / "report.md"
        carry.LOCK = base / ".run.lock"

    def tearDown(self):
        for name, value in self.paths.items():
            setattr(carry, name, value)
        self.tmp.cleanup()

    def account(self):
        return {
            "cash": 499.4, "spot_qty": 0.005, "perp_qty": 0.005,
            "short_notional": 400.0, "entry_spot": 80000.0,
            "entry_swap": 80000.0, "margin": 200.0, "status": "open",
            "last_funding_ts": 0, "realized_funding": 0.0, "fees": 0.6,
            "funding_events": [], "trades": [], "equity_history": [],
        }

    def snapshot(self, spot=81000.0, swap=81000.0, funding=0.0001):
        return {
            "observed_at": 1_800_000_000_000, "spot_last": spot,
            "spot_bid": spot - 1, "spot_ask": spot + 1,
            "swap_last": swap, "swap_bid": swap - 1, "swap_ask": swap + 1,
            "swap_mark": swap, "swap_index": swap, "funding_rate": funding,
            "funding_time": 0, "next_funding_time": 0, "basis": swap / spot - 1,
            "spread_bps": 0.2, "age_ms": 0, "source": "fixture",
        }

    def test_matched_legs_cancel_parallel_price_move(self):
        spot_pnl, swap_pnl, basis_pnl, equity = carry.mark(self.account(), self.snapshot())
        self.assertAlmostEqual(spot_pnl, 5.0)
        self.assertAlmostEqual(swap_pnl, -5.0)
        self.assertAlmostEqual(basis_pnl, 0.0)
        self.assertAlmostEqual(equity, self.account()["cash"])

    def test_basis_move_has_explicit_residual(self):
        _, _, basis_pnl, _ = carry.mark(self.account(), self.snapshot(spot=81000, swap=80500))
        self.assertAlmostEqual(basis_pnl, 2.5)

    def test_open_starts_after_observed_funding(self):
        a = self.account()
        a["status"] = "flat"
        carry.open_position(a, self.snapshot(), "fixture")
        self.assertEqual(a["status"], "open")
        self.assertEqual(a["last_funding_ts"], self.snapshot()["observed_at"])
        self.assertTrue(carry.EVENTS.exists())

    def test_capital_limit_covers_spot_and_margin(self):
        notional = carry.max_open_notional(500.0)
        self.assertLessEqual(notional + notional / carry.CONFIG["margin_leverage"], 500.0)

    def test_mark_does_not_mutate_account(self):
        a = self.account()
        before = copy.deepcopy(a)
        carry.mark(a, self.snapshot())
        self.assertEqual(a, before)

    def test_open_basis_breach_closes_position(self):
        a = self.account()
        s = self.snapshot(spot=80000, swap=83000)
        s["basis"] = s["swap_mark"] / s["spot_last"] - 1
        self.assertEqual(carry.decision(a, s), ("close", "basis_out_of_range"))

    def test_stale_data_halts_without_synthetic_close(self):
        a = self.account()
        s = self.snapshot()
        s["age_ms"] = carry.CONFIG["max_data_age_ms"] + 1
        self.assertEqual(carry.decision(a, s), ("halt", "stale_market_data"))

    def test_close_uses_executable_prices(self):
        a = self.account()
        s = self.snapshot(spot=81000, swap=81000)
        before = a["cash"]
        carry.close_position(a, s, "fixture")
        executable_spot = s["spot_bid"] * (1 - carry.CONFIG["slippage"])
        executable_swap = s["swap_ask"] * (1 + carry.CONFIG["slippage"])
        expected = before + a["trades"][-1]["basis_pnl"] - a["trades"][-1]["fee"]
        self.assertAlmostEqual(a["cash"], expected)
        self.assertAlmostEqual(a["trades"][-1]["basis_pnl"],
                               0.005 * (executable_spot - 80000)
                               + 0.005 * (80000 - executable_swap))

    def test_funding_is_idempotent_and_cursor_only_advances_processed_rows(self):
        funding_file = ROOT / "data" / ("okx_funding_" + carry.CONFIG["swap_inst"] + ".parquet")
        original = pd.read_parquet(funding_file)
        fixture = pd.DataFrame([
            {"ts": 100, "inst": carry.CONFIG["swap_inst"], "funding_rate": 0.0001, "src": "okx"},
            {"ts": 200, "inst": carry.CONFIG["swap_inst"], "funding_rate": -0.0001, "src": "okx"},
        ])
        a = self.account()
        a["last_funding_ts"] = 0
        s = self.snapshot()
        s["observed_at"] = 250
        with patch.object(pd, "read_parquet", return_value=fixture):
            cash, count = carry.funding_events(a, s)
            again, repeat = carry.funding_events(a, s)
        self.assertEqual(count, 2)
        self.assertEqual(repeat, 0)
        self.assertEqual(a["last_funding_ts"], 200)
        self.assertAlmostEqual(again, 0.0)
        self.assertAlmostEqual(cash, 0.0)
        self.assertTrue(carry.FUNDING_LEDGER.exists())
        self.assertEqual(len(original.columns), 4)


if __name__ == "__main__":
    unittest.main()
