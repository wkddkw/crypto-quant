# 开源方案调研与快速迭代建议

调研日期：2026-09-05。证据来自本次 GitHub 官方 API、项目官方 README、文档及源码读取。
本文是研究和实施建议，不是已完成的接入、策略收益验证或实盘授权。
本次未重新读取远程账本，不更新对 Grok 当前部署、余额或收益的判断。

实施追记：本日随后已实现 `research_runner.py` 的独立趋势研究入口，使用及准确范围见
[RESEARCH.md](../../RESEARCH.md)。下文保留调研时的建议；Carry 等后续范围未因此自动完成。

## 1. 结论

可以缩短工程迭代和淘汰无效策略的时间，不能由此承诺快速、稳定盈利。
优先借鉴 Freqtrade 的验证流程、Hummingbot 的执行生命周期，保留当前轻量账本和治理入口。
VectorBT 可用于隔离的批量研究，但当前尚未证明本项目的计算速度是主要瓶颈，不应先做全量迁移。

现有趋势策略继续冻结运行。新候选先离线验证，晋级需满足 `strategy_registry.json`；
12 周趋势观察、Carry 的 6 个月数据及完整交易条件不会因为工程加速而自动缩短。
不同账户保持独立，不把方向盘、Carry、GMGN 或 Polymarket 收益相加。

## 2. 可借鉴项目

| 项目 | 已核实能力 | 对本项目的用途 | 决策 |
|---|---|---|---|
| [Freqtrade](https://github.com/freqtrade/freqtrade) | OKX 在支持清单中；backtesting、dry-run、lookahead-analysis、recursive-analysis | 数据截断一致性、指标预热稳定性、实验导出和模拟验证流程 | 第一优先；先吸收方法，需要第二引擎交叉验证时再做隔离适配 |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | 交易所连接器、V2 executors、现货/永续套利与资金费差示例 | 交易腿状态、预算检查、费率周期归一化、费用与资金费分账 | 第二优先；借鉴设计，暂不部署真实执行器 |
| [VectorBT](https://github.com/polakowo/vectorbt) | NumPy/Numba 批量回测、多资产配置广播；README 还描述可选 Rust 引擎 | 少量预先声明候选的成本、延迟、参数邻域实验 | 可选研究依赖；先测现有实现耗时，不直接替换模拟账本 |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | 事件驱动回测/执行、共享策略代码、OKX 适配器 | 将来订单簿级回放、成交回报和重启对账 | 当前暂缓整套接入；迁移成本不利于本轮快速验证 |
| [HftBacktest](https://github.com/nkaz001/hftbacktest) | 行情/下单延迟、队列位置、订单簿回放 | 将来检验 maker、网格和做市的成交真实性 | 当前暂缓；需要 tick/L2/L3 数据，README 的实盘示例为 Binance Futures/Bybit |
| [Freqtrade Strategies](https://github.com/freqtrade/freqtrade-strategies) | 可复现策略示例，明确为教育用途 | 学习策略接口、构造对照测试 | 不将示例或收益截图作为盈利依据 |

以上未做安装速度、回测性能或收益的实测横向比较；项目自述的速度不是本项目的测速结论。
支持 OKX 也不代表用户的账户权限、地区、具体市场和部署环境已验证可用。

### 重要限制

1. Freqtrade 官方 backtesting 文档明确：价格在 K 线范围内时，默认假设按请求价格成交且无滑点。
   接入后必须统一本项目的手续费、滑点、成交时间、资金使用和最小交易额；不能只比较收益总数。
2. Freqtrade lookahead-analysis 只能检验实际触发的信号，未触发路径可能漏检；它不是无未来函数的完整证明。
3. Freqtrade 按交易组织策略，本项目按连续目标仓位调仓；适配需要逐时点核对目标、成交和余额，不能直接套接口。
4. Hummingbot `scripts/v2_funding_rate_arb.py` 当前默认连接 Hyperliquid/Binance 永续，默认杠杆 20，
   入场交易利润检查 `trade_profitability_condition_to_enter` 默认 false。
   这是跨所永续费差示例，不是本项目的 OKX 现货多加永续空方案，不能照搬参数或直接运行。
5. HftBacktest 文档明确不模拟自身订单对历史市场的影响。即使有排队、部分成交模型，也不等于实盘成交证明。
6. 本次核实许可：Freqtrade GPL-3.0、Hummingbot Apache-2.0、NautilusTrader LGPL-3.0、HftBacktest MIT。
   VectorBT 当前为 Apache 2.0 加 Commons Clause，限制提供价值主要来自该软件的收费产品/服务，
   不能按无附加条件的 Apache 项目处理。正式引入须锁定版本并保留相应许可。

## 3. 加速应落实为一条可重复验证流程

目标：从手工改规则、看一张报告，变成每个候选都有可复查的完整证据包。

```text
预先声明研究假设和有限候选
  -> 冻结行情、代码、参数、时间切分与费用假设
  -> 数据质量/未来数据/预热长度检查
  -> 历史开发与滚动验证
  -> 成本、延迟、参数邻域压力测试
  -> 生成候选与固定基准的比较报告
  -> 不通过则归档淘汰；通过则提交独立 shadow 提案
  -> 所有者审批与既定观察期
```

建议新增独立研究入口 `research_runner.py`，但本次尚未实现。输出仅写入
`data/research/<experiment_id>/`，不触碰 `data/paper/`、`data/trend_paper/` 或 `data/carry/`。

每份实验产物包括：

- `manifest.json`：研究假设、Git 提交、数据文件哈希、获取时间、来源、数据截至时间、候选列表、参数及成本。
- `splits.json`：开发/滚动验证/未触碰测试的边界，以及每次查看测试集的记录。
- `checks.json`：缺口、未收盘数据、未来数据检查、预热差异和字段有效性。
- `trades.csv`、`equity.csv`：完整成交和连续净值，资金费用与其他成本单独列出。
- `comparison.json`、`report.md`：各折结果、连续 OOS 曲线、基准、回撤、费用、换手、样本数、淘汰原因。

已在原报告中看过的历史区间不能重新宣称为“从未看过的最终样本外”。
历史滚动验证仍可作为开发证据；最终检验需使用确实未参与选择的数据和后续前向记录。
各折年化算术平均不等于连续账户复合年化，不混用两个指标。

### 第一轮只检验有限假设

| 候选/基准 | 研究问题 | 边界 |
|---|---|---|
| 同成本 BTC 买入持有 | 复杂策略是否优于最简单基准 | 固定起点、资金和估值价格 |
| 现有 trend_only | 当前趋势逻辑有无稳定的样本外增益 | 参数冻结，独立 shadow 继续观察 |
| MA200 简单择时对照 | 复杂动量/突破部分是否增加净收益 | 只作研究对照，不切换现有账户 |
| trend_only 加波动率仓位上限 | 能否在净收益可接受时减少回撤 | 新研究候选，参数事前固定，不直接更改现有策略 |

上述候选数量是首轮研究工作量建议，不是新增的策略晋级规则。
BTC/ETH 共振、同一天的多次扫描、重叠持有期不能当作大量独立样本。
不执行几千次参数搜索后只保留最优结果；所有试验和失败结果保留，避免隐藏多重选择。

### 成本和执行检查

- 以当前账本成本为基线，另做预先声明的滑点放大和延迟成交情景；情景参数不是实盘配置变更。
- 策略和买入持有采用相同成本口径，区分持仓估值、已实现损益和强制清仓后的价值。
- 记录扣交易成本收益，再单列服务器、数据和 LLM/API 的固定运行费用。
- 未来若测试限价单，不允许“碰到价格就全部成交”；必须有未成交、部分成交、排队和对冲腿失败假设。
- 信号相同但回测/模拟收益不同，要能归因到执行时刻、价格、成本、数量取整或数据版本。

## 4. Carry 借鉴方案

第一阶段仅对已有 OKX BTC/ETH 范围做独立只读机会研究，不扩大到跨所搬砖或自动下单。
ETH 机会扫描不等于已有 BTC Carry 账户获准交易 ETH。

借鉴 Hummingbot 的费率周期归一化和执行分层，记录：

- 实际结算费率、结算周期及采集时刻；未来显示费率只是估计，不提前计入账本。
- 现货/永续可执行 bid/ask、时间差、可成交深度、基差和价格新鲜度。
- 开平两腿费用、滑点、保证金资金占用与负资金费情景。
- 不同历史时段的资金费持续性，以及净 edge 分布，而不只把单次费率乘成年化。
- 按机会持续区间和完整开平统计有效机会，连续小时命中同一机会不重复包装成独立交易。

输出先回答“没有交易是缺数据、执行限制、成本过高，还是市场确实没有机会”。
空仓本身可以是正确结果，不能通过降低注册表门槛来制造成交。

## 5. 建议实施顺序

以下为约 5-7 个工程工作日的工作量估计，尚未实施，不是盈利倒计时。

| 顺序 | 产物 | 验收 |
|---|---|---|
| 第 1-2 日 | 冻结数据和实验 manifest；统一研究入口 | 同数据/代码/参数重复执行，成交与净值一致；不写正式账本 |
| 第 3-4 日 | 少量候选滚动验证、偏差检测、成本/延迟压力报告 | 能解释失败和各折差异；不把看过的测试集重新封装成未触碰 OOS |
| 第 5 日 | 回测与独立模拟的逐时点对账 | 信号、调仓频率、成本和资金口径一致；差异有明确原因 |
| 第 6-7 日 | BTC/ETH Carry 只读机会摘要及每周研究结论 | 明确样本有效性、成本后机会及剩余阻断；不自动改变策略 |

若既有 pandas/NumPy 回测已足够快，则不引入 VectorBT；若存在实际性能瓶颈，再在隔离研究环境验证。
Grok 可按现有授权维护数据、运行固定实验和总结异常，但不能每天自行换最优参数或自动晋级。
不在本机另启调度器；远程部署和调度状态需在具体实施时重新核实。

## 6. 盈利的资金账

用户实际本金、月净收入目标和运行成本尚未确认。以下 $500 仅取项目模拟规模作算术示例。
假设账户收益率已扣交易成本，但未扣运行费用；数字不是预测，也不表示每月收益均匀。

| 假设账户年收益率 | $500 的年收益 | 除以 12 的月平均 |
|---|---:|---:|
| 5% | $25 | $2.08 |
| 10% | $50 | $4.17 |
| 20% | $100 | $8.33 |

若每月固定运行费用为 $10，$500 本金仅覆盖此费用就需要约 24% 的简单年收益。
若想每月净赚 $100，$500 本金在未计运行成本前就需要 240% 的简单年收益，不能作为稳定目标。
这不是增加本金的建议，而是区分“策略有微小正期望”和“项目产生有意义净收入”。

Carry 的资金费年化通常按对冲名义本金计算，账户还要占用现货资金和保证金，
不能把费率年化直接当作全账户年化收益。

当前合理目标是尽快得到可重复的扣成本结果，及时停止无效投入；实际开始盈利仍取决于行情、
策略增益、执行成本和足够的前向证据。开源软件本身不能提供这些保证。

## 7. 官方证据链接

- [Freqtrade README / 交易所与功能](https://github.com/freqtrade/freqtrade/blob/develop/README.md)
- [Freqtrade 回测假设](https://github.com/freqtrade/freqtrade/blob/develop/docs/backtesting.md#assumptions-made-by-backtesting)
- [Freqtrade 未来数据检查及限制](https://github.com/freqtrade/freqtrade/blob/develop/docs/lookahead-analysis.md)
- [Freqtrade 指标递归/预热检查](https://github.com/freqtrade/freqtrade/blob/develop/docs/recursive-analysis.md)
- [Freqtrade 策略示例声明](https://github.com/freqtrade/freqtrade-strategies/blob/main/README.md)
- [Hummingbot V2 与 executors 概述](https://github.com/hummingbot/hummingbot/blob/master/README.md)
- [Hummingbot 资金费套利示例源码](https://github.com/hummingbot/hummingbot/blob/master/scripts/v2_funding_rate_arb.py)
- [Hummingbot 现货/永续套利源码](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy/spot_perpetual_arbitrage/spot_perpetual_arbitrage.py)
- [VectorBT 功能说明](https://github.com/polakowo/vectorbt/blob/master/README.md)
- [VectorBT Commons Clause 许可](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md)
- [NautilusTrader 官方说明](https://github.com/nautechsystems/nautilus_trader/blob/develop/README.md)
- [HftBacktest 官方说明](https://github.com/nkaz001/hftbacktest/blob/master/README.rst)
- [HftBacktest 撮合假设和市场冲击限制](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)

这些链接指向随项目更新的分支或文档，具体引入时应锁定提交及依赖版本。
本次部分 hummingbot.org 历史文档 URL 返回 404，已改读官方仓库源码，不把失效页面作为证据。
