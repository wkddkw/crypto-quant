#!/usr/bin/env python3
"""Read-only strategy governance review. It never changes trading configuration."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REGISTRY = ROOT / "strategy_registry.json"
OUT = DATA / "governance"
REPORT = OUT / "report.md"
STATUS = OUT / "status.json"


def load_json(path):
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"read_error:{exc}"


def fixture_contamination():
    path = DATA / "carry" / "events.jsonl"
    if not path.exists():
        return False
    return any('"fixture"' in line for line in path.read_text().splitlines())


def strategy_evidence(strategy):
    strategy_id = strategy["strategy_id"]
    if strategy_id == "okx_funding_carry":
        account, error = load_json(DATA / "carry" / "account.json")
        return {"account_error": error, "positions": int(bool(account.get("spot_qty"))),
                "trades": len(account.get("trades", [])), "fixture_contamination": fixture_contamination()}
    if strategy_id == "polymarket_complete_set":
        account, error = load_json(DATA / "polymarket" / "account.json")
        return {"account_error": error, "markets_observed": account.get("markets_observed", 0),
                "invalid_historical_sample": True}
    if strategy_id == "gmgn_solana_copy":
        account, account_error = load_json(DATA / "gmgn_solana_paper" / "account.json")
        status, status_error = load_json(DATA / "gmgn_solana_paper" / "status.json")
        return {"account_error": account_error, "status_error": status_error,
                "trades": len(account.get("trades", [])), "equity": account.get("cash"),
                "upstream_status": status.get("status")}
    if strategy_id == "btc_trend_only_shadow":
        return {"paper_runner": "not_implemented", "reason": "existing paper account still runs v0_full"}
    return {"historical_baseline": True}


def review():
    registry = json.loads(REGISTRY.read_text())
    generated_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for strategy in registry["strategies"]:
        evidence = strategy_evidence(strategy)
        conclusion = strategy["status"]
        concerns = []
        if strategy["strategy_id"] == "okx_funding_carry" and evidence.get("fixture_contamination"):
            concerns.append("carry event ledger contains fixture records; do not treat it as performance evidence")
        if strategy["strategy_id"] == "polymarket_complete_set":
            concerns.append("historical observations were invalid market matches and do not count toward sample requirements")
        if strategy["strategy_id"] == "gmgn_solana_copy" and evidence.get("upstream_status") == "blocked_config":
            concerns.append("live GMGN mode remains blocked until an official API contract and credentials are configured")
        rows.append({"strategy_id": strategy["strategy_id"], "status": conclusion,
                     "review_due": strategy["review_due"], "evidence": evidence, "concerns": concerns,
                     "promotion_rules": strategy["promotion_rules"], "kill_rules": strategy["kill_rules"]})
    status = {"generated_at": generated_at, "strategies": rows, "paper_only": True}
    OUT.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    lines = [f"# 策略治理日报 {generated_at}", "", "本报告只评估证据和运行状态，不会调参、切换资金或下单。", ""]
    for row in rows:
        lines.extend([f"## {row['strategy_id']}", f"- 当前状态: **{row['status']}** | 下次复核: `{row['review_due']}`"])
        for concern in row["concerns"]:
            lines.append(f"- 风险: {concern}")
        lines.append(f"- 证据: `{json.dumps(row['evidence'], ensure_ascii=False)}`")
        lines.append("- 晋级条件: " + "；".join(row["promotion_rules"]))
        lines.append("- 暂停/淘汰条件: " + "；".join(row["kill_rules"]))
        lines.append("")
    REPORT.write_text("\n".join(lines) + "\n")
    return status


def main():
    if len(sys.argv) > 1 and sys.argv[1] != "review":
        raise SystemExit("usage: governance.py [review]")
    result = review()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
