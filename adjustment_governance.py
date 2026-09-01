#!/usr/bin/env python3
"""Proposal-only adjustment governance.

Automation (including the remote research node) may create and expire
adjustment proposals, but proposals never activate themselves. Actual strategy
parameter, wallet-pool, status, or provider-permission changes remain reviewed
Git changes recorded here as audit events.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "governance" / "adjustments"
PROPOSALS = DATA / "proposals"
DECISIONS = DATA / "decisions.jsonl"
STATUS = DATA / "status.json"
REGISTRY = ROOT / "strategy_registry.json"

ALLOWED_TRANSITIONS = {
    "proposed": {"validated", "rejected", "expired"},
    "validated": {"approved", "rejected", "expired"},
    "approved": {"activated", "superseded"},
    "activated": {"superseded", "reverted"},
    "rejected": set(),
    "expired": set(),
    "superseded": set(),
    "reverted": set(),
}

REQUIRED_FIELDS = (
    "proposal_id", "strategy_id", "strategy_version", "hypothesis", "evidence",
    "config_target", "changes", "cost_model", "test_results", "kill_criterion",
    "rollback_ref", "prior_config_sha256", "registry_sha256", "git_revision",
    "proposer", "created_at", "expires_at",
)

FORBIDDEN_CHANGE_FIELDS = {
    "status", "capital_allocation", "wallet_pool", "provider_permissions",
    "mode", "kill_switch", "paper_only",
}


def now_ms():
    return int(time.time() * 1000)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_decision(record):
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def proposal_hash(proposal):
    payload = json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def policy_for(registry, strategy_id):
    for strategy in registry.get("strategies", []):
        if strategy.get("strategy_id") == strategy_id:
            return strategy.get("adjustment_policy", {})
    raise ValueError(f"unknown strategy_id:{strategy_id}")


def validate(proposal, registry=None):
    """Validate a proposal against the registry's adjustment policy.

    Returns (normalized proposal, list of validation errors). A proposal with
    any error is never persisted as `proposed`.
    """
    errors = []
    registry = registry if registry is not None else json.loads(REGISTRY.read_text())
    try:
        policy = policy_for(registry, proposal.get("strategy_id"))
    except ValueError as exc:
        return proposal, [str(exc)]
    if policy.get("mode") != "proposal_only":
        errors.append("adjustment_policy_mode_not_proposal_only")
    for field in REQUIRED_FIELDS:
        if field not in proposal or proposal[field] in (None, ""):
            errors.append(f"missing_field:{field}")
    changes = proposal.get("changes") or {}
    if not changes:
        errors.append("empty_changes")
    for key in changes:
        if key in FORBIDDEN_CHANGE_FIELDS:
            errors.append(f"forbidden_change:{key}")
    adjustable = policy.get("adjustable_fields", {})
    for key, delta in changes.items():
        if key not in adjustable:
            errors.append(f"field_not_adjustable:{key}")
            continue
        bounds = adjustable[key]
        new_value = delta.get("new")
        if isinstance(new_value, (int, float)):
            if "min" in bounds and new_value < bounds["min"]:
                errors.append(f"value_below_min:{key}")
            if "max" in bounds and new_value > bounds["max"]:
                errors.append(f"value_above_max:{key}")
    if proposal.get("prior_config_sha256") and proposal.get("prior_config_sha256") == proposal.get("new_config_sha256"):
        errors.append("config_unchanged")
    return proposal, errors


def create(proposal):
    """Validate and persist a new proposal as `proposed`. Returns (record, errors)."""
    proposal, errors = validate(proposal, json.loads(REGISTRY.read_text()))
    if errors:
        return None, errors
    proposal = dict(proposal)
    proposal.setdefault("created_at", now_ms())
    if not proposal.get("expires_at"):
        proposal["expires_at"] = proposal["created_at"] + 14 * 86_400_000
    proposal["proposal_sha256"] = proposal_hash(proposal)
    proposal["state"] = "proposed"
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    (PROPOSALS / f"{proposal['proposal_id']}.json").write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2) + "\n")
    append_decision({"observed_at": now_ms(), "action": "proposed",
                     "proposal_id": proposal["proposal_id"],
                     "proposal_sha256": proposal["proposal_sha256"],
                     "strategy_id": proposal["strategy_id"], "proposer": proposal["proposer"]})
    refresh_status()
    return proposal, []


def transition(proposal_id, action, approver=None, note=None):
    """Record an audited state transition. Activation itself is a Git change;
    `activated` here only records that the reviewed change was deployed."""
    path = PROPOSALS / f"{proposal_id}.json"
    proposal = json.loads(path.read_text())
    current = proposal.get("state", "proposed")
    if action not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid_transition:{current}->{action}")
    if action in ("approved", "rejected", "activated", "reverted", "superseded") and not approver:
        raise ValueError("approver_required")
    proposal["state"] = action
    path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n")
    append_decision({"observed_at": now_ms(), "action": action, "proposal_id": proposal_id,
                     "proposal_sha256": proposal["proposal_sha256"],
                     "strategy_id": proposal["strategy_id"], "approver": approver, "note": note})
    refresh_status()
    return proposal


def expire_stale():
    now = now_ms()
    expired = []
    if PROPOSALS.exists():
        for path in sorted(PROPOSALS.glob("*.json")):
            proposal = json.loads(path.read_text())
            if proposal.get("state") == "proposed" and int(proposal.get("expires_at", 0)) < now:
                proposal["state"] = "expired"
                path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n")
                append_decision({"observed_at": now, "action": "expired",
                                 "proposal_id": proposal["proposal_id"],
                                 "proposal_sha256": proposal["proposal_sha256"],
                                 "strategy_id": proposal["strategy_id"]})
                expired.append(proposal["proposal_id"])
    if expired:
        refresh_status()
    return expired


def refresh_status():
    proposals = []
    if PROPOSALS.exists():
        for path in sorted(PROPOSALS.glob("*.json")):
            proposals.append(json.loads(path.read_text()))
    counts = {}
    for proposal in proposals:
        counts[proposal.get("state", "unknown")] = counts.get(proposal.get("state", "unknown"), 0) + 1
    status = {"updated_at": now_ms(), "mode": "proposal_only",
              "open_proposals": counts.get("proposed", 0) + counts.get("validated", 0),
              "state_counts": counts, "proposals": [
                  {k: p.get(k) for k in ("proposal_id", "strategy_id", "state", "created_at", "expires_at")}
                  for p in proposals]}
    atomic_json(STATUS, status)
    return status


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "expire":
        print(json.dumps({"expired": expire_stale()}, ensure_ascii=False))
    elif command == "status":
        print(json.dumps(refresh_status(), ensure_ascii=False, indent=2))
    else:
        raise SystemExit("usage: adjustment_governance.py [status|expire]")


if __name__ == "__main__":
    main()
