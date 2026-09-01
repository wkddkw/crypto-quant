# Funding Carry Paper Ledger

独立于 `data/paper/` 的 OKX 资金费率套利模拟账本。

## 策略

```text
买入 BTC-USDT 现货
+ 卖空等名义 BTC-USDT-SWAP
= Delta 中性

收益 = 已结算 funding - 现货/永续手续费 - 滑点 - 基差/对冲残差
```

仅使用 OKX 公共 API。没有 API key、没有真实订单、没有钱包权限。

## 命令

```bash
.venv/bin/python carry_trader.py init
.venv/bin/python carry_trader.py run
.venv/bin/python carry_trader.py report
.venv/bin/python carry_trader.py status
.venv/bin/python carry_replay.py run
.venv/bin/python carry_replay.py report
.venv/bin/python -m unittest discover -s tests -v
```

## 产物

- `data/carry/account.json`: 当前仓位与权益快照
- `data/carry/events.jsonl`: append-only 开平仓、mark、funding 事件
- `data/carry/snapshots.csv`: 每小时 OKX 现货/永续报价、mark/index、basis、funding
- `data/carry/funding_ledger.csv`: 已结算 funding 明细
- `data/carry/report.md`: carry 专属报告
- `data/carry_replay/settlement_replay.csv`: 近期 OKX funding 历史回放逐结算记录
- `data/carry_replay/report.md`: 历史回放覆盖率、收益分解和限制

## 开仓门槛

- funding 年化至少 8%
- 净 edge 为正，且至少 2%
- 净 edge 统一为年化口径：

```text
funding 年化
- (往返手续费 + 滑点 + 基差缓冲) / 预计持仓天数 × 365
```

- 基差绝对值不高于 3%
- 行情数据必须新鲜
- 现货本金 + 2x 隔离保证金不超过 carry 账户资金

当前默认预计持仓 30 天，往返成本与缓冲合计 1.3%，折算年化成本约 15.8%。因此 funding 年化 7.5% 即使为正，净 edge 仍约 -8.3%，正确行为是空仓。

## 历史回放

`carry_replay.py` 只读取本地 UTC 日K与近三个月实际 OKX funding 结算记录，并独立写入 `data/carry_replay/`，不会触碰实时 `data/carry/` 账本。成交统一按当天日线开盘加固定滑点近似，报告会明确列出覆盖率、跳过原因和限制；它不能替代订单簿级可执行回测，也不会把 Deribit 长历史代理记作 OKX 收益。

## 限制

- funding 收益仅在 OKX 已结算 8 小时事件出现后入账，不提前计入。
- 当前账本用公共报价模拟 bid/ask 和固定滑点；尚未包含真实成交、订单簿深度、手续费等级、利息、强平和交易所信用风险。
- Deribit funding 只用于历史研究代理，不会写入 carry 已实现收益。
- 任何实盘或 OKX demo 连接都需要单独的实现与明确授权。
