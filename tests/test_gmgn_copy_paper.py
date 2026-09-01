import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


models = load("gmgn_models")
adapter = load("gmgn_adapter")
paper = load("gmgn_copy_paper")


class GmgnPaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.paths = {name: getattr(paper, name) for name in ("DATA", "ACCOUNT", "EVENTS", "DECISIONS", "SIGNALS", "STATUS", "REPORT", "RANKINGS", "LOCK")}
        paper.DATA = base
        paper.ACCOUNT = base / "account.json"
        paper.EVENTS = base / "events.jsonl"
        paper.DECISIONS = base / "decisions.jsonl"
        paper.SIGNALS = base / "signals.jsonl"
        paper.STATUS = base / "status.json"
        paper.REPORT = base / "report.md"
        paper.RANKINGS = base / "rankings"
        paper.LOCK = base / ".run.lock"

    def tearDown(self):
        for name, value in self.paths.items():
            setattr(paper, name, value)
        self.tmp.cleanup()

    def event(self, **changes):
        row = {
            "event_id": "test:0", "chain": "solana", "wallet_address": "wallet", "executed_at": 1000,
            "asset_mint": "mint", "asset_symbol": "TEST", "side": "buy", "trade_usd": 500,
            "price_usd": 2.0, "liquidity_usd": 50000,
        }
        row.update(changes)
        return models.TradeEvent.from_dict(row)

    def test_sells_and_stale_events_are_rejected(self):
        account = paper.load_account()
        self.assertEqual(paper.decision(account, self.event(side="sell"), {"wallet"}, 2000), ("reject", "sell_disabled"))
        stale = 1000 + paper.CONFIG["paper"]["max_event_age_sec"] * 1000 + 1
        self.assertEqual(paper.decision(account, self.event(), {"wallet"}, stale), ("reject", "stale_event"))

    def test_open_is_idempotent_by_event_id(self):
        account = paper.load_account()
        event = self.event()
        self.assertEqual(paper.decision(account, event, {"wallet"}, 2000)[0], "open")
        paper.open_position(account, event, 2000)
        account["processed_event_ids"].append(event.event_id)
        self.assertEqual(paper.decision(account, event, {"wallet"}, 2000), ("reject", "duplicate_event"))
        self.assertLess(account["cash"], account["initial_equity"])
        self.assertTrue(paper.EVENTS.exists())

    def test_fixture_run_is_separate_and_repeat_safe(self):
        status = paper.run_once(observed_at=4102444800000)
        again = paper.run_once(observed_at=4102444800000)
        account = json.loads(paper.ACCOUNT.read_text())
        self.assertEqual(status["accepted"], 1)
        self.assertEqual(again["accepted"], 0)
        self.assertEqual(len(account["positions"]), 1)
        self.assertNotEqual(paper.DATA.resolve(), (ROOT / "data" / "carry").resolve())
        self.assertNotEqual(paper.DATA.resolve(), (ROOT / "data" / "paper").resolve())

    def test_live_contract_blocks_without_http(self):
        original = adapter.CONFIG["mode"]
        adapter.CONFIG["mode"] = "live"
        try:
            with self.assertRaises(adapter.AdapterBlocked):
                adapter.rank_wallets()
        finally:
            adapter.CONFIG["mode"] = original


if __name__ == "__main__":
    unittest.main()
