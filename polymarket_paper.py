"""Polymarket paper research ledger. Never signs or submits orders."""
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "polymarket"
ACCOUNT = DATA / "account.json"
EVENTS = DATA / "events.jsonl"
REPORT = DATA / "report.md"
LOCK = DATA / ".run.lock"
CONFIG = json.loads((ROOT / "polymarket_config.json").read_text())


def now_ms():
    return int(time.time() * 1000)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_account():
    if ACCOUNT.exists():
        return json.loads(ACCOUNT.read_text())
    return {"schema": 1, "created_at": now_ms(), "initial_equity": CONFIG["initial_equity"],
            "cash": CONFIG["initial_equity"], "reserved_cash": 0.0,
            "status": "observe_only", "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "fees": 0.0, "markets_observed": 0, "candidates": 0, "rejected": 0,
            "unresolved": 0, "last_run_at": 0, "last_observation_at": 0, "equity_history": []}


def append_event(value):
    DATA.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


@contextmanager
def run_lock():
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("polymarket research run already in progress")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        LOCK.unlink(missing_ok=True)


def run_once():
    a = load_account()
    status_path = DATA / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {"markets_observed": 0, "opportunities": 0, "errors": []}
    run_at = int(status.get("updated_at", 0))
    if run_at and run_at != int(a.get("last_observation_at", 0)):
        a["markets_observed"] += int(status.get("markets_observed", 0))
        a["candidates"] += int(status.get("opportunities", 0))
        a["rejected"] += max(0, int(status.get("markets_observed", 0)) - int(status.get("opportunities", 0)))
        a["last_observation_at"] = run_at
    a["last_run_at"] = now_ms()
    atomic_json(ACCOUNT, a)
    append_event({"event_id": f"run:{a['last_run_at']}", "type": "observation", **status})
    lines = ["# Polymarket 15 分钟纸面研究", "",
             "- 模式：只读盘口观察 + complete-set 机会记录；未连接钱包、未签名、未下单。",
             f"- 状态：**{a['status']}** | 观察市场累计 {a['markets_observed']} | 候选机会累计 {a['candidates']}",
             f"- 纸面权益 ${a['cash']:.2f} | 已实现 PnL ${a['realized_pnl']:+.2f} | 未实现 PnL ${a['unrealized_pnl']:+.2f}",
             "- 候选仅代表扣除固定费用、滑点和 Gas 后仍可能为正的观察样本，不代表可成交或无风险收益。",
             "- Temporal Arb 当前关闭；单腿库存不会计入无风险套利收益。",
             "", "## 最近运行", f"- 本轮观察 {status.get('markets_observed', 0)} 个市场 | 候选 {status.get('opportunities', 0)} 个 | 错误 {len(status.get('errors', []))} 个",
             "", "## 验收标准", "- 至少积累 500–1000 个市场，并统计可成交深度、完整成交率、单腿暴露、延迟和扣成本净 PnL 后再评估。"]
    REPORT.write_text("\n".join(lines) + "\n")
    return a


def main():
    with run_lock():
        print(json.dumps(run_once(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
