import copy
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("paper_trader", ROOT / "paper_trader.py")
paper = importlib.util.module_from_spec(spec)
sys.modules["paper_trader"] = paper
spec.loader.exec_module(paper)


class PaperTraderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_paper = paper.PAPER
        self.old_account = paper.ACCOUNT
        paper.PAPER = Path(self.tmp.name)
        paper.ACCOUNT = paper.PAPER / "account.json"

    def tearDown(self):
        paper.PAPER = self.old_paper
        paper.ACCOUNT = self.old_account
        self.tmp.cleanup()

    def signal(self, target):
        return {"target": target, "s": target, "total": target * 12,
                "s_trend": target, "factors": {}, "context": {},
                "price_close": 100, "ma50": 100, "ma200": 100,
                "ret_1d": 0, "ret_7d": 0, "ret_30d": 0}

    def exercise_trade_cycle(self, account, target):
        saved = []
        with patch.object(paper, "load_account", return_value=account), \
             patch.object(paper, "build_signal", return_value=self.signal(target)), \
             patch.object(paper, "live_price", return_value=100.0), \
             patch.object(paper, "save_account", side_effect=lambda a: saved.append(copy.deepcopy(a))), \
             patch.object(paper, "record_prediction", return_value={"accuracy": None, "n_dir": 0}), \
             patch.object(paper, "write_report"):
            decision = paper.run_once()
        return decision, saved[-1]

    def test_dust_buy_does_not_change_balances(self):
        account = {"cash": 5.0, "btc": 0.0, "equity_history": [], "trades": [], "decisions": []}
        decision, saved = self.exercise_trade_cycle(account, 0.10)
        self.assertEqual(decision["action"], "hold(dust)")
        self.assertEqual(saved["cash"], 5.0)
        self.assertEqual(saved["btc"], 0.0)
        self.assertEqual(saved["trades"], [])

    def test_dust_sell_does_not_change_balances(self):
        account = {"cash": 0.0, "btc": 0.05, "equity_history": [], "trades": [], "decisions": []}
        decision, saved = self.exercise_trade_cycle(account, 0.89)
        self.assertEqual(decision["action"], "hold(dust)")
        self.assertEqual(saved["cash"], 0.0)
        self.assertEqual(saved["btc"], 0.05)
        self.assertEqual(saved["trades"], [])

    def test_notional_at_least_one_dollar_executes(self):
        account = {"cash": 20.0, "btc": 0.0, "equity_history": [], "trades": [], "decisions": []}
        decision, saved = self.exercise_trade_cycle(account, 0.20)
        self.assertEqual(decision["action"], "BUY")
        self.assertEqual(len(saved["trades"]), 1)
        self.assertLess(saved["cash"], 20.0)
        self.assertGreater(saved["btc"], 0.0)


if __name__ == "__main__":
    unittest.main()
