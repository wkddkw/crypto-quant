import json
import tempfile
import unittest
from pathlib import Path

import adjustment_governance as gov
from runtime_provenance import canonical_hash


def base_proposal():
    return {
        "proposal_id": "prop-test-001",
        "strategy_id": "okx_funding_carry",
        "strategy_version": "carry_paper_v1",
        "hypothesis": "Raising min net edge avoids marginal opens.",
        "evidence": {"window_days": 30, "observations": 30,
                     "cost_model": "conservative", "result_type": "paper"},
        "config_target": "carry_config.json",
        "changes": {"min_net_edge_pct": {"old": 0.02, "new": 0.03}},
        "cost_model": "round_trip 0.3% + basis buffer",
        "test_results": {"suite": "ok", "replay": "ok"},
        "kill_criterion": "Paper round trips turn non-positive.",
        "rollback_ref": "git revert <change-commit>",
        "prior_config_sha256": "a" * 64,
        "registry_sha256": "b" * 64,
        "git_revision": "c" * 40,
        "proposer": "grok-bot",
        "created_at": 1700000000000,
        "expires_at": 1700000000000 + 14 * 86_400_000,
    }


def registry():
    reg = json.loads(Path("strategy_registry.json").read_text())
    for strategy in reg["strategies"]:
        if strategy["strategy_id"] == "okx_funding_carry":
            policy = strategy["adjustment_policy"]
            policy["mode"] = "proposal_only"
            policy["config_target"] = "carry_config.json"
            policy["adjustable_fields"] = {"min_net_edge_pct": {"min": 0.02, "max": 0.05}}
    return reg


class AdjustmentGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.old = (gov.DATA, gov.PROPOSALS, gov.DECISIONS, gov.STATUS, gov.REGISTRY)
        gov.DATA = base
        gov.PROPOSALS = base / "proposals"
        gov.DECISIONS = base / "decisions.jsonl"
        gov.STATUS = base / "status.json"
        reg_path = base / "registry.json"
        reg_path.write_text(json.dumps(registry()))
        gov.REGISTRY = reg_path

    def tearDown(self):
        (gov.DATA, gov.PROPOSALS, gov.DECISIONS, gov.STATUS, gov.REGISTRY) = self.old
        self.tmp.cleanup()

    def test_valid_proposal_is_persisted_as_proposed(self):
        proposal, errors = gov.create(base_proposal())
        self.assertEqual(errors, [])
        self.assertEqual(proposal["state"], "proposed")
        self.assertTrue(gov.DECISIONS.exists())

    def test_forbidden_change_field_is_rejected(self):
        proposal = base_proposal()
        proposal["changes"]["status"] = {"old": "observe_only", "new": "shadow"}
        _, errors = gov.create(proposal)
        self.assertIn("forbidden_change:status", errors)

    def test_value_out_of_bounds_is_rejected(self):
        reg = registry()
        reg["strategies"][2]["adjustment_policy"]["adjustable_fields"] = {
            "min_net_edge_pct": {"min": 0.02, "max": 0.05}}
        proposal = base_proposal()
        proposal["changes"]["min_net_edge_pct"] = {"old": 0.02, "new": 0.10}
        _, errors = gov.validate(proposal, reg)
        self.assertIn("value_above_max:min_net_edge_pct", errors)

    def test_field_not_adjustable_is_rejected(self):
        _, errors = gov.validate(base_proposal(), json.loads(Path("strategy_registry.json").read_text()))
        self.assertIn("field_not_adjustable:min_net_edge_pct", errors)

    def test_transition_requires_approver_and_valid_path(self):
        proposal, _ = gov.create(base_proposal())
        with self.assertRaises(ValueError):
            gov.transition(proposal["proposal_id"], "approved")
        gov.transition(proposal["proposal_id"], "validated")
        gov.transition(proposal["proposal_id"], "approved", approver="owner")
        with self.assertRaises(ValueError):
            gov.transition(proposal["proposal_id"], "expired")

    def test_expire_stale_marks_proposed_records(self):
        proposal, _ = gov.create(base_proposal())
        expired = gov.expire_stale()
        self.assertEqual(expired, [proposal["proposal_id"]])
        again = gov.expire_stale()
        self.assertEqual(again, [])

    def test_canonical_hash_is_deterministic(self):
        value = {"b": 1, "a": [1, 2]}
        self.assertEqual(canonical_hash(value), canonical_hash({"a": [1, 2], "b": 1}))


if __name__ == "__main__":
    unittest.main()
