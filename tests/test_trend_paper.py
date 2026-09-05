import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import trend_paper as trend
from paper_metrics import metrics


class TrendPaperTests(unittest.TestCase):
    def signal(self, candle=200 * trend.DAY_MS, target=1.0):
        return {"candle_ts": candle, "target": target, "s": target}

    def test_fully_invested_strategy_matches_costed_benchmark(self):
        account = trend.advance(None, self.signal(), 100.0, 201 * trend.DAY_MS)
        expected_qty = 500 / (100 * 1.0003 * 1.001)
        self.assertAlmostEqual(account["btc"], expected_qty)
        self.assertAlmostEqual(account["cash"], 0)
        self.assertAlmostEqual(account["fees"], account["benchmark"]["fees"])
        self.assertAlmostEqual(metrics(account)["excess_return"], 0)
        self.assertLess(metrics(account)["net_return"], 0)

    def test_hourly_marks_do_not_rebalance_and_daily_exit_counts_costs(self):
        first = 201 * trend.DAY_MS
        account = trend.advance(None, self.signal(target=0.5), 100, first)
        account = trend.advance(account, self.signal(target=0), 200, first + 3600_000)
        self.assertEqual(len(account["trades"]), 1)
        self.assertEqual(len(account["daily_observations"]), 1)
        account = trend.advance(account, self.signal(201 * trend.DAY_MS, 0), 200, first + trend.DAY_MS)
        self.assertEqual(len(account["trades"]), 2)
        self.assertAlmostEqual(account["btc"], 0)
        qty = 250 / 100
        expected = 500 - qty * 100 * 1.0003 * 1.001 + qty * 200 * .9997 * .999
        self.assertAlmostEqual(account["cash"], expected)
        self.assertEqual(metrics(account)["complete_daily_intervals"], 1)
        self.assertEqual(metrics(account)["trade_count"], 2)

    def test_gap_does_not_count_as_complete_day(self):
        first = 201 * trend.DAY_MS
        account = trend.advance(None, self.signal(), 100, first)
        account = trend.advance(account, self.signal(203 * trend.DAY_MS), 90, first + 3 * trend.DAY_MS)
        self.assertEqual(metrics(account)["complete_daily_intervals"], 0)
        self.assertEqual(metrics(account)["daily_gaps"], 1)
        self.assertLess(metrics(account)["max_drawdown"], -.1)

    def test_invalid_price_or_changed_rules_reject_without_mutation(self):
        first = 201 * trend.DAY_MS
        account = trend.advance(None, self.signal(), 100, first)
        before = copy.deepcopy(account)
        for price in (float("nan"), float("inf"), 0, -1):
            with self.assertRaises(ValueError):
                trend.advance(account, self.signal(), price, first + 1)
            self.assertEqual(account, before)
        account["rules_sha256"] = "different"
        with self.assertRaisesRegex(ValueError, "rules_changed"):
            trend.advance(account, self.signal(), 100, first + 1)

    def candles(self):
        return pd.DataFrame({"ts": np.arange(202) * trend.DAY_MS,
                             "close": np.linspace(100, 160, 202),
                             "high": np.linspace(101, 161, 202)})

    def test_signal_matches_frozen_trend_formula_and_excludes_open_candle(self):
        frame = self.candles()
        with patch.object(trend.pd, "read_parquet", return_value=frame):
            signal = trend.build_signal(201 * trend.DAY_MS + 180_000)
        close = frame["close"].iloc[:201]
        momentum = np.clip(close.pct_change(90).iloc[-1] /
                           (np.log(close).diff().rolling(90).std().iloc[-1] * np.sqrt(365)) * 2, -2, 2)
        self.assertAlmostEqual(signal["s"], (3 + momentum) / 6)
        self.assertEqual(signal["candle_ts"], 200 * trend.DAY_MS)
        frame.loc[201, "close"] = 1e9
        with patch.object(trend.pd, "read_parquet", return_value=frame):
            self.assertEqual(signal, trend.build_signal(201 * trend.DAY_MS + 180_000))

    def test_stale_and_missing_candles_block(self):
        frame = self.candles()
        with patch.object(trend.pd, "read_parquet", return_value=frame):
            with self.assertRaisesRegex(ValueError, "stale"):
                trend.build_signal(203 * trend.DAY_MS)
        with patch.object(trend.pd, "read_parquet", return_value=frame.drop(index=50)):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                trend.build_signal(202 * trend.DAY_MS)

    def test_run_writes_only_independent_ledger_and_preserves_it_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "trend_paper"
            with patch.object(trend, "LEDGER", ledger), \
                 patch.object(trend, "build_signal", return_value=self.signal()), \
                 patch.object(trend, "live_price", return_value=100):
                trend.run_once()
                path = ledger / "account.json"
                before = path.read_bytes()
                self.assertIn("benchmark", json.loads(before))
                with patch.object(trend, "build_signal", side_effect=ValueError("stale")):
                    with self.assertRaises(ValueError):
                        trend.run_once()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((Path(tmp) / "paper").exists())


if __name__ == "__main__":
    unittest.main()
