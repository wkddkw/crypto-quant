#!/usr/bin/env python3
"""Offline, frozen-input research. Never imports runners or writes paper ledgers."""
import argparse
import hashlib
import io
import json
import math
import platform
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow

from runtime_provenance import canonical_hash, git_revision

ROOT = Path(__file__).resolve().parent
DAY_MS = 86_400_000
WARMUP = 201
ROLLING_CANDIDATES = ("buy_hold", "trend_only", "ma200", "trend_vol_cap")
FIXED_START_CANDIDATES = ("buy_hold", "trend_only", "trend_only_scale_50", "trend_vol_cap")
# Backward-compatible alias used by older tests/imports.
CANDIDATES = ROLLING_CANDIDATES


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def research_mode(config):
    return config.get("mode", "rolling")


def validate_config(config):
    mode = research_mode(config)
    if config.get("schema") != 1:
        raise ValueError("unsupported_schema")
    if mode not in ("rolling", "fixed_start"):
        raise ValueError("unsupported_mode")
    expected = FIXED_START_CANDIDATES if mode == "fixed_start" else ROLLING_CANDIDATES
    if tuple(config["candidates"]) != expected:
        raise ValueError("unsupported_candidates_or_schema")
    if not config["assets"] or len(set(config["assets"])) != len(config["assets"]):
        raise ValueError("invalid_assets")
    if not set(config["assets"]) <= {"BTC-USDT", "ETH-USDT"}:
        raise ValueError("unsupported_asset")
    for name in ("initial_equity", "fee", "slippage", "rebalance_threshold", "min_notional"):
        value = config[name]
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid_config:{name}")
    if config["initial_equity"] <= 0 or config["fee"] >= 1 or config["rebalance_threshold"] > 1:
        raise ValueError("invalid_cost_or_capital")
    if config["volatility_cap"]["lookback_days"] != 30:
        raise ValueError("unsupported_vol_cap_lookback")
    if not 0 < config["volatility_cap"]["annual_target"] <= 1:
        raise ValueError("invalid_volatility_target")
    if "base" not in config["scenarios"]:
        raise ValueError("missing_base_scenario")
    if config["scenarios"]["base"] != {"slippage_multiplier": 1, "signal_delay_days": 0}:
        raise ValueError("base_must_use_configured_costs_and_next_open")
    for name, scenario in config["scenarios"].items():
        if not name.replace("_", "").isalnum():
            raise ValueError("invalid_scenario_name")
        if scenario["signal_delay_days"] not in (0, 1):
            raise ValueError("unsupported_signal_delay")
        if not 1 <= scenario["slippage_multiplier"] <= 10 or config["slippage"] * scenario["slippage_multiplier"] >= 1:
            raise ValueError("invalid_slippage_multiplier")
    if mode == "rolling":
        if config["training_years"] != 3:
            raise ValueError("unsupported_research_window")
        start = pd.Timestamp(config["validation_start"], tz="UTC")
        if start.month != 1 or start.day != 1:
            raise ValueError("validation_start_must_be_january_1")
    else:
        starts = config.get("validation_starts")
        if not starts or len(set(starts)) != len(starts):
            raise ValueError("invalid_validation_starts")
        for start_str in starts:
            start = pd.Timestamp(start_str, tz="UTC")
            if start.month != 1 or start.day != 1:
                raise ValueError("validation_start_must_be_january_1")


def load_candles(raw, asset, now_ms):
    frame = pd.read_parquet(io.BytesIO(raw))
    columns = {"ts", "inst", "open", "high", "low", "close"}
    if not columns <= set(frame.columns):
        raise ValueError(f"missing_columns:{asset}")
    ts = pd.to_numeric(frame["ts"], errors="raise")
    if not np.isfinite(ts).all() or (ts < 0).any() or (ts % DAY_MS != 0).any():
        raise ValueError(f"invalid_utc_timestamps:{asset}")
    frame = frame.assign(ts=ts.astype("int64")).sort_values("ts")
    excluded = int((frame["ts"] + DAY_MS > now_ms).sum())
    frame = frame[frame["ts"] + DAY_MS <= now_ms].reset_index(drop=True)
    if len(frame) <= WARMUP or frame["ts"].diff().dropna().ne(DAY_MS).any():
        raise ValueError(f"missing_or_duplicate_daily_candles:{asset}")
    if not frame["inst"].eq(asset).all():
        raise ValueError(f"instrument_mismatch:{asset}")
    for col in ("open", "high", "low", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="raise").astype(float)
    prices = frame[["open", "high", "low", "close"]]
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError(f"invalid_prices:{asset}")
    if (frame["high"] < prices.max(axis=1)).any() or (frame["low"] > prices.min(axis=1)).any():
        raise ValueError(f"invalid_ohlc_range:{asset}")
    age = max(0, now_ms - int(frame["ts"].iloc[-1]) - DAY_MS)
    return frame, {"rows": len(frame), "first_candle_ts": int(frame["ts"].iloc[0]),
                   "last_candle_ts": int(frame["ts"].iloc[-1]), "excluded_unclosed": excluded,
                   "age_since_last_close_hours": age / 3600_000,
                   "stale_for_live_use": age > DAY_MS, "historical_only": True}


def targets(frame, config):
    close = frame["close"]
    ma50, ma200 = close.rolling(50).mean(), close.rolling(200).mean()
    trend = np.where(close > ma200, 2.0, -2.0) + np.where(close > ma50, 1.0, -1.0)
    vol = np.log(close).diff().rolling(90).std(ddof=1) * np.sqrt(365)
    momentum = (close.pct_change(90) / vol.replace(0, np.nan) * 2).clip(-2, 2).fillna(0)
    breakout = (close >= frame["high"].rolling(20).max().shift(1)).astype(float)
    signal = ((trend + momentum + breakout) / 6).clip(0, 1)
    rv = np.log(close).diff().rolling(config["volatility_cap"]["lookback_days"]).std(ddof=1) * np.sqrt(365)
    scale = (config["volatility_cap"]["annual_target"] / rv.replace(0, np.nan)).clip(upper=1).fillna(1)
    result = pd.DataFrame({
        "buy_hold": 1.0,
        "trend_only": signal,
        "ma200": (close > ma200).astype(float),
        "trend_vol_cap": signal * scale,
        "trend_only_scale_50": signal * 0.5,
    })
    result.iloc[:WARMUP - 1] = np.nan
    return result


def bias_checks(frame, config):
    """Compare prefix-only and bounded warmup computations to the full series."""
    full = targets(frame, config)
    columns = list(config["candidates"])
    points = sorted(set(np.linspace(WARMUP - 1, len(frame) - 1, min(24, len(frame) - WARMUP + 1), dtype=int)))
    prefix_error = warmup_error = 0.0
    for end in points:
        expected = full.iloc[end][columns].to_numpy()
        prefix = targets(frame.iloc[:end + 1].reset_index(drop=True), config).iloc[-1][columns].to_numpy()
        bounded = targets(frame.iloc[end - WARMUP + 1:end + 1].reset_index(drop=True), config).iloc[-1][columns].to_numpy()
        if not all(np.isfinite(values).all() for values in (expected, prefix, bounded)):
            return {"sampled_points": len(points), "prefix_max_error": None, "warmup_max_error": None,
                    "passed": False, "reason": "nonfinite_indicator_in_prefix_or_warmup"}
        prefix_error = max(prefix_error, float(np.max(np.abs(prefix - expected))))
        warmup_error = max(warmup_error, float(np.max(np.abs(bounded - expected))))
    return {"sampled_points": len(points), "prefix_max_error": prefix_error,
            "warmup_max_error": warmup_error, "passed": prefix_error < 1e-10 and warmup_error < 1e-10,
            "limitation": "sampled indicator checks, not proof over every possible execution path"}


def simulate(frame, weights, start, end, config, scenario):
    """Daily target rebalancing at the next open; final value is marked, not liquidated."""
    cash, qty = config["initial_equity"], 0.0
    fee_rate = config["fee"]
    slip = config["slippage"] * scenario["slippage_multiplier"]
    delay = scenario["signal_delay_days"]
    marks, trades = [], []
    arrays = {col: frame[col].to_numpy() for col in ("ts", "open", "close")}
    values = np.asarray(weights)
    for i in range(start, end):
        signal_index = i - 1 - delay
        want = float(values[signal_index]) if signal_index >= WARMUP - 1 else 0.0
        if not math.isfinite(want) or not 0 <= want <= 1:
            raise ValueError("invalid_target_weight")
        price = float(arrays["open"][i])
        equity_open = cash + qty * price
        weight = qty * price / equity_open
        fees = slippage_cost = 0.0
        if abs(want - weight) >= config["rebalance_threshold"]:
            difference = want * equity_open - qty * price
            buy = difference > 0
            fill = price * (1 + slip if buy else 1 - slip)
            amount = min(abs(difference) / price, cash / (fill * (1 + fee_rate)) if buy else qty)
            if amount > 0 and amount * fill >= config["min_notional"]:
                fees = amount * fill * fee_rate
                slippage_cost = amount * abs(fill - price)
                cash += -amount * fill - fees if buy else amount * fill - fees
                qty += amount if buy else -amount
                trades.append({"ts": int(arrays["ts"][i]), "signal_ts": int(arrays["ts"][signal_index]),
                               "side": "buy" if buy else "sell", "qty": amount, "price": fill,
                               "notional": amount * fill, "fee": fees, "slippage_cost": slippage_cost})
        close = float(arrays["close"][i])
        equity = cash + qty * close
        position = (qty * close / equity) if equity > 0 else 0.0
        marks.append({"ts": int(arrays["ts"][i]), "equity": equity, "cash": cash, "qty": qty,
                      "close": close, "position": position, "target": want,
                      "fee": fees, "slippage_cost": slippage_cost})
    return marks, trades


def statistics(marks, trades, initial, monthly_cost=None):
    values = np.array([initial] + [row["equity"] for row in marks])
    returns = values[1:] / values[:-1] - 1
    days = len(marks)
    sharpe = float(returns.mean() / returns.std(ddof=1) * np.sqrt(365)) if days > 1 and returns.std(ddof=1) > 0 else None
    fees = sum(row["fee"] for row in trades)
    running = monthly_cost * days * 12 / 365.25 if monthly_cost is not None else None
    if marks:
        avg_position = float(np.mean([row["position"] for row in marks]))
        avg_target = float(np.mean([row["target"] for row in marks]))
    else:
        avg_position = avg_target = None
    return {"days": days, "initial_equity": initial, "final_equity": float(values[-1]),
            "net_pnl": float(values[-1] - initial), "net_return": float(values[-1] / initial - 1),
            "cagr": float((values[-1] / initial) ** (365.25 / days) - 1) if days >= 365 else None,
            "max_drawdown": float((values / np.maximum.accumulate(values) - 1).min()),
            "sharpe": sharpe, "trades": len(trades), "fees": fees,
            "slippage_cost": sum(row["slippage_cost"] for row in trades),
            "turnover_notional": sum(row["notional"] for row in trades),
            "avg_position": avg_position, "avg_target": avg_target,
            "running_cost": running,
            "pnl_after_running_cost": float(values[-1] - initial - running) if running is not None else None}


def rolling_splits(frame, config):
    dates = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    rows = []
    for year in range(pd.Timestamp(config["validation_start"]).year, dates.iloc[-1].year + 1):
        start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        train = start - pd.DateOffset(years=config["training_years"])
        finish = start + pd.DateOffset(years=1)
        train_i = int(dates.searchsorted(train))
        start_i, end_i = int(dates.searchsorted(start)), int(dates.searchsorted(finish))
        if train_i < WARMUP + 1 or start_i >= end_i:
            continue
        rows.append({"year": year, "train_start": train.isoformat(), "validation_start": start.isoformat(),
                     "validation_end_exclusive": finish.isoformat(), "train_index": train_i,
                     "start_index": start_i, "end_index": end_i,
                     "partial_year": int(frame["ts"].iloc[end_i - 1]) + DAY_MS < int(finish.timestamp() * 1000)})
    if not rows:
        raise ValueError("insufficient_history_for_three_year_rolling_validation")
    return rows


def fixed_start_windows(frame, config):
    """Exact calendar starts to a shared cutoff; never silently shift a requested start."""
    dates = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    end_i = len(frame)
    rows = []
    for start_str in config["validation_starts"]:
        start = pd.Timestamp(start_str, tz="UTC")
        requested_ms = int(start.timestamp() * 1000)
        start_i = int(dates.searchsorted(start))
        if start_i >= end_i:
            raise ValueError(f"no_trading_bar_at_or_after_start:{start_str}")
        actual_ms = int(frame["ts"].iloc[start_i])
        if actual_ms != requested_ms:
            raise ValueError(
                f"requested_start_unavailable:{start_str}:nearest_bar="
                f"{pd.Timestamp(actual_ms, unit='ms', tz='UTC').date().isoformat()}"
            )
        # Need WARMUP bars strictly before the first trade day for indicator warmup.
        if start_i < WARMUP:
            raise ValueError(
                f"insufficient_warmup_before_start:{start_str}:"
                f"have={start_i}:need={WARMUP}"
            )
        warmup_cutoff_ts = int(frame["ts"].iloc[start_i - 1])
        rows.append({
            "requested_start": start_str,
            "requested_start_ts": requested_ms,
            "start_index": start_i,
            "end_index": end_i,
            "actual_first_execution_time": pd.Timestamp(actual_ms, unit="ms", tz="UTC").isoformat(),
            "warmup_cutoff": pd.Timestamp(warmup_cutoff_ts, unit="ms", tz="UTC").isoformat(),
            "warmup_cutoff_ts": warmup_cutoff_ts,
        })
    return rows


def compare_asset(frame, config, splits, monthly_cost):
    signals = targets(frame, config)
    candidates = tuple(config["candidates"])
    outputs, all_marks, all_trades = {}, [], []
    start, end = splits[0]["start_index"], splits[-1]["end_index"]
    for scenario_name, scenario in config["scenarios"].items():
        outputs[scenario_name] = {}
        for candidate in candidates:
            marks, trades = simulate(frame, signals[candidate], start, end, config, scenario)
            summary = statistics(marks, trades, config["initial_equity"], monthly_cost)
            folds = []
            for fold in splits:
                first = int(frame["ts"].iloc[fold["start_index"]])
                last = int(frame["ts"].iloc[fold["end_index"] - 1])
                segment = [r for r in marks if first <= r["ts"] <= last]
                executions = [r for r in trades if first <= r["ts"] <= last]
                offset = fold["start_index"] - start
                before = marks[offset - 1]["equity"] if offset else config["initial_equity"]
                train_marks, train_trades = simulate(frame, signals[candidate], fold["train_index"],
                                                       fold["start_index"], config, scenario)
                folds.append({"year": fold["year"], "partial_year": fold["partial_year"],
                              "training": statistics(train_marks, train_trades, config["initial_equity"], monthly_cost),
                              "validation": statistics(segment, executions, before, monthly_cost)})
            outputs[scenario_name][candidate] = {"continuous_validation": summary, "folds": folds}
            all_marks.extend({"scenario": scenario_name, "candidate": candidate, **row} for row in marks)
            all_trades.extend({"scenario": scenario_name, "candidate": candidate, **row} for row in trades)
        baseline = outputs[scenario_name]["buy_hold"]["continuous_validation"]
        for candidate in candidates:
            row = outputs[scenario_name][candidate]["continuous_validation"]
            row["excess_return"] = row["net_return"] - baseline["net_return"]
    return outputs, all_marks, all_trades


def compare_asset_fixed_start(frame, config, windows, monthly_cost):
    signals = targets(frame, config)
    candidates = tuple(config["candidates"])
    outputs, all_marks, all_trades = {}, [], []
    for window in windows:
        start_key = window["requested_start"]
        outputs[start_key] = {}
        start, end = window["start_index"], window["end_index"]
        for scenario_name, scenario in config["scenarios"].items():
            outputs[start_key][scenario_name] = {}
            for candidate in candidates:
                marks, trades = simulate(frame, signals[candidate], start, end, config, scenario)
                summary = statistics(marks, trades, config["initial_equity"], monthly_cost)
                payload = {
                    "requested_start": window["requested_start"],
                    "actual_first_execution_time": window["actual_first_execution_time"],
                    "warmup_cutoff": window["warmup_cutoff"],
                    "metrics": summary,
                }
                outputs[start_key][scenario_name][candidate] = payload
                all_marks.extend({
                    "requested_start": start_key, "scenario": scenario_name, "candidate": candidate, **row
                } for row in marks)
                all_trades.extend({
                    "requested_start": start_key, "scenario": scenario_name, "candidate": candidate, **row
                } for row in trades)
            baseline = outputs[start_key][scenario_name]["buy_hold"]["metrics"]
            for candidate in candidates:
                row = outputs[start_key][scenario_name][candidate]["metrics"]
                row["excess_return"] = row["net_return"] - baseline["net_return"]
    return outputs, all_marks, all_trades


def _fmt_pct(value):
    return f"{value:.2%}" if value is not None else "n/a"


def _fmt_num(value, digits=2):
    return f"{value:.{digits}f}" if value is not None else "n/a"


def render_report(manifest, results, checks):
    config = manifest["config"]
    mode = research_mode(config)
    if mode == "fixed_start":
        return render_fixed_start_report(manifest, results, checks)
    lines = ["# 独立历史研究报告", "", f"实验：`{manifest['experiment_id']}`",
             f"生成时间 UTC：{manifest['generated_at']}",
             "", "固定候选的历史滚动验证；历史已参与研究，不是未触碰的最终样本外，也不是纸面或实际收入。",
             "各候选分别以相同初始资金运行。连续验证曲线跨年不重置，年度指标不取平均代替复合收益。",
             "日线收盘信号在下一根开盘执行；延迟情景额外滞后一天，不能模拟分钟级延迟或订单簿容量。",
             "期末持仓按收盘估值，未强制平仓；成本含配置手续费和滑点，未模拟市场冲击。", ""]
    lines.append(f"每个独立账户初始 ${config['initial_equity']:.2f}；基线单边手续费 {config['fee']:.3%}，"
                 f"单边滑点 {config['slippage']:.3%}；调仓偏差阈值 {config['rebalance_threshold']:.0%}。")
    if manifest["monthly_running_cost"] is None:
        lines.append("运行成本未提供，不能据此判断项目净收入。")
    candidates = tuple(config["candidates"])
    for asset, scenarios in results.items():
        health = checks[asset]["data"]
        last = pd.Timestamp(health["last_candle_ts"], unit="ms", tz="UTC").isoformat()
        first_validation = scenarios["base"]["buy_hold"]["folds"][0]["year"]
        lines.extend(["", f"## {asset}", f"来源：OKX 现货日线本地快照；最后日线 UTC：{last}；"
                      f"距收盘 {health['age_since_last_close_hours']:.1f} 小时；"
                      f"不适合实时使用：{health['stale_for_live_use']}。",
                      f"连续验证从 {first_validation}-01-01 UTC 开盘开始，到上述最后日线收盘结束。",
                      "", "| 情景 | 候选 | 净收益 | 超额收益 | 年化 | 最大回撤 | Sharpe | 成交 | 手续费 | avg_pos | avg_tgt | 扣运行费损益 |",
                      "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for scenario, cand_map in scenarios.items():
            for name, result in cand_map.items():
                row = result["continuous_validation"]
                cagr = f"{row['cagr']:.2%}" if row["cagr"] is not None else "不足一年"
                sharpe = f"{row['sharpe']:.2f}" if row["sharpe"] is not None else "未定义"
                income = f"${row['pnl_after_running_cost']:+.2f}" if row["pnl_after_running_cost"] is not None else "未提供成本"
                lines.append(
                    f"| {scenario} | {name} | {row['net_return']:.2%} | {row['excess_return']:+.2%} | "
                    f"{cagr} | {row['max_drawdown']:.2%} | {sharpe} | {row['trades']} | ${row['fees']:.2f} | "
                    f"{row['avg_position']:.3f} | {row['avg_target']:.3f} | {income} |"
                )
        lines.extend(["", "### 候选复核", ""])
        for candidate in candidates[1:]:
            failures = []
            for scenario, cand_map in scenarios.items():
                row = cand_map[candidate]["continuous_validation"]
                baseline = cand_map["buy_hold"]["continuous_validation"]
                if row["net_return"] <= 0:
                    failures.append(f"{scenario}: 扣交易成本收益非正")
                if row["excess_return"] < 0:
                    failures.append(f"{scenario}: 跑输买入持有")
                if abs(row["max_drawdown"]) >= abs(baseline["max_drawdown"]) / 2:
                    failures.append(f"{scenario}: 回撤未降至基准一半以下")
            lines.append(f"- {candidate}: " + ("；".join(failures) if failures else "历史收益/回撤筛查通过，仍需前向验证"))
    lines.extend(["", "## 边界", "", "- 没有参数搜索、自动选优、策略切换或晋级；注册表保持原样。",
                  "- 偏差检查抽查前缀与预热一致性，不是覆盖全部路径的数学证明。",
                  "- BTC/ETH 和重叠训练窗口并非独立样本；不同资产和候选收益不相加。",
                  "- 本次未评估未实现的双腿撮合、GMGN、Polymarket 或 Carry 盈利。",
                  "- 完整年度明细见 comparison.json，逐笔成本和连续权益见 trades.csv / equity.csv。"])
    return "\n".join(lines) + "\n"


def render_fixed_start_report(manifest, results, checks):
    config = manifest["config"]
    candidates = tuple(config["candidates"])
    lines = [
        "# 固定起点历史验证：vol-cap vs half-scale",
        "",
        f"实验：`{manifest['experiment_id']}`",
        f"生成时间 UTC：{manifest['generated_at']}",
        f"模式：`fixed_start` / 标签：历史开发验证 (historical_only)",
        f"untouched_holdout={manifest.get('untouched_holdout', False)}；仍需前向验证；**不可称为 OOS / 样本外**。",
        "",
        "无训练窗、无参数选择。每个 `validation_starts` 日期是独立账户，从该 UTC 元旦开盘交易到共享冻结 cutoff。",
        "候选：buy_hold / trend_only / trend_only_scale_50 (=0.5×trend_only) / trend_vol_cap。",
        "半仓仍使用 10pp 调仓阈值 → 相对调仓频率会变化；费用下降可能同时来自更小仓位与更低换手。",
        "若 vol_cap 与 half-scale 的 avg_position 相差很大，只描述收益/风险权衡，**不得**声称动态波动控制已证明，也不得回测重调半仓或波动目标。",
        "avg_position = 标记日实际持仓市值/当日收盘权益均值；avg_target = 目标权重均值（二者分开报告）。",
        "",
        f"每个独立账户初始 ${config['initial_equity']:.2f}；手续费 {config['fee']:.3%}；"
        f"滑点 {config['slippage']:.3%}；调仓阈值 {config['rebalance_threshold']:.0%}；"
        f"vol_cap lookback={config['volatility_cap']['lookback_days']} / annual_target={config['volatility_cap']['annual_target']}。",
    ]
    if manifest["monthly_running_cost"] is None:
        lines.append("运行成本未提供，不能据此判断项目净收入。")

    group_count = 0
    for asset, starts in results.items():
        health = checks[asset]["data"]
        last = pd.Timestamp(health["last_candle_ts"], unit="ms", tz="UTC").isoformat()
        lines.extend([
            "", f"## {asset}",
            f"来源：OKX 现货日线本地快照；最后日线 UTC：{last}；"
            f"距收盘 {health['age_since_last_close_hours']:.1f} 小时；"
            f"不适合实时使用：{health['stale_for_live_use']}。",
            f"共享冻结 cutoff = 上述最后已收盘日线；所有起点共用同一快照。",
            "",
            "| 起点 | 情景 | 候选 | 净收益 | 超额 | CAGR | 最大回撤 | Sharpe | 成交 | 手续费 | 换手名义 | 滑点成本 | avg_pos | avg_tgt | 首笔执行 | warmup截止 |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ])
        for start_key in config["validation_starts"]:
            for scenario, cand_map in starts[start_key].items():
                for name in candidates:
                    payload = cand_map[name]
                    row = payload["metrics"]
                    group_count += 1
                    cagr = _fmt_pct(row["cagr"]) if row["cagr"] is not None else "不足一年"
                    sharpe = _fmt_num(row["sharpe"]) if row["sharpe"] is not None else "未定义"
                    lines.append(
                        f"| {start_key} | {scenario} | {name} | {row['net_return']:.2%} | "
                        f"{row['excess_return']:+.2%} | {cagr} | {row['max_drawdown']:.2%} | {sharpe} | "
                        f"{row['trades']} | ${row['fees']:.2f} | ${row['turnover_notional']:.2f} | "
                        f"${row['slippage_cost']:.2f} | {row['avg_position']:.3f} | {row['avg_target']:.3f} | "
                        f"{payload['actual_first_execution_time']} | {payload['warmup_cutoff']} |"
                    )
    lines.extend([
        "", f"合计报告组数：{group_count}（期望 3 starts × 2 assets × 3 scenarios × 4 candidates = 72）。",
        "", "## 解读护栏", "",
        "- 历史开发验证 / historical_only；untouched_holdout=false；仍需前向验证；never claim OOS。",
        "- half-scale 仍用 10pp rebalance threshold → 相对再平衡频率变化；fee 改善可能来自更小 size 与更低 turnover。",
        "- 若 vol_cap vs half-scale 的 avg_position 差异大，只描述 return/risk tradeoff；不声称动态 vol control 已证明；不重调 half scale 或 vol target。",
        "- 没有参数搜索、自动选优、策略切换或晋级；注册表/纸面账户/crontab 保持原样。",
        "- 完整明细见 comparison.json；逐笔见 trades.csv / equity.csv。",
    ])
    return "\n".join(lines) + "\n"


def run(data_dir=None, output_root=None, config_path=None, monthly_cost=None, now=None, mode=None):
    data_dir = Path(data_dir or ROOT / "data")
    output_root = Path(output_root or ROOT / "data/research")
    config_path = Path(config_path or ROOT / "research_config.json")
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    if monthly_cost is not None and (not math.isfinite(monthly_cost) or monthly_cost < 0):
        raise ValueError("invalid_monthly_running_cost")
    raw_config = config_path.read_bytes()
    config = json.loads(raw_config)
    if mode is not None:
        config["mode"] = mode
    validate_config(config)
    mode_name = research_mode(config)
    experiment = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output_root.mkdir(parents=True, exist_ok=True)
    stage = output_root / ("." + experiment + ".inprogress")
    stage.mkdir()
    snapshots = stage / "snapshots"
    snapshots.mkdir()
    (snapshots / "research_config.json").write_bytes(
        json.dumps(config, ensure_ascii=False, indent=2).encode() + b"\n"
    )
    for name in ("research_runner.py", "runtime_provenance.py", "strategy_registry.json"):
        (snapshots / name).write_bytes((ROOT / name).read_bytes())
    manifest = {"schema": 1, "experiment_id": experiment, "generated_at": now.isoformat(),
                "git_revision": git_revision(), "code_sha256": hashlib.sha256((snapshots / "research_runner.py").read_bytes()).hexdigest(),
                "config_sha256": canonical_hash(config), "config": config, "mode": mode_name,
                "environment": {"python": platform.python_version(), "numpy": np.__version__,
                                "pandas": pd.__version__, "pyarrow": pyarrow.__version__},
                "monthly_running_cost": monthly_cost, "status": "running", "historical_only": True,
                "untouched_holdout": False, "automatic_promotion": False, "label": "历史开发验证",
                "inputs": {}}
    checks, splits, results = {}, {}, {}
    started = time.monotonic()
    try:
        for asset in config["assets"]:
            filename = f"okx_candles_{asset}.parquet"
            raw = (data_dir / filename).read_bytes()
            (snapshots / filename).write_bytes(raw)
            manifest["inputs"][asset] = {"filename": filename, "sha256": hashlib.sha256(raw).hexdigest(),
                                         "source": "OKX spot daily candles, local collector snapshot",
                                         "snapshot_captured_at": now.isoformat(), "upstream_acquired_at": None}
            frame, health = load_candles(raw, asset, now_ms)
            bias = bias_checks(frame, config)
            checks[asset] = {"data": health, "bias": bias}
            if not bias["passed"]:
                raise ValueError(f"indicator_bias_check_failed:{asset}")
            folder = stage / asset
            folder.mkdir()
            if mode_name == "fixed_start":
                windows = fixed_start_windows(frame, config)
                splits[asset] = windows
                results[asset], marks, trades = compare_asset_fixed_start(
                    frame, config, windows, monthly_cost
                )
                pd.DataFrame(marks).to_csv(folder / "equity.csv", index=False)
                trade_cols = ["requested_start", "scenario", "candidate", "ts", "signal_ts", "side",
                              "qty", "price", "notional", "fee", "slippage_cost"]
                pd.DataFrame(trades, columns=trade_cols).to_csv(folder / "trades.csv", index=False)
            else:
                splits[asset] = rolling_splits(frame, config)
                results[asset], marks, trades = compare_asset(frame, config, splits[asset], monthly_cost)
                pd.DataFrame(marks).to_csv(folder / "equity.csv", index=False)
                pd.DataFrame(trades, columns=["scenario", "candidate", "ts", "signal_ts", "side", "qty",
                                              "price", "notional", "fee", "slippage_cost"]).to_csv(
                    folder / "trades.csv", index=False
                )
        manifest["status"] = "complete"
        write_json(stage / "comparison.json", results)
        (stage / "report.md").write_text(render_report(manifest, results, checks))
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        manifest["elapsed_seconds"] = time.monotonic() - started
        write_json(stage / "manifest.json", manifest)
        write_json(stage / "checks.json", checks)
        split_kind = ("fixed_start_no_parameter_selection" if mode_name == "fixed_start"
                      else "historical_rolling_validation_no_parameter_selection")
        write_json(stage / "splits.json", {"kind": split_kind, "assets": splits})
        stage.rename(output_root / experiment)
    return output_root / experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--config", type=Path, default=ROOT / "research_config.json")
    parser.add_argument("--mode", choices=["rolling", "fixed_start"], default=None,
                        help="Override config mode (rolling | fixed_start).")
    parser.add_argument("--monthly-running-cost", type=float, default=None,
                        help="Optional USD/month for each independent simulated account; never combined.")
    args = parser.parse_args()
    output = run(data_dir=args.data_dir, config_path=args.config, monthly_cost=args.monthly_running_cost,
                 mode=args.mode)
    print(f"Research artifacts: {output}")
    print(f"Report: {output / 'report.md'}")


if __name__ == "__main__":
    main()
