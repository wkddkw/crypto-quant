#!/usr/bin/env python3
"""Generate a Beijing-time daily paper-strategy report from local ledgers."""
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from governance import review

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = DATA / "daily_reports"


def load_json(path):
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return {}, "文件不存在"
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"读取失败:{exc}"


def load_jsonl(path):
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()], None
    except FileNotFoundError:
        return [], "文件不存在"
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"读取失败:{exc}"


def day_bounds(day):
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=zone)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def in_day(rows, start, end, key="observed_at"):
    return [row for row in rows if start <= int(row.get(key, 0) or 0) <= end]


def strategy_summary(name, root, start, end):
    account, account_error = load_json(root / "account.json")
    status, status_error = load_json(root / "status.json")
    decisions, decisions_error = load_jsonl(root / "decisions.jsonl")
    events, events_error = load_jsonl(root / "events.jsonl")
    today_decisions = in_day(decisions, start, end)
    today_events = in_day(events, start, end)
    reasons = Counter(row.get("reason", "unknown") for row in today_decisions if row.get("action") != "open")
    accepted = sum(row.get("action") in ("open", "candidate") for row in today_decisions)
    return {
        "name": name,
        "status": status.get("status", account.get("status", "uninitialized")),
        "halt_reason": account.get("halt_reason") or status.get("halt_reason"),
        "equity": account.get("cash"),
        "initial_equity": account.get("initial_equity"),
        "open_positions": len(account.get("positions", {})) if isinstance(account.get("positions"), dict) else None,
        "signals": len(today_decisions),
        "accepted": accepted,
        "rejected": len(today_decisions) - accepted,
        "top_rejections": reasons.most_common(5),
        "today_events": len(today_events),
        "updated_at": status.get("updated_at") or account.get("last_snapshot_ts"),
        "errors": [error for error in (account_error, status_error, decisions_error, events_error) if error],
    }


def build(day=None):
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = day or now.date().isoformat()
    start, end = day_bounds(day)
    governance = review()
    summaries = [
        strategy_summary("BTC direction", DATA / "paper", start, end),
        strategy_summary("OKX funding carry", DATA / "carry", start, end),
        strategy_summary("Polymarket complete-set", DATA / "polymarket", start, end),
        strategy_summary("GMGN Solana smart money", DATA / "gmgn_solana_paper", start, end),
    ]
    payload = {"date_beijing": day, "generated_at": now.isoformat(), "paper_only": True,
               "strategies": summaries, "governance": governance}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{day}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    lines = [f"# 每日策略报告 {day}（北京时间）", "", "所有数据均为本地研究/纸面账本，不包含真实成交或资金。", ""]
    for item in summaries:
        equity = "-" if item["equity"] is None else f"${float(item['equity']):.2f}"
        base = "-" if item["initial_equity"] is None else f"${float(item['initial_equity']):.2f}"
        lines.extend([f"## {item['name']}", f"- 运行状态: **{item['status']}** | 暂停原因: `{item['halt_reason'] or '-'}`",
                      f"- 账本权益: {equity} / 初始 {base} | 未平仓: `{item['open_positions'] if item['open_positions'] is not None else '-'}`",
                      f"- 今日信号/接受/拒绝: `{item['signals']}` / `{item['accepted']}` / `{item['rejected']}` | 事件: `{item['today_events']}`",
                      f"- 主要拒绝原因: `{item['top_rejections'] or '-'}`", f"- 最新数据时间: `{item['updated_at'] or '-'}`"])
        if item["errors"]:
            lines.append("- 数据问题: " + "；".join(item["errors"]))
        lines.append("")
    lines.append("## 治理结论")
    for row in governance["strategies"]:
        lines.append(f"- `{row['strategy_id']}`: **{row['status']}**，下次复核 `{row['review_due']}`")
    report = "\n".join(lines) + "\n"
    (OUT / f"{day}.md").write_text(report)
    return payload, report


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else None
    _, report = build(day)
    print(report)


if __name__ == "__main__":
    main()
