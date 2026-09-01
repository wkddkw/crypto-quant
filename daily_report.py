#!/usr/bin/env python3
"""Generate Beijing-time daily paper-strategy reports and half-day sync packages."""
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from governance import review
from runtime_provenance import git_revision

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = DATA / "daily_reports"
SYNC = DATA / "sync"
DELIVERY_AUDIT = DATA / "governance" / "delivery_audit.jsonl"
SLOTS = ("0600", "1800")


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


def latest_btc_mark(account):
    """Return the latest paper mark written by paper_trader's live-price cycle."""
    for row in reversed(account.get("equity_history", [])):
        try:
            return float(row["equity"])
        except (KeyError, TypeError, ValueError):
            continue
    cash = account.get("cash")
    return None if cash is None else float(cash)


def strategy_summary(name, strategy_id, root, start, end):
    account, account_error = load_json(root / "account.json")
    status, status_error = load_json(root / "status.json")
    decisions, decisions_error = load_jsonl(root / "decisions.jsonl")
    events, events_error = load_jsonl(root / "events.jsonl")
    today_decisions = in_day(decisions, start, end)
    today_events = in_day(events, start, end)
    reasons = Counter(row.get("reason", "unknown") for row in today_decisions if row.get("action") != "open")
    accepted = sum(row.get("action") in ("open", "candidate") for row in today_decisions)
    cash = account.get("cash")
    equity = latest_btc_mark(account) if name == "BTC direction" else cash
    position_value = None
    if equity is not None and cash is not None:
        position_value = float(equity) - float(cash)
    return {
        "name": name,
        "strategy_id": strategy_id,
        "status": status.get("status", account.get("status", "uninitialized")),
        "halt_reason": account.get("halt_reason") or status.get("halt_reason"),
        "cash": cash,
        "position_value": position_value,
        "equity": equity,
        "initial_equity": account.get("initial_equity", 500.0 if name == "BTC direction" and account else None),
        "open_positions": len(account.get("positions", {})) if isinstance(account.get("positions"), dict) else None,
        "signals": len(today_decisions),
        "accepted": accepted,
        "rejected": len(today_decisions) - accepted,
        "top_rejections": reasons.most_common(5),
        "today_events": len(today_events),
        "updated_at": status.get("updated_at") or account.get("last_snapshot_ts"),
        "errors": [error for error in (account_error, status_error, decisions_error, events_error) if error],
    }


def build(day=None, slot=None):
    """Build a report. With `slot` (0600/1800) this writes immutable
    slot-specific snapshots and a sync package; otherwise it writes the
    legacy date-keyed daily report."""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = day or now.date().isoformat()
    start, end = day_bounds(day)
    governance = review()
    summaries = [
        strategy_summary("BTC direction", "btc_v0_full", DATA / "paper", start, end),
        strategy_summary("OKX funding carry", "okx_funding_carry", DATA / "carry", start, end),
        strategy_summary("Polymarket complete-set", "polymarket_complete_set", DATA / "polymarket", start, end),
        strategy_summary("GMGN Solana smart money", "gmgn_solana_copy", DATA / "gmgn_solana_paper", start, end),
    ]
    payload = {"date_beijing": day, "generated_at": now.isoformat(), "paper_only": True,
               "git_revision": git_revision(), "strategies": summaries, "governance": governance}
    report = render(payload)
    OUT.mkdir(parents=True, exist_ok=True)
    if slot is None:
        (OUT / f"{day}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        (OUT / f"{day}.md").write_text(report)
        return payload, report
    if slot not in SLOTS:
        raise ValueError(f"invalid slot:{slot}")
    stamp = f"{day}T{slot}+0800"
    (OUT / f"{stamp}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    (OUT / f"{stamp}.md").write_text(report)
    sync_package(stamp, payload, report)
    return payload, report


def render(payload):
    day = payload["date_beijing"]
    summaries = payload["strategies"]
    lines = [f"# 每日策略报告 {day}（北京时间）", "", "所有数据均为本地研究/纸面账本，不包含真实成交或资金。", ""]
    for item in summaries:
        equity = "-" if item["equity"] is None else f"${float(item['equity']):.2f}"
        base = "-" if item["initial_equity"] is None else f"${float(item['initial_equity']):.2f}"
        cash = "-" if item["cash"] is None else f"${float(item['cash']):.2f}"
        position_value = "-" if item["position_value"] is None else f"${float(item['position_value']):.2f}"
        lines.extend([f"## {item['name']}", f"- 运行状态: **{item['status']}** | 暂停原因: `{item['halt_reason'] or '-'}`",
                      f"- 账本权益: {equity} / 初始 {base} | 现金: {cash} | 持仓市值: {position_value} | 未平仓: `{item['open_positions'] if item['open_positions'] is not None else '-'}`",
                      f"- 今日信号/接受/拒绝: `{item['signals']}` / `{item['accepted']}` / `{item['rejected']}` | 事件: `{item['today_events']}`",
                      f"- 主要拒绝原因: `{item['top_rejections'] or '-'}`", f"- 最新数据时间: `{item['updated_at'] or '-'}`"])
        if item["errors"]:
            lines.append("- 数据问题: " + "；".join(item["errors"]))
        lines.append("")
    lines.append("## 治理结论")
    for row in payload["governance"]["strategies"]:
        lines.append(f"- `{row['strategy_id']}`: **{row['status']}**，下次复核 `{row['review_due']}`")
    report = "\n".join(lines) + "\n"
    return report


def strategy_label(strategy_id, governance):
    for row in governance.get("strategies", []):
        if row.get("strategy_id") == strategy_id:
            return row.get("status"), row.get("review_due")
    return None, None


def sync_package(stamp, payload, report_text):
    """Write the half-day sync package consumed by the owner and the local session."""
    SYNC.mkdir(parents=True, exist_ok=True)
    protection_events = []
    proposals_path = DATA / "governance" / "adjustments" / "status.json"
    proposals = json.loads(proposals_path.read_text()) if proposals_path.exists() else None
    package = {
        "sync_id": stamp,
        "generated_at": payload["generated_at"],
        "git_revision": payload["git_revision"],
        "report_md_sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
        "paper_only": True,
        "strategies": payload["strategies"],
        "governance": payload["governance"],
        "runtime_protection_events": protection_events,
        "adjustment_proposals": proposals,
    }
    md = [f"# 半日同步包 {stamp}（北京时间）", "",
          f"- Git revision: `{payload['git_revision']}`",
          f"- 报告哈希: `{package['report_md_sha256'][:16]}…`",
          "- 本包仅包含研究与纸面账本数据；未连接钱包、未签名、未提交真实交易。",
          ""]
    for item in payload["strategies"]:
        equity = "-" if item["equity"] is None else f"${float(item['equity']):.2f}"
        md.append(f"- {item['name']} (`{item['strategy_id']}`): 状态 **{item['status']}**, 权益 {equity}, "
                  f"信号 {item['signals']}/接受 {item['accepted']}/拒绝 {item['rejected']}")
    md.append("")
    md.append("## 调整提案")
    if proposals and proposals.get("open_proposals"):
        for row in proposals.get("proposals", []):
            md.append(f"- `{row.get('proposal_id')}` 策略 `{row.get('strategy_id')}` 状态 **{row.get('state')}**")
    else:
        md.append("- 无待处理提案。")
    (SYNC / f"{stamp}.json").write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n")
    (SYNC / f"{stamp}.md").write_text("\n".join(md) + "\n")
    return package


def record_delivery(channel, sync_id, outcome, message_id=None, error_class=None):
    """Append-only delivery audit. Never store tokens, webhooks, or chat IDs."""
    DELIVERY_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    record = {"delivered_at": datetime.now(timezone.utc).isoformat(), "channel_alias": channel,
              "sync_id": sync_id, "outcome": outcome, "message_id": message_id,
              "error_class": error_class}
    with DELIVERY_AUDIT.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return record


def main():
    day = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else None
    slot = None
    if "--slot" in sys.argv:
        slot = sys.argv[sys.argv.index("--slot") + 1]
    _, report = build(day, slot)
    print(report)


if __name__ == "__main__":
    main()
