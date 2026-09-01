import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("polymarket_data", ROOT / "polymarket_data.py")
pm = importlib.util.module_from_spec(spec)
sys.modules["polymarket_data"] = pm
spec.loader.exec_module(pm)


class PolymarketDataTests(unittest.TestCase):
    def test_normalize_market_accepts_json_strings(self):
        result = pm.normalize_market({"id": 1, "conditionId": "c", "question": "BTC Up or Down",
                                      "outcomes": '["Up", "Down"]', "clobTokenIds": '["u", "d"]',
                                      "active": True})
        self.assertEqual(result["market_id"], "1")
        self.assertEqual(result["token_ids"], ["u", "d"])

    def test_quote_uses_executable_ask_and_costs(self):
        market = {"market_id": "m", "condition_id": "c", "question": "", "slug": "", "token_ids": ["u", "d"]}
        books = {"u": {"asks": [{"price": "0.44", "size": "10"}], "bids": [{"price": "0.43", "size": "5"}]},
                 "d": {"asks": [{"price": "0.54", "size": "10"}], "bids": [{"price": "0.53", "size": "5"}]}}
        original = pm.orderbook
        pm.orderbook = lambda token: books[token]
        try:
            quote = pm.quote_market(market)
        finally:
            pm.orderbook = original
        self.assertAlmostEqual(quote["complete_set_cost"], 0.98)
        self.assertLess(quote["net_edge"], quote["gross_edge"])

    def test_target_market_rejects_hegseth_and_long_dated_markets(self):
        bad = {"question": "Will Pete Hegseth win?", "slug": "pete-hegseth", "token_ids": ["u", "d"], "end_date": "2028-11-07T00:00:00Z"}
        self.assertFalse(pm.is_target_market(bad))

    def test_paper_ledger_is_separate(self):
        spec = importlib.util.spec_from_file_location("polymarket_paper", ROOT / "polymarket_paper.py")
        ledger = importlib.util.module_from_spec(spec)
        sys.modules["polymarket_paper"] = ledger
        spec.loader.exec_module(ledger)
        self.assertNotEqual(ledger.DATA.resolve(), (ROOT / "data" / "carry").resolve())
        self.assertNotEqual(ledger.DATA.resolve(), (ROOT / "data" / "paper").resolve())


if __name__ == "__main__":
    unittest.main()
