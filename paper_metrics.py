"""Read-only metrics for BTC paper accounts, including legacy embedded records."""
from datetime import datetime, timezone


def timestamp(row):
    value = row.get("observed_at") or row.get("ts")
    if isinstance(value, (int, float)):
        return int(value)
    if value:
        return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)
    return None


def drawdown(values, initial):
    peak, worst = initial, 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def metrics(account):
    initial = account.get("initial_equity", 500.0)
    history = account.get("equity_history", [])
    equity = history[-1]["equity"] if history else account.get("cash", initial)
    trades = account.get("trades", [])
    result = {"net_pnl": equity - initial, "net_return": equity / initial - 1,
              "max_drawdown": drawdown([r["equity"] for r in history], initial),
              "trade_count": len(trades), "observations": len(history),
              "fees": account.get("fees", sum(t.get("usd", 0) * 0.001 for t in trades)),
              "fees_estimated": "fees" not in account,
              "slippage_cost": account.get("slippage_cost"),
              "created_at": account.get("created_at") or timestamp({"ts": account.get("created")}),
              "updated_at": account.get("updated_at") or (timestamp(history[-1]) if history else None)}
    if history and "benchmark_equity" in history[-1]:
        benchmark = history[-1]["benchmark_equity"]
        result.update(benchmark_equity=benchmark, benchmark_return=benchmark / initial - 1,
                      excess_return=(equity - benchmark) / initial,
                      benchmark_max_drawdown=drawdown([r["benchmark_equity"] for r in history], initial))
        daily = account.get("daily_observations", [])
        intervals = sum(b["candle_ts"] - a["candle_ts"] == 86_400_000
                        and 22 * 3600_000 <= b["observed_at"] - a["observed_at"] <= 26 * 3600_000
                        for a, b in zip(daily, daily[1:]))
        result["complete_daily_intervals"] = intervals
        result["required_daily_intervals"] = 84
        result["daily_gaps"] = max(0, len(daily) - 1 - intervals)
        result["earliest_review_at"] = account["created_at"] + 84 * 86_400_000
    return result
