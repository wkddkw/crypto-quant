import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import research_runner as research


def _utc_day_ms(start, periods):
    idx = pd.date_range(start, periods=periods, freq="D", tz="UTC")
    return np.array([int(ts.timestamp() * 1000) for ts in idx], dtype="int64")


def candles_from(start="2018-01-11", n=2800, inst="BTC-USDT", seed=71):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0006, 0.025, n)))
    opening = np.r_[close[0], close[:-1]] * np.exp(rng.normal(0, 0.005, n))
    return pd.DataFrame({
        "ts": _utc_day_ms(start, n),
        "inst": inst,
        "open": opening,
        "close": close,
        "high": np.maximum(opening, close) * 1.01,
        "low": np.minimum(opening, close) * 0.99,
    })


def encoded(frame):
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    return buffer.getvalue()


class FixedStartResearchTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((research.ROOT / "research_config_fixed_start.json").read_text())
        self.config["assets"] = ["BTC-USDT"]
        self.frame = candles_from()
        self.now = datetime(2025, 6, 1, tzinfo=timezone.utc)

    def test_three_accounts_start_at_requested_calendar_dates(self):
        windows = research.fixed_start_windows(self.frame, self.config)
        self.assertEqual([w["requested_start"] for w in windows],
                         ["2020-01-01", "2021-01-01", "2022-01-01"])
        for window in windows:
            start = pd.Timestamp(window["requested_start"], tz="UTC")
            self.assertEqual(int(self.frame["ts"].iloc[window["start_index"]]),
                             int(start.timestamp() * 1000))
            self.assertEqual(window["actual_first_execution_time"], start.isoformat())
            warmup = pd.Timestamp(window["warmup_cutoff"])
            self.assertEqual(warmup, start - pd.Timedelta(days=1))
            self.assertGreaterEqual(window["start_index"], research.WARMUP)
            self.assertEqual(window["end_index"], len(self.frame))

        results, _, _ = research.compare_asset_fixed_start(
            self.frame, self.config, windows, None
        )
        for start_str in self.config["validation_starts"]:
            payload = results[start_str]["base"]["trend_only"]
            self.assertEqual(payload["requested_start"], start_str)
            self.assertEqual(payload["actual_first_execution_time"],
                             pd.Timestamp(start_str, tz="UTC").isoformat())

    def test_unavailable_start_hard_fails(self):
        bad = copy.deepcopy(self.config)
        bad["validation_starts"] = ["2017-01-01"]
        with self.assertRaises(ValueError) as ctx:
            research.fixed_start_windows(self.frame, bad)
        self.assertIn("requested_start_unavailable", str(ctx.exception))

        short = candles_from(start="2019-10-01", n=800)
        bad2 = copy.deepcopy(self.config)
        bad2["validation_starts"] = ["2020-01-01"]
        with self.assertRaises(ValueError) as ctx:
            research.fixed_start_windows(short, bad2)
        self.assertIn("insufficient_warmup_before_start", str(ctx.exception))

        gapped = self.frame.copy()
        drop_ts = int(pd.Timestamp("2021-01-01", tz="UTC").timestamp() * 1000)
        gapped = gapped[gapped["ts"] != drop_ts].reset_index(drop=True)
        bad3 = copy.deepcopy(self.config)
        bad3["validation_starts"] = ["2021-01-01"]
        with self.assertRaises(ValueError) as ctx:
            research.fixed_start_windows(gapped, bad3)
        self.assertIn("requested_start_unavailable", str(ctx.exception))

    def test_avg_position_vs_avg_target_distinction(self):
        frame = candles_from(n=400)
        frame[["open", "high", "low", "close"]] = 100.0
        weights = pd.Series(0.5, index=frame.index)
        marks, trades = research.simulate(
            frame, weights, 205, 250, self.config, self.config["scenarios"]["base"]
        )
        stats = research.statistics(marks, trades, self.config["initial_equity"])
        self.assertIn("avg_position", stats)
        self.assertIn("avg_target", stats)
        self.assertAlmostEqual(stats["avg_target"], 0.5, places=9)
        self.assertGreater(stats["avg_position"], 0.4)
        self.assertLess(stats["avg_position"], 0.6)
        fake_marks = [
            {"equity": 100.0, "cash": 80.0, "qty": 0.2, "close": 100.0, "position": 0.2, "target": 1.0,
             "fee": 0.0, "slippage_cost": 0.0},
            {"equity": 100.0, "cash": 80.0, "qty": 0.2, "close": 100.0, "position": 0.2, "target": 1.0,
             "fee": 0.0, "slippage_cost": 0.0},
        ]
        distinct = research.statistics(fake_marks, [], 100.0)
        self.assertAlmostEqual(distinct["avg_position"], 0.2)
        self.assertAlmostEqual(distinct["avg_target"], 1.0)
        self.assertNotAlmostEqual(distinct["avg_position"], distinct["avg_target"])

    def test_trend_only_scale_50_is_half_of_trend_only(self):
        signals = research.targets(self.frame, self.config)
        valid = signals["trend_only"].notna()
        np.testing.assert_allclose(
            signals.loc[valid, "trend_only_scale_50"].to_numpy(),
            signals.loc[valid, "trend_only"].to_numpy() * 0.5,
            rtol=0, atol=1e-15,
        )

    def test_fixed_start_end_to_end_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            (data / "okx_candles_BTC-USDT.parquet").write_bytes(encoded(self.frame))
            config_path = root / "config.json"
            config_path.write_text(json.dumps(self.config))
            out = research.run(data, root / "research", config_path, now=self.now)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["mode"], "fixed_start")
            self.assertFalse(manifest["untouched_holdout"])
            self.assertTrue(manifest["historical_only"])
            comparison = json.loads((out / "comparison.json").read_text())
            groups = 0
            for start in self.config["validation_starts"]:
                for scenario in self.config["scenarios"]:
                    for candidate in self.config["candidates"]:
                        payload = comparison["BTC-USDT"][start][scenario][candidate]
                        self.assertEqual(payload["requested_start"], start)
                        self.assertIn("avg_position", payload["metrics"])
                        self.assertIn("avg_target", payload["metrics"])
                        groups += 1
            self.assertEqual(groups, 3 * 1 * 3 * 4)
            report = (out / "report.md").read_text()
            self.assertIn("historical_only", report)
            self.assertIn("不可称为 OOS", report)

    def test_fixed_start_config_rejects_rolling_candidates(self):
        bad = copy.deepcopy(self.config)
        bad["candidates"] = list(research.ROLLING_CANDIDATES)
        with self.assertRaises(ValueError):
            research.validate_config(bad)


if __name__ == "__main__":
    unittest.main()
