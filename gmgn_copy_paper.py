#!/usr/bin/env python3
"""Solana smart-money paper ledger driven by a GMGN-compatible read-only feed.

The script has no wallet, signing, or order-submission code. Live mode is blocked
until an official GMGN API contract is configured.
"""
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from gmgn_adapter import AdapterBlocked, list_trade_events, rank_wallets
from gmgn_models import ValidationError

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "gmgn_solana_paper"
ACCOUNT = DATA / "account.json"
EVENTS = DATA / "events.jsonl"
DECISIONS = DATA / "decisions.jsonl"
SIGNALS = DATA / "signals.jsonl"
STATUS = DATA / "status.json"
REPORT = DATA / "report.md"
RANKINGS = DATA / "rankings"
LOCK = DATA / ".run.lock"
CONFIG = json.loads((ROOT / "gmgn_config.json").read_text())


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


def append_jsonl(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


@contextmanager
def run_lock():
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("gmgn paper run already in progress")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        LOCK.unlink(missing_ok=True)


def load_account():
    if ACCOUNT.exists():
        return json.loads(ACCOUNT.read_text())
    equity = CONFIG["paper"]["initial_equity_usd"]
    return {
        "schema": 1,
        "created_at": now_ms(),
        "initial_equity": equity,
        "cash": equity,
        "status": "flat",
        "halt_reason": None,
        "positions": {},
        "processed_event_ids": [],
        "wallet_pool_date": None,
        "wallet_pool_size": 0,
        "fees": 0.0,
        "trades": [],
        "equity_history": [],
    }


def beijing_day(ts_ms=None):
    ts = now_ms() if ts_ms is None else ts_ms
    return datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def frozen_wallet_pool(day):
    path = RANKINGS / f"{day}.json"
    if path.exists():
        snapshot = json.loads(path.read_text())
        return snapshot, False
    wallets = rank_wallets()
    snapshot = {
        "schema": 1,
        "source": "gmgn",
        "chain": "solana",
        "selected_at": now_ms(),
        "beijing_day": day,
        "complete": len(wallets) == CONFIG["universe"]["daily_rank_limit"],
        "wallets": [wallet.__dict__ for wallet in wallets],
    }
    atomic_json(path, snapshot)
    append_jsonl(EVENTS, {"type": "ranking_snapshot", **snapshot})
    return snapshot, True


def exposure(account, key, value):
    return sum(position["notional"] for position in account["positions"].values() if position.get(key) == value)


def total_exposure(account):
    return sum(position["notional"] for position in account["positions"].values())


def equity(account):
    marked = sum(position["qty"] * position["mark_price"] for position in account["positions"].values())
    return account["cash"] + marked


def decision(account, event, eligible_wallets, observed_at):
    paper = CONFIG["paper"]
    if paper["kill_switch"]:
        return "reject", "kill_switch"
    if event.event_id in set(account["processed_event_ids"]):
        return "reject", "duplicate_event"
    if event.chain != "solana":
        return "reject", "non_solana"
    if event.wallet_address not in eligible_wallets:
        return "reject", "wallet_not_in_frozen_top100"
    if event.side != "buy":
        return "reject", "sell_disabled" if event.side == "sell" else "unsupported_side"
    age_ms = observed_at - event.executed_at
    if age_ms < 0:
        return "reject", "future_event"
    if age_ms > paper["max_event_age_sec"] * 1000:
        return "reject", "stale_event"
    if event.price_usd is None or event.price_usd <= 0:
        return "reject", "missing_price"
    if event.trade_usd < paper["min_trade_usd"]:
        return "reject", "trade_below_minimum"
    if event.liquidity_usd is None or event.liquidity_usd < paper["min_liquidity_usd"]:
        return "reject", "liquidity_below_minimum"
    if len(account["positions"]) >= paper["max_open_positions"]:
        return "reject", "max_open_positions"
    for trade in reversed(account["trades"]):
        if trade["wallet_address"] == event.wallet_address and trade["asset_mint"] == event.asset_mint:
            if observed_at - trade["observed_at"] < paper["cooldown_sec"] * 1000:
                return "reject", "cooldown_active"
            break
    account_equity = equity(account)
    notional = min(account_equity * paper["max_trade_pct"], account["cash"])
    if notional <= 0:
        return "reject", "insufficient_cash"
    limits = [
        (exposure(account, "wallet_address", event.wallet_address) + notional, account_equity * paper["max_wallet_exposure_pct"], "wallet_exposure_limit"),
        (exposure(account, "asset_mint", event.asset_mint) + notional, account_equity * paper["max_asset_exposure_pct"], "asset_exposure_limit"),
        (total_exposure(account) + notional, account_equity * paper["max_total_exposure_pct"], "total_exposure_limit"),
    ]
    for actual, maximum, reason in limits:
        if actual > maximum + 1e-9:
            return "reject", reason
    return "open", "eligible"


def open_position(account, event, observed_at):
    paper = CONFIG["paper"]
    notional = min(equity(account) * paper["max_trade_pct"], account["cash"])
    fill_price = event.price_usd * (1 + paper["slippage_pct"])
    fee = notional * paper["fee_pct"] + paper["gas_usd"]
    qty = notional / fill_price
    position_id = f"{event.event_id}:{event.asset_mint}"
    account["cash"] -= notional + fee
    account["fees"] += fee
    account["positions"][position_id] = {
        "position_id": position_id,
        "event_id": event.event_id,
        "wallet_address": event.wallet_address,
        "asset_mint": event.asset_mint,
        "asset_symbol": event.asset_symbol,
        "opened_at": observed_at,
        "notional": notional,
        "qty": qty,
        "entry_price": fill_price,
        "mark_price": fill_price,
        "fee": fee,
    }
    trade = {
        "event_id": event.event_id,
        "wallet_address": event.wallet_address,
        "asset_mint": event.asset_mint,
        "asset_symbol": event.asset_symbol,
        "observed_at": observed_at,
        "action": "open",
        "notional": notional,
        "source_price": event.price_usd,
        "paper_fill_price": fill_price,
        "fee": fee,
        "latency_ms": observed_at - event.executed_at,
    }
    account["trades"].append(trade)
    append_jsonl(EVENTS, {"type": "execution", **trade})


def write_report(account, status):
    current_equity = equity(account)
    lines = [
        f"# GMGN Solana 聪明钱纸面跟随 {datetime.now(timezone.utc):%F %H:%M} UTC",
        "",
        "## 运行边界",
        "- 仅使用 GMGN 兼容的只读排名与钱包交易事件；不连接钱包、不签名、不下单。",
        f"- 模式 `{CONFIG['mode']}` | 状态 **{account['status']}** | 原因 `{account.get('halt_reason') or '-'}`",
        "",
        "## 账本",
        f"- 权益 **${current_equity:.2f}** / 初始 ${account['initial_equity']:.2f} | 累计 {current_equity / account['initial_equity'] - 1:+.2%}",
        f"- 现金 ${account['cash']:.2f} | 未平仓 {len(account['positions'])} | 费用 ${account['fees']:.2f}",
        f"- 当日冻结钱包池 {account.get('wallet_pool_size', 0)} | 信号 {status.get('signals', 0)} | 接受 {status.get('accepted', 0)} | 拒绝 {status.get('rejected', 0)}",
    ]
    REPORT.write_text("\n".join(lines) + "\n")
    return "\n".join(lines)


def run_once(observed_at=None):
    observed_at = now_ms() if observed_at is None else observed_at
    account = load_account()
    day = beijing_day(observed_at)
    try:
        pool, created = frozen_wallet_pool(day)
        wallets = {row["wallet_address"] for row in pool["wallets"]}
        account["wallet_pool_date"] = day
        account["wallet_pool_size"] = len(wallets)
        status = {"updated_at": observed_at, "mode": CONFIG["mode"], "status": "ok", "pool_created": created,
                  "pool_complete": pool["complete"], "signals": 0, "accepted": 0, "rejected": 0, "errors": []}
        if CONFIG["paper"]["kill_switch"]:
            account["status"] = "halt"
            account["halt_reason"] = "kill_switch"
        else:
            account["status"] = "open" if account["positions"] else "flat"
            account["halt_reason"] = None
        cursor = max((trade.get("observed_at", 0) for trade in account["trades"]), default=0)
        for wallet in sorted(wallets):
            for event in list_trade_events(wallet, since_ms=0):
                status["signals"] += 1
                append_jsonl(SIGNALS, {"observed_at": observed_at, **event.as_dict()})
                action, reason = decision(account, event, wallets, observed_at)
                result = {"observed_at": observed_at, "event_id": event.event_id, "wallet_address": event.wallet_address,
                          "asset_mint": event.asset_mint, "action": action, "reason": reason,
                          "latency_ms": observed_at - event.executed_at}
                append_jsonl(DECISIONS, result)
                account["processed_event_ids"].append(event.event_id)
                if action == "open":
                    open_position(account, event, observed_at)
                    status["accepted"] += 1
                else:
                    status["rejected"] += 1
        account["processed_event_ids"] = account["processed_event_ids"][-20000:]
    except (AdapterBlocked, ValidationError, RuntimeError) as exc:
        account["status"] = "halt"
        account["halt_reason"] = str(exc)
        status = {"updated_at": observed_at, "mode": CONFIG["mode"], "status": "blocked_config" if str(exc).startswith("blocked_config") else "upstream_error",
                  "signals": 0, "accepted": 0, "rejected": 0, "errors": [str(exc)]}
    current_equity = equity(account)
    account["equity_history"].append({"ts": observed_at, "equity": current_equity, "status": account["status"]})
    account["equity_history"] = account["equity_history"][-5000:]
    atomic_json(ACCOUNT, account)
    atomic_json(STATUS, status)
    write_report(account, status)
    return status


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "init":
        if ACCOUNT.exists():
            print("GMGN paper 账户已存在，不覆盖")
        else:
            atomic_json(ACCOUNT, load_account())
            print("GMGN Solana paper 账户已初始化")
        return
    if command == "report":
        print(REPORT.read_text() if REPORT.exists() else "尚无 GMGN paper 报告，请先运行 run")
        return
    if command == "status":
        print(json.dumps(json.loads(STATUS.read_text()) if STATUS.exists() else {"status": "uninitialized"}, ensure_ascii=False, indent=2))
        return
    if command != "run":
        raise SystemExit("usage: gmgn_copy_paper.py [init|run|report|status]")
    with run_lock():
        print(json.dumps(run_once(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
