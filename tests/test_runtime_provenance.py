import unittest

from runtime_provenance import canonical_hash, provenance, strategy_version


class RuntimeProvenanceTests(unittest.TestCase):
    def test_canonical_hash_is_order_independent(self):
        self.assertEqual(canonical_hash({"a": 1, "b": 2}), canonical_hash({"b": 2, "a": 1}))

    def test_strategy_version_resolves_from_registry(self):
        self.assertEqual(strategy_version("okx_funding_carry"), "carry_paper_v1")
        self.assertEqual(strategy_version("gmgn_solana_copy"), "gmgn_solana_paper_v1")

    def test_strategy_version_unknown_is_none(self):
        self.assertIsNone(strategy_version("does_not_exist"))

    def test_provenance_contains_required_fields(self):
        record = provenance("okx_funding_carry", "carry_config.json", run_id="run-1")
        for field in ("strategy_id", "strategy_version", "config_sha256",
                      "registry_sha256", "git_revision", "run_id"):
            self.assertIn(field, record)
        self.assertEqual(record["strategy_id"], "okx_funding_carry")
        self.assertEqual(record["run_id"], "run-1")
        self.assertIsNotNone(record["config_sha256"])
        self.assertIsNotNone(record["registry_sha256"])

    def test_config_hash_changes_with_content(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text('{"a": 1}')
            first = provenance("okx_funding_carry", path)["config_sha256"]
            path.write_text('{"a": 2}')
            second = provenance("okx_funding_carry", path)["config_sha256"]
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
