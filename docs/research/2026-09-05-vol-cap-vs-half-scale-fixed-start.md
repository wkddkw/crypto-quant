# Vol-cap vs half-scale：固定起点历史验证设计

日期：2026-09-05  
标签：历史开发验证 / `historical_only`  
`untouched_holdout=false` — **不可称为 OOS / 样本外**；仍需前向验证。

## 目标

在**不改动**纸面账户、策略注册表、crontab、`hourly_observe` 行为的前提下，比较：

| 候选 | 含义 |
|------|------|
| `buy_hold` | 全仓持有 |
| `trend_only` | 现有趋势信号 |
| `trend_only_scale_50` | `0.5 * trend_only`（半仓缩放） |
| `trend_vol_cap` | 趋势信号 × 波动上限缩放（lookback=30, annual_target=0.3） |

主问题：波动上限相对半仓缩放，在相近平均仓位下是否改善收益/回撤权衡。若二者 `avg_position` 差很多，**只描述** return/risk tradeoff，**不得**声称动态波动控制已证明，也**不得**回测重调半仓比例或 vol target。

## `fixed_start` 模式

- 配置：`research_config_fixed_start.json`，`mode: fixed_start`。
- **无训练窗 / 无参数选择**；不要求 `training_years==3`。
- `validation_starts`: `["2020-01-01","2021-01-01","2022-01-01"]`（UTC 元旦）。
- 每个起点只需起点前有足够指标预热（`WARMUP=201` 根日线）；若无法兑现（预热不足、或该日无交易 K 线），**硬失败**，绝不静默后移起点。
- 每个起点 = 独立账户，从该日起模拟到**共享**冻结 cutoff（同一快照的最后已收盘 bar）。
- 情景：`base` / `slippage_x2` / `signal_delay_1d`。
- 成本：fee=0.001，slippage=0.0003，rebalance_threshold=0.1，min_notional=1.0。

报告组数：3 starts × 2 assets × 3 scenarios × 4 candidates = **72**。

## 指标

保留：`net_return`, `cagr`, `max_drawdown`, `sharpe`, `fees`, `trades`, `turnover_notional`, `slippage_cost`。

新增：

- `avg_position`：标记日 **实际持仓市值 / 当日收盘权益** 的均值（来自模拟持仓，非 target）。
- `avg_target`：目标权重均值；**不得**取代 `avg_position`。

汇总表含：`requested_start`, `actual_first_execution_time`, `warmup_cutoff`（首个交易日前一根 / 预热最后一根）。

## 解读护栏

1. half-scale 仍使用 **10pp** 调仓阈值 → 相对再平衡频率会变；费用下降可能同时来自更小仓位与更低换手。
2. 历史已参与研究开发；非未触碰 holdout。
3. 不自动晋级、不改 registry / paper / cron。

## 复现步骤（本节点）

```bash
cd /opt/crypto-quant/app
source .venv/bin/activate
# 单测
python -m pytest tests/test_research_runner.py tests/test_research_runner_fixed_start.py -q
# 全量离线实验（与 hourly_observe 互斥锁）
CRYPTO_QUANT_NO_PROXY=1 flock -x data/.hourly-observe.lock \
  .venv/bin/python research_runner.py \
  --config research_config_fixed_start.json \
  --mode fixed_start \
  2>&1 | tee /opt/crypto-quant/logs/research_runner-fixed-start.log
```

结果默认写在 `data/research/<experiment_id>/`（gitignore）。关键产物副本提交到：

`docs/research/results/2026-09-05-fixed-start-vol-vs-half/`  
（`report.md`, `comparison.json`, `checks.json`, `manifest.json`, 以及精简 summary）。

## 与 rolling 模式关系

默认 `research_config.json` 仍为 rolling（含 `ma200`）；`fixed_start` 为独立配置/CLI，互不破坏。
