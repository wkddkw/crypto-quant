import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import daily_report as report


class DailyReportTests(unittest.TestCase):
    def test_btc_summary_uses_latest_marked_equity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "account.json").write_text(json.dumps({
                "cash": 425.44,
                "btc": 0.0005,
                "equity_history": [{"equity": 500.89}],
            }))
            summary = report.strategy_summary("BTC direction", root, 0, 9_999_999_999_999)

        self.assertEqual(summary["cash"], 425.44)
        self.assertEqual(summary["equity"], 500.89)
        self.assertAlmostEqual(summary["position_value"], 75.45)
        self.assertEqual(summary["initial_equity"], 500.0)

    def test_btc_summary_without_mark_falls_back_to_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "account.json").write_text(json.dumps({"cash": 500.0, "btc": 0.0}))
            summary = report.strategy_summary("BTC direction", root, 0, 9_999_999_999_999)

        self.assertEqual(summary["equity"], 500.0)
        self.assertEqual(summary["position_value"], 0.0)


if __name__ == "__main__":
    unittest.main()
