import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import daily_report as report


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_out = report.OUT
        self.old_sync = report.SYNC
        report.OUT = Path(self.tmp.name) / "daily_reports"
        report.SYNC = Path(self.tmp.name) / "sync"

    def tearDown(self):
        report.OUT = self.old_out
        report.SYNC = self.old_sync
        self.tmp.cleanup()

    def payload(self):
        account = {"cash": 425.44, "btc": 0.0005,
                   "equity_history": [{"equity": 500.89}]}
        with tempfile.TemporaryDirectory() as ledger:
            root = Path(ledger)
            (root / "account.json").write_text(json.dumps(account))
            return report.strategy_summary("BTC direction", "btc_v0_full", root, 0, 9_999_999_999_999)

    def test_btc_summary_uses_latest_marked_equity(self):
        summary = self.payload()
        self.assertEqual(summary["cash"], 425.44)
        self.assertEqual(summary["equity"], 500.89)
        self.assertAlmostEqual(summary["position_value"], 75.45)
        self.assertEqual(summary["strategy_id"], "btc_v0_full")
        self.assertEqual(summary["initial_equity"], 500.0)

    def test_btc_summary_without_mark_falls_back_to_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "account.json").write_text(json.dumps({"cash": 500.0, "btc": 0.0}))
            summary = report.strategy_summary("BTC direction", "btc_v0_full", root, 0, 9_999_999_999_999)
        self.assertEqual(summary["equity"], 500.0)
        self.assertEqual(summary["position_value"], 0.0)

    def test_slot_report_is_immutable_and_writes_sync_package(self):
        slot_payload = {"date_beijing": "2026-09-01",
                        "generated_at": "2026-09-01T06:00:00+08:00",
                        "git_revision": "test", "paper_only": True,
                        "strategies": [], "governance": {"strategies": []}}
        text = report.render(slot_payload)
        package = report.sync_package("2026-09-01T0600+0800", slot_payload, text)
        self.assertTrue((report.SYNC / "2026-09-01T0600+0800.md").exists())
        self.assertTrue((report.SYNC / "2026-09-01T0600+0800.json").exists())
        self.assertEqual(package["sync_id"], "2026-09-01T0600+0800")
        self.assertIn("report_md_sha256", package)

    def test_invalid_slot_is_rejected(self):
        with self.assertRaises(ValueError):
            report.build("2026-09-01", slot="0700")

    def test_delivery_audit_is_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "delivery_audit.jsonl"
            old = report.DELIVERY_AUDIT
            report.DELIVERY_AUDIT = audit
            try:
                report.record_delivery("telegram-primary", "2026-09-01T0600+0800", "ok", message_id="m1")
                report.record_delivery("telegram-primary", "2026-09-01T0600+0800", "failed",
                                       error_class="network")
                rows = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
            finally:
                report.DELIVERY_AUDIT = old
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["outcome"], "ok")
        self.assertEqual(rows[1]["error_class"], "network")


if __name__ == "__main__":
    unittest.main()
