# 研究任务规格：聪明钱钱包质量测量与关联簇分析（step-1 gate）

- 日期：2026-09-02
- 状态：proposed（所有者批准后进入执行）
- 关联策略提案：GMGN 聪明钱跟单（见 2026-09-02 讨论定稿的三条设计规则）
- 执行边界：纯只读研究。不碰 `strategy_registry.json`，不开 GMGN live，
  不下任何单，不改任何策略参数。产出物是数据 + 一份结论报告。

## 0. 目的与门控关系

本任务是聪明钱策略的**第一道门**，回答两个问题：

1. **质量**：GMGN Solana 榜前 100 钱包的历史买入，在我们实际可达的入场
   延迟下，收益中位数是否为正？为负则策略直接否决，后续管道全部不建。
2. **独立性**：这 100 个钱包里有多少其实属于同一关联簇（互转资金、同步
   买卖、共同持仓）？簇重合度决定"≥K 个独立确认"信号是否有存在空间。

两个问题共用同一份原始采集（钱包历史交易 + 一跳资金来源），一次采集两用。

## 1. 数据源与密钥

| 源 | 用途 | 密钥 | 状态 |
|---|---|---|---|
| GMGN 官方只读 API | 前 100 钱包榜单 + 每钱包 PnL/胜率 | `GMGN_READONLY_API_KEY`（仅存环境变量，不入库） | **阻塞项**：contract 字段未填（见 §6） |
| Helius RPC / webhook | 买入后 1h/24h 代币价格轨迹、tx 详情 | `HELIUS_API_KEY` | 免费档即可 |
| Solana FM / explorer | 每钱包资金流入（CEX 提款、转账来源） | 无 | 兜底 |
| Dune（备选） | 历史价格轨迹批量查询 | 免费档有限 | 仅当 Helius 不够 |

原则：密钥只走环境变量与本地配置，绝不写入仓库、日志、研究记录。

## 2. 样本与数据模型

**样本**：GMGN Solana 30d PnL 榜前 100 钱包（与 `gmgn_config.json` 的
`universe` 字段一致：chain=solana, ranking_period=30d, daily_rank_limit=100）。
每日重抓一次榜单快照，研究期内快照可对比（榜单稳定性本身是观察项）。

**每钱包拉取**：近 **90 天**、单笔 `trade_usd >= 200` 的买入事件
（过滤 dust 噪音）。事件 schema 对齐 `gmgn_models.TradeEvent`，研究扩展字段：

```
event_id, wallet_address, executed_at, asset_mint, side,
trade_usd, price_usd, liquidity_usd,
rank_at_capture          (int, 买入时该钱包在榜单上的名次)
followers_at_capture     (int|null, 当时的跟随者数)
```

**目标规模**：约 100 钱包 × 90 天。按每钱包每周 20 笔估算约 2.5–3 万事件；
超过 5 万说明 dust 过滤太松。

## 3. 测量设计

### M1 — 事件收益曲线（质量主检验）

对每笔买入，记录代币在 t0（成交时刻）/ t+1min / t+10min / t+1h / t+24h 的
美元价格（来源：Helius 按 pool 取价）。定义 `ret_h = price(t+h)/price(t0) - 1`。

- 报告维度：全体分布、**按 rank 分层**（top-20 / 21-50 / 51-100）、按钱包聚合。
- 判据指标：
  - `winrate_h` = P(ret_h > 0)，**必须扣除成本后计**：从收益中减去往返
    双边手续费 + 滑点（`gmgn_config.paper` 的 `fee_pct`、`slippage_pct`，
    单边各一次）；
  - `median_ret_h`（分布中位，抗肥尾——memecoin 少数暴涨会拉高均值，
    对跟单者而言中位数才是日常体验）；
  - `p90_loss_h`（90 分位最大亏损，衡量尾部风险）；
  - 钱包级 Sharpe-like 指标：`mean(ret_24h)/std(ret_24h)` per wallet。

### M2 — 可达延迟折价（把基建现实算进收益）

对每笔买入记录**信号可见时刻** `t_vis`：GMGN/Dune 上该笔交易可被外部读取
的时间戳（用 GMGN 公开页面的 tx 时间作代理）。价格取
`price(max(t0 + 60s, t_vis + 10s))` 为"现实可达入场价"。

- 核心对比：`ret_24h@t0`（理论无延迟）vs `ret_24h@可达价`。
- 结论形式：延迟分布直方图 + 每增加 1 分钟延迟的中位收益衰减斜率。

### M3 — 簇分析（独立性检验）

对 100 钱包构建无向关联图，三种边：

- **E1 资金关联**：钱包 A 的 SOL/USDC 流入与钱包 B 的流出同源（同一 CEX
  热钱包除外；同一非 CEX 地址或同一提款交易）→ 边权 1.0，一跳；
- **E2 行为同步**：过去 90 天内两钱包买入同一代币且时间差 < 60s 的次数
  ≥ 3 → 边权按次数归一；
- **E3 持仓重合**：同时持有同一非主流币（排除 SOL/USDC/BTC/ETH 及 GMGN
  榜单前 20 的代币）≥ 5 个 → 边权 0.5。

连通分量 = 簇。**主指标：有效独立钱包数 = 100 − (簇内钱包数 − 簇数)**，
即每簇只留一票后的独立源数量。

### M4 — 簇内确认模拟（决定策略存活的组合检验）

用 M1+M3 的数据回放"30–120 分钟窗口内 ≥K 个**独立簇**买入同一代币"信号，
对比 K∈{3,5,10}、窗口∈{30,120} 分钟、簇去重开/关的每种组合：
信号数量、`median_ret_24h@可达价`、`winrate@可达价`（扣成本）。
这一步直接回答修正版策略（报警 ≥5 独立簇、纸面跟单）有没有正期望，
而不是原始"5/10 钱包数"版本。

## 4. 判据与决策

| 结果 | 决策 |
|---|---|
| M2 显示 `median_ret_24h@可达价 < 0`（扣成本） | **策略否决**，不建任何管道；出报告说明衰减斜率，作为项目级 kill 记录归档 |
| M1 中位为正但 M2 归零 | 降级：仅保留报警功能（不纸面跟单），或转长窗口（>4h）研究 |
| M3 显示有效独立源 < 30/100 | 确认数阈值 K 上调（原设计的 5/10 建立在 100 独立钱包假设上） |
| M4 存在任一 (窗口, K) 组合 `winrate@扣成本 > 55%` 且样本 ≥ 30 | **通过**，进入影子纸面账本设计提案 |

任何情况下都**不**触碰 live，不修改 `gmgn_config.json` 的 `mode: fixture`。

## 5. 产出物

```
scripts/wallet_quality/collect_gmgn_wallets.py   # 榜单 + 历史交易拉取
scripts/wallet_quality/collect_price_tracks.py   # Helius 价格轨迹
scripts/wallet_quality/cluster_analysis.py       # E1-E3 图 + 簇
scripts/wallet_quality/simulate_confirmations.py # M4 回放
data/wallet_quality/raw/*.jsonl                  # 全部 gitignored
data/wallet_quality/wallets.csv
data/wallet_quality/events.csv
data/wallet_quality/price_tracks.csv
data/wallet_quality/clusters.csv
data/wallet_quality/metrics.json
docs/research/2026-09-0X-wallet-quality-report.md # 脱敏结论报告
tests/test_wallet_quality.py                      # 纯逻辑：窗口/成本/簇合并
```

脱敏规则：报告与提交物不出现原始钱包地址（用 sha256 前 10 位作 ID）；
`data/` 整体 gitignored。

## 6. 前置阻塞与降级路径

- **阻塞项**：GMGN 官方 API 契约。`gmgn_config.json` 的
  `gmgn.base_url / api_version / auth / ranking_contract / trade_feed_contract`
  当前全为 null，`mode` 锁 `fixture`。需要先确认官方只读 API 是否对个人账户
  开放（读 `gmgn.ai/docs`，或申请 key 时拿到的文档），把契约填进配置。
- **降级路径（A）**：若 GMGN 契约不可用 → 钱包名单改用 Helius 自建：拉
  pump.fun/Raydium 历史 swap，按已实现 PnL 排序出前 100。代价：无"跟随者数"
  字段（放弃 `followers_at_capture`），且榜单定义与 GMGN 榜不完全等价。
- **降级路径（B）**：若 90 天历史拉不动（限流） → 缩到 30 天 + 事件量减半，
  报告里注明样本收缩。
- **远程执行选项**：管道可跑在 Grok 节点（美国出口直连已验证；本任务全部
  是公共只读数据，符合其远程边界），由它按 owner 批准的任务范围执行并出 PR。

## 7. 时间表与权限

- 预算 5–7 个工作日（§6 契约确认 1 天，可与拉数据并行）。
- 只读研究：不修改 `strategy_registry.json`、不改任何策略配置、
  不产生任何交易意图；`gmgn_copy_paper.py` 的账本本任务不使用。
- 批准后先开分支执行，结论报告走 PR 由所有者合并（owner 合并制）。
