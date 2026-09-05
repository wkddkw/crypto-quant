import copy
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import research_runner as research
import trend_paper


def _utc_day_ms(start, periods):
    """Portable UTC midnight ms across pandas 2 (ns) and 3 (us) int resolutions."""
    idx = pd.date_range(start, periods=periods, freq="D", tz="UTC")
    return np.array([int(ts.timestamp() * 1000) for ts in idx], dtype="int64")


def candles(n=1750):
    rng = np.random.default_rng(71)
    close = 100 * np.exp(np.cumsum(rng.normal(.0006, .025, n)))
    opening = np.r_[close[0], close[:-1]] * np.exp(rng.normal(0, .005, n))
    return pd.DataFrame({"ts": _utc_day_ms("2017-01-01", n),
                         "inst": "BTC-USDT", "open": opening, "close": close,
                         "high": np.maximum(opening, close) * 1.01,
                         "low": np.minimum(opening, close) * .99})


def encoded(frame):
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    return buffer.getvalue()


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((research.ROOT / "research_config.json").read_text())
        self.config["assets"] = ["BTC-USDT"]
        self.config["validation_start"] = "2021-01-01"
        self.frame = candles()
        self.now = datetime(2021, 11, 1, tzinfo=timezone.utc)

    def test_unclosed_excluded_and_gaps_duplicates_wrong_instrument_rejected(self):
        frame = self.frame.iloc[:250].copy()
        now_ms = int(frame.ts.iloc[-1])
        loaded, check = research.load_candles(encoded(frame), "BTC-USDT", now_ms)
        self.assertEqual(len(loaded), 249)
        self.assertEqual(check["excluded_unclosed"], 1)
        variants = [frame.drop(index=20), pd.concat([frame, frame.iloc[[20]]]), frame.assign(inst="ETH-USDT")]
        for variant in variants:
            with self.assertRaises(ValueError):
                research.load_candles(encoded(variant), "BTC-USDT", now_ms)

    def test_invalid_prices_fail_closed(self):
        for column, value in [("close", float("nan")), ("open", -1), ("high", 0.01)]:
            frame = self.frame.copy()
            frame.loc[100, column] = value
            with self.assertRaises(ValueError):
                research.load_candles(encoded(frame), "BTC-USDT", int(self.now.timestamp() * 1000))

    def test_signals_match_shadow_and_are_causal(self):
        signals = research.targets(self.frame, self.config)
        for index in (210, 540, 1000, len(self.frame) - 1):
            with patch.object(trend_paper.pd, "read_parquet", return_value=self.frame):
                expected = trend_paper.build_signal(int(self.frame.ts.iloc[index]) + research.DAY_MS + 180_000)
            self.assertAlmostEqual(signals.trend_only.iloc[index], expected["target"], places=10)
        self.assertTrue(research.bias_checks(self.frame, self.config)["passed"])
        changed = self.frame.copy()
        changed.loc[800:, ["open", "high", "low", "close"]] *= 10
        pd.testing.assert_frame_equal(signals.iloc[:800], research.targets(changed, self.config).iloc[:800])

    def test_bias_checker_detects_future_information(self):
        original = research.targets

        def biased(frame, config):
            result = original(frame, config)
            result["trend_only"] = frame["close"].mean() / 10000
            return result

        with patch.object(research, "targets", side_effect=biased):
            self.assertFalse(research.bias_checks(self.frame, self.config)["passed"])

    def test_nan_at_prefix_boundary_does_not_hide_lookahead(self):
        original = research.targets

        def shifted(frame, config):
            result = original(frame, config)
            result["trend_only"] = result["trend_only"].shift(-1)
            return result

        with patch.object(research, "targets", side_effect=shifted):
            result = research.bias_checks(self.frame, self.config)
        self.assertFalse(result["passed"])
        json.dumps(result, allow_nan=False)

    def test_execution_matches_shadow_at_identical_prices_and_targets(self):
        config = self.config
        weights = research.targets(self.frame, config).trend_only
        start, end = 250, 410
        marks, trades = research.simulate(self.frame, weights, start, end, config, config["scenarios"]["base"])
        account = None
        for i, row in zip(range(start, end), marks):
            signal = {"candle_ts": int(self.frame.ts.iloc[i - 1]), "target": float(weights.iloc[i - 1]),
                      "s": float(weights.iloc[i - 1])}
            account = trend_paper.advance(account, signal, float(self.frame.open.iloc[i]), int(self.frame.ts.iloc[i]))
            self.assertAlmostEqual(row["cash"], account["cash"], places=9)
            self.assertAlmostEqual(row["qty"], account["btc"], places=9)
            self.assertAlmostEqual(row["equity"], account["cash"] + account["btc"] * self.frame.close.iloc[i], places=9)
        self.assertEqual(len(trades), len(account["trades"]))
        self.assertAlmostEqual(sum(t["fee"] for t in trades), account["fees"])

    def test_first_entry_cost_included_and_baseline_not_rebalanced(self):
        frame = candles(220)
        frame[["open", "high", "low", "close"]] = 100.0
        weights = pd.Series(1.0, index=frame.index)
        marks, trades = research.simulate(frame, weights, 205, 215, self.config, self.config["scenarios"]["base"])
        stats = research.statistics(marks, trades, 500, monthly_cost=10)
        self.assertEqual(len(trades), 1)
        expected = 500 / (1.0003 * 1.001)
        self.assertAlmostEqual(stats["final_equity"], expected)
        self.assertAlmostEqual(stats["net_return"], expected / 500 - 1)
        self.assertAlmostEqual(stats["max_drawdown"], expected / 500 - 1)
        self.assertAlmostEqual(stats["pnl_after_running_cost"], expected - 500 - 10 * 10 * 12 / 365.25)
        self.assertIsNone(stats["cagr"])
        stressed, _ = research.simulate(frame, weights, 205, 215, self.config, self.config["scenarios"]["slippage_x2"])
        self.assertLess(stressed[-1]["equity"], marks[-1]["equity"])

    def test_signal_delay_never_executes_current_candle(self):
        weights = pd.Series(0.0, index=self.frame.index)
        weights.iloc[251:] = 1.0
        _, base = research.simulate(self.frame, weights, 250, 260, self.config, self.config["scenarios"]["base"])
        _, delayed = research.simulate(self.frame, weights, 250, 260, self.config, self.config["scenarios"]["signal_delay_1d"])
        self.assertEqual(base[0]["ts"], int(self.frame.ts.iloc[252]))
        self.assertEqual(delayed[0]["ts"], int(self.frame.ts.iloc[253]))
        self.assertTrue(all(t["ts"] >= t["signal_ts"] + 2 * research.DAY_MS for t in delayed))

    def test_rolling_splits_and_compound_folds_match_continuous_account(self):
        frame = candles(2300)
        splits = research.rolling_splits(frame, self.config)
        self.assertEqual(splits[0]["year"], 2021)
        self.assertTrue(splits[-1]["partial_year"])
        config = copy.deepcopy(self.config)
        config["scenarios"] = {"base": config["scenarios"]["base"]}
        results, _, _ = research.compare_asset(frame, config, splits, None)
        for result in results["base"].values():
            compound = np.prod([1 + fold["validation"]["net_return"] for fold in result["folds"]]) - 1
            self.assertAlmostEqual(compound, result["continuous_validation"]["net_return"])

    def test_artifacts_reproducible_frozen_and_do_not_touch_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            source = data / "okx_candles_BTC-USDT.parquet"
            source.write_bytes(encoded(self.frame))
            ledger = data / "paper/account.json"
            ledger.parent.mkdir()
            ledger.write_text('{"cash": 506.8}')
            config = root / "config.json"
            config.write_text(json.dumps(self.config))
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, ledger)}
            a = research.run(data, root / "research", config, now=self.now)
            b = research.run(data, root / "research", config, now=self.now)
            self.assertNotEqual(a, b)
            self.assertEqual((a / "comparison.json").read_bytes(), (b / "comparison.json").read_bytes())
            self.assertEqual((a / "BTC-USDT/equity.csv").read_bytes(), (b / "BTC-USDT/equity.csv").read_bytes())
            manifest = json.loads((a / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertFalse(manifest["untouched_holdout"])
            self.assertEqual(manifest["inputs"]["BTC-USDT"]["sha256"], before[source])
            for path, expected in before.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
            self.assertEqual((a / "snapshots" / source.name).read_bytes(), source.read_bytes())

    def test_failed_experiment_is_archived_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.json"
            config.write_text(json.dumps(self.config))
            with self.assertRaises(FileNotFoundError):
                research.run(root, root / "research", config, now=self.now)
            outputs = list((root / "research").iterdir())
            self.assertEqual(len(outputs), 1)
            self.assertEqual(json.loads((outputs[0] / "manifest.json").read_text())["status"], "failed")
            self.assertFalse((outputs[0] / "report.md").exists())

    def test_config_and_cost_validation(self):
        research.validate_config(self.config)
        for bad in (float("nan"), -1):
            with self.assertRaises(ValueError):
                research.run(monthly_cost=bad)
        changed = copy.deepcopy(self.config)
        changed["scenarios"]["base"]["signal_delay_days"] = 1
        with self.assertRaises(ValueError):
            research.validate_config(changed)


if __name__ == "__main__":
    unittest.main()
