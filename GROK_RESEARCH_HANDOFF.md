# Remote Research Handoff

This is the current evidence package for an always-on remote research node and Grok Bot. It complements [GROK_REMOTE_RUNBOOK.md](GROK_REMOTE_RUNBOOK.md), which describes deployment, services, scheduling, dashboard access, and safety boundaries.

**Snapshot date:** 2026-09-01

## 1. What Is Verified

The project is a Python research and paper-accounting system. It has no exchange private API integration, wallet signing, private key storage, transaction submission, order cancellation, or real-money accounting.

The public repository intentionally excludes `data/`, `.env`, virtual environments, local reports, paper ledgers, and credentials. A remote node must keep its own local `data/` directory and back it up separately.

All results described below are research, backtest, fixture, or local paper results. None are real trading results.

## 2. Strategy Status

| Strategy | Governance state | Evidence that is usable | What is not usable | Next hard gate |
|---|---|---|---|---|
| BTC `v0_full` | `retired` historical baseline | It has a local paper-accounting implementation and historical research output. | Its paper PnL must not validate the production candidate because research found the composite emotion/trend version inferior to `trend_only`. | Do not promote. Retain only for comparison. |
| BTC `trend_only` | `shadow` | Full-sample backtest: annualized return about 37.4%, Sharpe 1.18, max drawdown about -37.9%; better than buy-and-hold in that in-sample test. | Walk-forward average Sharpe is only 0.44; 2022 and 2026 were weak. No dedicated shadow runner exists yet. | Fixed-rule, non-overlapping daily paper evidence for at least 12 weeks, then frozen 12-month OOS review. |
| OKX funding carry | `observe_only` | Accounting structure separates spot, swap, funding, costs, basis residual, and halt behavior. Current conservative filter correctly stays flat when net edge is negative. | No valid completed paper round trip. Existing event history contains `fixture` records and must not be treated as performance evidence. | Six months of OKX funding data, 30 conservative eligible observations, 20 reconciled paper round trips with positive net expectancy. |
| Polymarket complete-set | `paused` | Public Gamma/CLOB observation and executable two-leg ask-cost calculation exist. | Historical samples matched a long-dated Hegseth market due to unsafe substring matching; all 16 old records are invalid for strategy evaluation. | Accumulate at least 500 correctly matched, expired BTC/ETH binary Up/Down markets after the fixed filter. |
| GMGN Solana copy | `shadow` | Dedicated paper ledger, daily frozen wallet pool, buy-only policy, de-duplication, latency rejection, exposure caps, costs, cooldown, and kill switch are implemented and tested. | Current mode is fixture only. The fixture contains future timestamps and produces no valid paper trade. No official live GMGN API contract has been configured or validated. | Verify terms and official read-only API contract; collect 30 complete days and at least 100 independent accepted paper signals with positive post-cost PnL. |

The source of truth for promotion and retirement conditions is [strategy_registry.json](strategy_registry.json).

## 3. Known Data-Quality Issues

1. **BTC daily aggregate report bug:** the first daily report used the BTC account `cash` field as equity. The local paper account's cash is not total equity when BTC remains open. Treat that report's BTC `$425.44` figure as invalid; use the account's mark-to-market equity or strategy report instead.
2. **Carry fixture contamination:** `data/carry/events.jsonl` contains execution events marked `reason:"fixture"`. Exclude them from every performance calculation. Never delete historical records automatically; flag them in governance.
3. **Polymarket invalid history:** old observations matched a long-dated political market rather than a BTC/ETH 15-minute binary market. Do not count any of those observations, candidates, or implied PnL toward validation.
4. **GMGN has no live source yet:** fixture mode is intentional. Do not guess an endpoint or scrape a web UI. Missing live API contract fields must fail closed as `blocked_config`.
5. **Backtest is not promotion evidence:** full-sample BTC results are hypothesis-generation evidence. Walk-forward weakness and absent shadow evidence prevent production use.

## 4. Highest-Value Remote Work

The remote node can improve confidence faster by parallelizing computation and data-quality work. It cannot compress calendar-time requirements such as 12 weeks of independent paper observations, 30 days of GMGN completeness, or a future regime's live carry opportunities.

Prioritize work in this order:

1. Implement a dedicated fixed-rule BTC `trend_only` shadow runner and daily non-overlapping evaluation, without altering the retired `v0_full` paper account.
2. Repair the daily report aggregation so every strategy reports true marked equity, cash, open-position value, and PnL separately.
3. Add reconciliation checks for Carry: account state, funding ledger, and non-fixture events must agree before performance is calculated.
4. Run the corrected Polymarket filter in observation mode; preserve the old records but start a new valid sample counter.
5. Obtain official GMGN API documentation and permission. Implement only the documented read-only contract, then first run one day in observe-only mode before paper fills.
6. Run sensitivity studies rather than parameter searches: cost, slippage, latency, liquidity, and data-dropout stress tests for each strategy.
7. Produce a daily market-and-data-quality memo with evidence links and no trade recommendation.

## 5. Required Research Discipline

For every proposed change or new strategy, Grok Bot must create a research proposal containing:

```text
hypothesis
why it could work before looking at results
data source and provider permission
time range and sample size
feature/signal definition
entry and exit model
fee, gas, slippage, and latency model
position and risk limits
paper/shadow validation duration
promotion rule
kill rule
known failure modes
```

A new idea is never allowed to change a deployed rule automatically. The required lifecycle is:

```text
research proposal -> historical/replay test -> fixture test -> shadow paper run -> governance review -> human approval
```

## 6. GitHub Communication Model

GitHub is the durable code and documentation channel. It is not the live ledger transport.

### Main branch

- `main` is the reviewed public baseline.
- The remote Grok node pulls `main` with `git pull --ff-only origin main`.
- Do not store local `data/`, `.env`, API keys, reports containing sensitive identifiers, or paper account files in Git.

### Grok Bot changes

- Grok Bot creates a branch such as `grok/research-YYYY-MM-DD`.
- It commits only code, tests, documentation, fixtures that contain no real credentials or sensitive wallet data, and reproducible research summaries.
- It opens a pull request or provides a patch. Do not push directly to `main`.
- Every pull request includes test output, data cut-off time, assumptions, and a statement that no real trade was submitted.

### Daily reports

- The remote node writes detailed reports to its own local `data/daily_reports/` and delivers a Chinese summary through the Grok Bot's configured notification channel.
- Do not automatically commit daily paper account reports to this public repository.
- If a report needs long-term Git history, commit only a manually reviewed, redacted research summary to a branch or `docs/research/`.

### Local and remote communication

- This local Mac does not run strategy schedules or a Dashboard anymore.
- The remote node is the sole running paper/research node.
- To inspect remote status, use the remote dashboard through authenticated private access and the Grok Bot's scheduled report.
- When moving away from Grok Bot in about two months, migrate the repository, systemd unit files, timer definitions, server-local `.env`, the local `data/` backup, and the reports directory. The database/ledger state is local data, not Git history.

## 7. Grok Bot Daily Deliverable

At 18:00 Asia/Shanghai, deliver a Chinese report containing:

1. Every strategy's state, true marked paper equity, cash, open position value, and open-position risk.
2. Signal count, accepted/rejected count, and top rejection reasons.
3. Freshness timestamp and upstream error/blocked-configuration status for each data channel.
4. Whether any fixture, invalid market, duplicate event, or ledger mismatch invalidates a metric.
5. Governance state, next review date, remaining sample/time requirement, and blocker.
6. Market context as cited background only; no direct buy/sell recommendation.
7. At most three research hypotheses, each with a test and kill condition.

Use the exact phrase below in every report footer:

```text
本报告仅包含研究与纸面账本数据；未连接钱包、未签名、未提交真实交易。
```

## 8. Migration Checklist

Before the Grok Bot subscription or server is retired:

1. Copy `/opt/crypto-quant/app/data/` using a verified backup, including JSONL event logs and daily reports.
2. Copy `/opt/crypto-quant/app/.env` through a secure secret channel, not Git or chat.
3. Export systemd unit and timer files, reverse-proxy/Tailscale configuration, and relevant logs.
4. Record the active Git commit, installed Python version, and `pip freeze` output.
5. Run tests, governance, daily report, and dashboard health check on the replacement node.
6. Compare account state and event counts between old and new nodes before enabling schedules.
7. Disable old systemd timers and dashboard service only after the replacement report and health checks are confirmed.
