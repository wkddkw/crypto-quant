import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import dashboard_data as data


class DashboardDataTests(unittest.TestCase):
    def test_missing_files_return_visible_error(self):
        frame, error = data.load_csv(Path("/tmp/does-not-exist-dashboard.csv"))
        self.assertTrue(frame.empty)
        self.assertEqual(error, "文件不存在")

    def test_utc_conversion_uses_utc_timezone(self):
        value = data.utc(0)
        self.assertEqual(value.tzinfo.utcoffset(value).total_seconds(), 0)
        self.assertEqual(value.year, 1970)

    def test_dated_frame_accepts_iso_dates(self):
        frame = data.dated_frame(pd.DataFrame({"ts": ["2018-07-31"]}))
        self.assertEqual(str(frame.iloc[0]["time"].tzinfo), "UTC")
        self.assertEqual(frame.iloc[0]["time"].year, 2018)

    def test_equity_keeps_account_values_isolated(self):
        carry_equity = data.equity({"cash": 500, "spot_qty": 1}, "spot_qty", 100)
        paper_equity = data.equity({"cash": 200, "btc": 1}, "btc", 100)
        self.assertEqual(carry_equity, 600)
        self.assertEqual(paper_equity, 300)

    def test_data_health_marks_deribit_as_research_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deribit_funding_BTC-PERPETUAL.parquet").touch()
            frame = pd.DataFrame({"ts": [0, 28_800_000], "funding_rate": [0.0, 0.0]})
            with patch.object(data, "DATA", root), patch.object(data, "load_parquet", return_value=(frame, None)):
                health = data.data_health()
        self.assertEqual(health.iloc[0]["source"], "Deribit research proxy")
        self.assertEqual(health.iloc[0]["gaps"], 0)

    def test_invalid_json_is_a_visible_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{")
            payload, error = data.load_json(path)
        self.assertEqual(payload, {})
        self.assertIn("读取失败", error)


if __name__ == "__main__":
    unittest.main()
