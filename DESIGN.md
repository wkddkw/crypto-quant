# Crypto Quant 复利/套利系统 — 设计大纲 v0.1

日期: 2026-08-30 | 状态: 设计稿, 待定参数见 §9

## 0. 定位: 双引擎

| 引擎 | 赚什么 | 是否预测 | 预期 |
|---|---|---|---|
| 稳定腿: 资金费率套利 | 现货多头 + 等值永续空头, 吃正 funding | 不预测 | 年化约 5–20%, 阶段性更差(负 funding 期) |
| 进攻腿: 方向择时 | 1天~1周级别的 多/空/平 | 弱预测(仓位化) | 目标: 跑赢买入持有、回撤减半 |

复利 = 正期望 × 仓位管理 × 不死。风控优先于信号。

**引擎结论 (2026-08-30)**: 核心引擎 = 条件化资金费率套利(占 70–80%, 只在 funding 分位高时开仓), 卫星 = 趋势择时(20–30%); 趋势信号兼作 carry 开关(负 funding 段退出)。择时只保留趋势类因子, 情绪/事件类仅做辅助权重。

**现货为主约束 (2026-08-30 确认)**: 主交易所在 OKX、以现货为主 → 进攻腿只做 多/空仓(long/flat), 信号输出域 [0, 1], 不做借币做空; carry 腿需要开通 OKX 合约交易权限(现货多 + 等值永续空, delta 中性, 隔离保证金 ≤2x), 是否开通由用户在 P2 数据研究后决定——不开通则砍掉 carry 腿, 预期收益下降但系统大幅简化。

非目标: 高频交易、跨所搬砖(延迟/费用/提币风险, 散户已无空间)、预测具体价格点位。

## 1. 架构 (七层)

```
L0 配置   config.json: 代理、品种清单(v0); API key(仓库内只放示例)、风控参数、仓位上限
L1 数据   data/: OHLCV、funding、OI、多空比、F&G、事件日历 → Parquet + SQLite, 增量更新
L2 因子   features/: 技术动量 / 情绪反指 / 杠杆资金 / 周期位置 / 事件
L3 信号   signals/: 打分制 v0 → 统计模型 v1, 输出目标仓位 [-1, +1], 不输出价格
L4 回测   backtest/: 向量化回测 + walk-forward + 成本模型, 严格防未来函数
L5 风控   risk/: 波动率目标仓位、凯利上限、熔断、降仓
L6 执行   execution/: paper → testnet → 小实盘; 幂等下单、每日对账
L7 运维   ops/: cron/launchd 调度、每日报告(Telegram/邮件)、异常告警、日志
```

核心循环: 每天定时 → 拉数据 → 算因子 → 出目标仓位 → 与当前仓位比对 → 差异下单 → 记录 + 日报。

技术栈: Python 3.11 + ccxt + pandas/polars + Parquet + 自写向量化回测(日线频率不需要 event-driven 重框架) + cron + python-telegram-bot(可选)。

## 2. 数据源 (v0 全免费)

| 数据 | 用途 | 来源 | 频率 |
|---|---|---|---|
| 日线/4h K线 (BTC/ETH/SOL) | 主行情 | ccxt → Binance/OKX 现货 | 日增量 |
| 永续资金费率历史 | 情绪反指 + 套利腿 | 交易所 futures API | 8h |
| 持仓量 OI | 杠杆拥挤度 | 交易所 futures API | 日 |
| 多空比 | 情绪 | Binance futures API | 日 |
| 恐惧贪婪指数 | 情绪 | alternative.me | 日 |
| 事件日历 | 政策/宏观/暴雷 | 手工 events.csv (v0 人工标注) | 事件 |
| 宏观 (v1) | 利率/DXY/纳指 | FRED / yahoo | 日 |

历史深度: 尽量 2017 起(覆盖约 3 轮牛熊), 至少 2019。

## 3. 因子库 v0 (对应四个参照方向)

- **趋势/动量**(历史上最稳的一类): 均线状态、N 日波动率调整动量、Donchian 突破
- **市场情绪**(反指为主): F&G 极值(≥80 / ≤20)、funding 历史分位、多空比极值
- **杠杆/资金**: OI 增速、funding×OI 组合
- **4年周期**(样本极少, 低权重): 距最近减半月数、200 周均线偏离度
- **事件**(样本少, 低权重): 事件前后 N 日收益统计、事件日 flag
- **波动率**: ATR / 已实现波动 → 用于仓位计算, 不是方向信号

纪律: 每个因子必须先写一句"为什么它应该有效"(事前逻辑)再看数据; 没逻辑的不进库。

### 3.1 因子生命周期与状态适应 (2026-08-30 补充)

概念分层: **1 个因子 = 1 条从原始数据算出的时间序列(单指标、归一化)**。多因子加权组合只发生在信号层(L3), 不允许在因子内部揉多个指标——否则某部分失效时无法归因、无法单独退役。

因子库三个来源(因子库是代码资产, 不是可下载资产):
1. API 直接给因子值: Coin Metrics community (MVRV、活跃地址等)
2. 开源库计算技术类: pandas-ta / TA-Lib (均线、ATR、Donchian 等)
3. 文献定义 + 自写: crypto 特有因子(funding 分位、OI 变化)每个 5–20 行代码

v0 因子清单:

| 因子 | 定义 | 事前逻辑 | 主要适用状态 |
|---|---|---|---|
| trend_ma | 价格 vs MA200 / MA50 状态 | 趋势风险溢价 | 趋势市 |
| mom90 | 90 日波动率调整动量 | 行为延续 | 趋势市 |
| donchian20 | 20 日唐奇安通道突破 | 追势 | 趋势市 |
| funding_pct | funding 30 日滚动分位 | 多头拥挤付费→反指 | 过热期高分位 |
| fg_extreme | F&G ≥80 / ≤20 | 情绪反向 | 极端区 |
| oi_chg | OI 7 日变化率 | 杠杆堆积→顶部风险 | 顶部识别 |
| mvrv_z | MVRV 4 年 z 分 | 链上持币浮盈, 周期顶底 | 月频/周期级 |
| halving_m | 距最近减半月数 | 周期背景 | 极低权重 |
| rv | 已实现波动率 | 只用于仓位, 非方向 | 全状态 |

状态(regime)机制:
- 定义 2–3 个简单可观测的状态标签(趋势市 / 震荡市 / 危机), 由 价格-MA200、波动率分位 等规则产生; 状态规则参数 ≤3 个, 防止状态识别本身过拟合
- 因子权重 = f(状态) + 滚动 IC: 分状态评估每个因子历史 IC, 在无效状态权重 = 0
- 示例: 趋势因子在震荡市权重→0; 情绪反指只在极端分位启用; MVRV 类只在月频起作用

失效对策(学术诚实: "因子择时"大多数尝试是失败的, 所以只做机械机制, 不追每期最优因子):
1. 滚动重估: walk-forward 月频再校准权重, 样本外持续无效→自动降权
2. 退役规则: 连续 3 个月滚动 IC<0 → 权重归零, 冷却 3 个月后才可回归
3. 组合分散: 趋势 + carry + 情绪反指是低相关组合; carry 本身就是对"择时因子失效"的对冲
4. 禁止行为: 回看历史挑"每个时期最有效的因子"拼出完美回测——这是过拟合快车道

## 4. 信号与仓位

- v0 打分制: 每因子输出 -2..+2, 加权求和 → 信号强度 s ∈ [-1, 1]; 现货模式映射为目标仓位 [0, 1](s<0 一律 = 空仓持币), 合约模式才允许负仓位
- 目标仓位 = s × min(波动率目标 / 实际波动, 上限), 凯利分数减半封顶, 杠杆 ≤ 2x
- v1 再考虑 logistic / 梯度提升(数据量小, ML 放后, 防过拟合)
- 调仓频率: 日线收盘后一次(先不做小时级)

## 5. 回测规范 (防自欺, 本项目最重要一节)

1. 未来函数: 信号 t 日只用 ≤ t-1 收盘数据, 按次日价格成交
2. 成本: 现货 taker 0.1%/边, 合约 taker ~0.05%/边; 加滑点, 往返保守按 0.15–0.2% 计
3. walk-forward: 滚动 3 年训练 / 1 年验证; 另留最后 12 个月为最终样本外, 全程只准看一次
4. 参数纪律: 单策略参数 ≤ 4 个; BTC 有效且 ETH/SOL 大致同向才通过
5. 淘汰规则: 样本外无效 → 换因子, 禁止调参硬凑

## 6. 风控

- 单日 -5% / 单周 -10%: 停止开新仓
- 总回撤 -25%: 仓位减半, 人工复盘后恢复
- API/网络异常: 停止开新仓 + 告警 + 人工介入; 不做自动市价平仓(避免故障时乱平)
- 极端行情预案(312/LUNA/FTX 型跳空): 永续对冲腿必须留足保证金倍数
- 实盘冷启动: 总资金 5–10% 起步, 3 个月稳定再放大

## 7. 执行与运维

- 幂等: clientOrderId = 策略名+日期+序号, 重复请求去重
- 对账: 每日核对本地成交记录 vs 交易所, 不一致即告警
- 状态落盘: 持仓/挂单/当日熔断状态 → SQLite, 重启后先对账再运行
- 调度: launchd/cron 每日固定时间; 日报: 持仓、PnL、信号明细推 Telegram
- 环境: 先本机 Mac, 稳定后迁 VPS

## 8. 路线图 (每阶段有过关/淘汰标准)

| 阶段 | 内容 | 验收标准 | 预估 |
|---|---|---|---|
| P0 | 环境 + 数据管线 | 一键增量更新, 2019→今全量落库 | 1–2 天 |
| P1 | 回测框架 + 基线策略 | 含成本的 buy&hold / DCA / 均线基线可信 | 2–3 天 |
| P2 | 因子研究 | ≥2 个因子样本外有效(可弱) | 1–2 周 |
| P3 | 信号合成 + 仓位管理 | 样本外 Sharpe>基线, 回撤<持有一半 | ~1 周 |
| P4 | 模拟盘 | 2–4 周, 模拟与回测偏差可解释 | 2–4 周 |
| P5 | 小实盘 | 3 个月风控不误触发, 再评估放大 | 持续 |

## 9. 参数确认状态 (2026-08-30)

已确认:
- 交易所: OKX(公共数据已实测走代理可用, 见 §11)
- 交易习惯: 以现货为主 → 进攻腿 long/flat(见 §0)
- 运行环境: 本机 Mac(常开) + 代理 127.0.0.1:10808; 稳定运行 3 个月后评估迁 VPS

待定(不阻塞 P0–P2):
- 是否开通 OKX 合约权限 → 决定 carry 腿去留(P2 数据研究完成后决定)
- 资金量级(仅影响 P5 实盘的滑点假设与单所上限)
- 合规与资金: 自行评估所在地监管, 只投亏得起的钱

## 10. 诚实预期

- "每天/每周都赚"的系统不存在; Sharpe ≈ 1、样本外年化跑赢持有且回撤减半, 已是很好的结果
- 历史回测 ≠ 未来; 本项目 80% 的工作量在数据质量和防过拟合, 不在"预测模型"
- 4 年周期只有约 4 个样本, 只能当背景权重, 不能当主信号

## 11. 数据源实测 (2026-08-30, 本机网络)

| 来源 | 实测结果 | 说明 |
|---|---|---|
| OKX v5 公共 API | ✅ 走代理可用 | K线(1Dutc)现货 2018-01→今 / 永续 2020→今; funding 仅最近 ~3 个月, 作实盘真实值, 日常累积 |
| Deribit funding 历史 | ✅ 走代理可用 | 小时费率聚合 8h, 2019-04-30 起连续 7.3 年, 作研究代理源; Bitget/Gate/KuCoin/MEXC 实测均不可用或无深历史 |
| Binance API | ❌ 代理出口为受限地区(疑似美区) | 换非美节点可用; OI/多空比更全但历史仅 30 天 |
| Bybit API | ❌ CloudFront 国家封锁 | 同取决于出口节点 |
| alternative.me F&G | ✅ 走代理可用 | 当前 69 (Greed), 2018-02 起全历史(历史有 2 天缺口) |
| Coin Metrics community | ✅ 免代理直连可用 | MVRV/活跃地址, 数据延迟 1–2 天, BTC 自 2010 年起 |

网络策略: 所有采集器显式走本机代理 127.0.0.1:10808 (HTTP/SOCKS 同端口, V2Ray/Xray 型); ccxt/requests 配 proxies 或 env HTTP(S)_PROXY; 迁 VPS 后可去掉。WebFetch 等不走系统代理的工具会直连超时, 属预期。

当前快照: OKX BTC 资金费率 ≈0.003–0.004%/8h → 年化 ≈3–5%, 处于 carry 平淡期, 印证条件化开仓(低分位不开仓)的必要性。
注意: OKX rubik 持仓量/多空比端点历史深度有限 → P0 第一天就要开始日积攒, 这是最容易后悔没早攒的数据。

## 12. 因子文献参考 (去哪找"值得测的因子")

- 趋势/动量: Moskowitz–Ooi–Pedersen "Time Series Momentum"; Liu–Tsyvinski "Risks and Returns of Cryptocurrency" (RFS 2022)
- Carry: Franz–Valentin "Crypto Carry" (SSRN)
- 币圈因子框架: Liu–Tsyvinski–Wu "Common Risk Factors in Cryptocurrency" (JF 2022)
- 链上周期: Coin Metrics community API (MVRV CapMVRVCur / 活跃地址 AdrActCnt); checkonchain / lookintobitcoin 仅人工参考
- 宏观 (v1): FRED API (利率/DXY) + yfinance (纳指/VIX/黄金)
- 事件: 手工 events.csv + CoinDesk RSS 自动抓取; Google Trends (pytrends, 限流严重, v1)

## 13. 评审记录 (2026-08-30, 确认 OKX + 现货为主后)

1. 无做空能力: 现货模式只能 long/flat, 熊市只能避险不能盈利; 回测一律按此约束, 不测需要空头的策略
2. carry 腿前置条件: 开通 OKX 合约交易权限 + 理解保证金机制; 隔离保证金、名义杠杆 ≤2x、留足缓冲; 极端行情有追加保证金风险 → 这是 carry 腿的真实成本
3. 费率模型(OKX 普通档): 现货 taker 0.10% / maker 0.08%; 永续 taker 0.05% / maker 0.02%; carry 开+平两腿往返 ≈0.24–0.30% → carry 持仓以周为单位, funding 年化 <8% 不值得开仓
4. 代理是单点: 采集器需代理健康检查 + 重试 + 数据新鲜度告警(日线/F&G 超 2 天未更新即提醒)
5. OKX 接口注意: /market/candles 单页 ≤300、/history-candles ≤100 需翻页; K线必须用 `1Dutc`(默认 `1D` 是 UTC+8 边界, 与 F&G/链上数据差 8 小时); funding 单页 ≤100; 最新一根K线未收盘必须丢弃(防未来函数); 公共接口限频需限速(采集器已内置)
6. 空仓期资金停在 OKX 的 USDT: 交易所 + 稳定币双重敞口, 无解只能限敞口 → 单所资金上限是铁律, 后期可两所分散
7. API key(P4/P5 才需要): 只开交易权限、禁提币、绑 IP 白名单; key 不进 git
8. 时区: 全系统 UTC; OKX 日线收盘 = 北京时间 08:00, 定时任务设在北京时间 08:05
9. P4 模拟盘: OKX demo trading 可用(x-simulated-trading 请求头), 无需真金

P0 已完成: collector.py(full/update/status 三命令), 数据落 data/*.parquet, 快速上手见 README.md

10. funding 深历史结论(2026-08-30 实测): OKX 仅 ~3 个月; Bitget(翻页参数被忽略)/Gate(要鉴权)/KuCoin/MEXC(墙) 深历史均不可得; **Deribit 可得 2019-04-30 起连续小时费率**(聚合 8h, BTC/ETH 零缺口) → 因子研究用 Deribit 代理 + OKX 真实值日常累积; 备选: 代理切非受限节点后一次性拉 Binance fapi fundingRate 全历史

## 14. 虚拟操盘试点 (2026-08-30 启动, P4 提前最小化)

- 账户: $500 模拟资金, 本地模拟成交(OKX 现货 taker 0.1% + 滑点 0.03%), 只做 BTC-USDT 现货 long/flat
- 代码: paper_trader.py (run/report/reset); 状态 data/paper/account.json, 报告 data/paper/report.md, 决策日志 journal.md
- 策略 = §3.1 v0 打分制: trend(±3) + mom90(±2) + donchian20(0/1) + funding 反指(±2) + F&G(±2) + MVRV(±2), 满分 ±12 → s = 总分/12 → 目标仓位 = max(0, s)
- 调仓纪律: 目标权重偏离 ≥10 个百分点才动, 防摩擦; 信号只用已收盘日线, 净值用实时 ticker 标记
- 验证闭环: 每小时记录 (时刻, 价格, 信号方向) → 24h 后结算方向命中率 → 汇入报告; 命中率是 P2 因子研究的先导证据
- 调度: 每小时自动巡检(采集更新 → 操盘 → 汇报); 定期复盘时只允许改"规则", 不允许对着亏损改参数
- 迭代原则: 首月只观察和记录; 因子权重调整必须有 journal.md 里的书面理由, 禁止盘中随手改

## 15. P1 回测结果 (2026-08-31, 全样本内初步校准)

窗口 2018-07-30 → 2026-08-30 (8.1 年) | 成本 taker 0.1% + 滑点 0.03% | 信号收盘计算/次日开盘成交/情绪因子滞后 1 天 | 代码 backtest.py, 产物 data/backtest/

| 策略 | 总收益 | 年化 | 最大回撤 | Sharpe | 交易次数 | 累计手续费 |
|---|---|---|---|---|---|---|
| **v0_trend_only** | **+1207%** | **+37.4%** | **-37.9%** | **1.18** | 1601 | 53.7% |
| ma200择时 | +995% | +34.5% | -64.2% | 0.90 | 61 | 38.1% |
| 买入持有 | +850% | +32.1% | -76.6% | 0.76 | 1 | 0.13% |
| v0_full(trend+情绪) | +101% | +9.0% | -15.2% | 0.86 | 1325 | 14.7% |

分年度收益 (v0_trend_only): 2018 0% / 2019 +72% / 2020 +105% / 2021 +35% / 2022 0% / 2023 +58% / 2024 +56% / 2025 -1% / 2026 +7% → **无一自然年亏损超 1%**, 回撤为持有一半。

TH 敏感度 0.10→0.40: 年化 33.7–40.9%, Sharpe 1.08–1.21, MDD ≈-37% → 参数平台而非尖峰; 维持 0.10。

结论与 P2 议程(书面依据, 改规则须引用本节):
1. **情绪因子作方向因子失败**: v0_full 在每个牛市年都被拖累(2020 +16% vs +105%), 熊市年无增益。原因 = 反指在牛市中长期为负(§3.1 设计阶段已预警的事前逻辑, 现被数据证实)。
2. 生产候选 = **v0_trend_only**; 情绪因子降级为"减震器"(仅极值分位如 ≥97.5%/≤2.5% 允许压仓, 永不加仓)——该改造须 P2 walk-forward 通过后才切换。
3. 主要摩擦是手续费(累计 30–54%): P2 测试周频调仓/带状缓冲降频。
4. 过拟合防线: 以上为全样本观察; walk-forward(滚动 3 年训练/1 年验证) + paper 双确认后才定生产规则。
5. paper 账户保持 v0_full 规则不变继续验证, 仅新增对照信号 s_trend 记录(纯埋点, 非规则变更)。

## 16. P2 验证结果 (2026-08-31)

代码: `p2_research.py`; 报告: `data/backtest/p2_report.md`。本节用于决定研究方向, **不等于生产策略批准**。

### Walk-forward

滚动 3 年训练窗按 Sharpe 选择候选, 下一自然年验证(2021–2026): trend-only 被选 4 次, buy-and-hold 被选 2 次; OOS 平均年化 +10.8%、平均年内回撤 -30.3%、Sharpe 0.44。2022 和 2026 训练选择 buy-and-hold, 验证分别 -65.5% 和 -18.4%, 说明“根据近期 Sharpe 自动切换策略”目前不可靠, 不进入生产。

### 跨币种

ETH 同规则 trend-only: 年化 +37.4%、回撤 -42.6%、Sharpe 1.00, 买入持有年化 +22.9%、回撤 -81.7%; 2022 年 ETH 仍亏 -6%,不是无风险。与 BTC trend-only 日收益相关性 0.63, 不是独立分散资产。

### 情绪减震器与降频

- 过热极值压半仓: 年化 +29.2%、回撤 -39.0%、Sharpe 1.03, 低于无情绪基线(37.4%/−37.9%/1.18), 暂不切换。
- 周频调仓: 交易 279 次、累计手续费 20.7%, 年化 31.3%、回撤 -33.8%、Sharpe 1.04; 相比日频手续费 53.7% 大幅下降但收益也下降, 作为 P3 候选而非结论。

### P2 结论

1. 研究首选仍是 trend-only, 但必须继续做滚动样本外和纸面验证; 全样本 +1207% 不能作为未来收益承诺。
2. 动态“选近期最优策略”淘汰; 情绪因子不再做方向加分, 只保留极值风控候选。
3. P3 先做固定规则的周频/带状缓冲版本, 与当前 v0_full 并行记录，不修改 $500 账户仓位规则。
4. 当前账户继续仅模拟，不接 OKX API key、不发送真实订单; 任一生产候选需通过样本外、模拟稳定性和风险门槛后才考虑。

## 17. 开源项目适配

完整评估见 `OPEN_SOURCE_ADAPTATION.md`。结论: 不整套搬入股票量化框架，保留当前轻量 OKX 核心。

- 立即吸收 Qlib 的研究纪律(固定时间切分、实验 manifest、特征快照)和 PanWatch 的监控/决策卡片思路。
- TradingAgents 只作为每日低频研究 memo 旁路，不能修改自动仓位、不能下单。
- Kronos 作为严格 OOS 的预测挑战者，不能直接变成生产信号；验收看扣成本后的可交易收益/回撤，不看裸方向准确率。
- vn.py/Lean 的订单状态机、成交回报、对账思想在 P4 借鉴；当前日线纸面盘不引入完整框架。
- AKShare/easytrader 面向股票或 A 股执行，对 OKX crypto 不适用；只有增加 A 股账户时隔离引入。

## 18. 当前范围(精简版)

完整纳入/排除清单见 `SCOPE.md`。主线已收敛为: **OKX 资金费率套利**，先新增独立 carry paper ledger，再考虑 demo/实盘。

纳入: OKX 现货/永续价格与 funding、Deribit funding 研究代理、净 edge、现货多+永续空、手续费/滑点/基差/保证金模型、轻量订单状态机/对账/熔断、Qlib 研究纪律、PanWatch 监控形式。

不纳入当前主线: Polymarket 15m、TradingAgents 自动决策、Kronos 直接下单、AKShare/easytrader、整套 Qlib/vn.py/Lean、情绪/政策/4年周期开仓、现货趋势策略冒充套利。当前 `$500` 账户仍是独立的 BTC 方向基准盘，不是资金费率套利。

波动结论: funding carry 不是“波动越大越赚钱”。低到中等波动、正 funding 持续、基差稳定且净 edge 足够时更适合稳定 carry; 高波动虽可能抬高 funding, 也会放大基差、滑点、保证金和强平风险。所有开仓只看扣除全部成本与风险缓冲后的净 edge。

## 19. Carry Paper Ledger (2026-08-31)

代码: `carry_trader.py`; 专用配置: `carry_config.json`; 使用和限制: `CARRY.md`; 状态目录: `data/carry/`。完全隔离于 `data/paper/` 的 BTC 方向基准盘。

- 实时公共输入: OKX 现货/永续 ticker bid/ask、swap mark price、index price、当前 funding/下一结算; 每小时落盘快照。
- 账本: 现货数量、永续空头数量、开仓价、隔离保证金、已结算 funding、费用、basis/对冲残差、权益和 append-only 事件。
- 开仓: funding 年化 ≥8%, 年化净 edge ≥2%, 基差 <3%, 数据新鲜。净 edge 统一为 funding 年化减去按预计持仓天数年化的往返成本和基差缓冲。
- 近期历史回放: `carry_replay.py` 使用 2026-05-25 起的实际 OKX funding 与本地 UTC 日K，独立写入 `data/carry_replay/`，绝不改写实时账本或将 Deribit 代理当作 OKX 收益。当前 291 个结算样本均未通过扣年化成本后的 net edge 门槛，模拟应为零开仓、零收益；该结果是过滤器证据，不是策略失败或未来收益结论。
- 异常处理: 基差越界或 funding 恶化且报价可用时按模拟 bid/ask 加滑点平仓；市场快照失败时只进入 `halt`，不伪造无法定价的平仓。每小时任务固定为先更新数据，再结算 carry，再运行方向盘。
- 方向基准盘: 小于 $1 的模拟调仓会作为 `hold(dust)` 跳过且不改变现金、BTC 或成交记录。
- 当前首次真实公共行情验证: funding 年化约 7.5%, 30 天持有期年化成本/缓冲约 15.8%, 净 edge 约 −8.3% → **正确空仓**, 不为“正 funding”硬开。
- 已通过 7 个单元测试: 两腿平行价格变动抵消、basis 残差、funding 符号、资本占用、标记无副作用、开仓资金时间、方向账户路径隔离。连续巡检也确认 `data/paper/*` 未变化。
- 当前限制: 公共 bid/ask+固定滑点模拟，不含真实成交、订单簿容量、真实手续费等级、利息、强平和交易所信用风险; 不接 API key/不发送订单。
## 20. Strategy Governance and GMGN Solana Shadow (2026-09-01)

`strategy_registry.json` is the source of truth for each strategy's fixed version, status, evidence paths, review date, promotion rules, and pause/retirement rules. `governance.py review` only writes a local evidence report; it cannot tune parameters, reallocate capital, or submit orders. This is the boundary for learning: the system may collect evidence, pause a failed strategy, and run frozen challengers in shadow, but it cannot promote an untested rule solely because it recently performed well.

Current status:

- `btc_v0_full`: retired historical baseline. The composite emotion/trend rule was weaker than `trend_only`; it must not be treated as a production candidate.
- `btc_trend_only_shadow`: candidate for a fixed 12-week, non-overlapping paper comparison and a final frozen 12-month OOS review.
- `okx_funding_carry`: observe-only. It requires six months of OKX funding data, 30 conservative eligible observations, 20 reconciled complete paper round trips, and positive net expectancy before any promotion discussion. Existing fixture-tagged carry events are a data-integrity warning, not performance evidence.
- `polymarket_complete_set`: paused. Old observations matched a long-dated Hegseth market through an unsafe substring rule. Only explicit BTC/ETH binary Up/Down markets within 30 minutes of expiry count from now on; at least 500 valid expired markets are required.
- `gmgn_solana_copy`: shadow paper strategy. It freezes a daily Solana Top 100 wallet pool, accepts only fresh buy events, and applies exposure, liquidity, cost, cooldown, and kill-switch rules. It has no wallet, signing, or order execution code.

GMGN live mode may start only after official API terms permit automated monitoring and the documented endpoint, authentication, ranking, wallet-event, pagination, event-ID, price, liquidity, and latency contracts have been placed in `gmgn_config.json`. Missing configuration fails closed as `blocked_config`; it does not issue network requests or create synthetic fills. The default fixture mode exists to verify the audit trail and risk logic without external access.

`daily_report.py` produces Beijing-time Markdown and JSON reports under `data/daily_reports/`. Each report includes channel freshness, upstream errors, signal acceptance/rejection, paper PnL and positions, governance status, review dates, and explicit limitations. Reports must continue to label all current results as paper/research results.


用户提供的 X 帖子指向 `FrondEnt/PolymarketBTC15mAssistant`。源码核验结论:

- 它是 Node.js 实时**交易辅助/分析面板**, 不是完整套利机器人；README 明确描述为 assistant。
- 数据: Polymarket 市场的 UP/DOWN 价格和订单簿、Polymarket/Polygon Chainlink BTC/USD、Binance BTC 参考价; 技术指标包括 VWAP、RSI、MACD、Heikin-Ashi、短线 delta。
- 概率逻辑是透明的加分规则: 价格相对 VWAP、VWAP slope、RSI 及 slope、MACD、Heikin-Ashi、VWAP reclaim 失败 → rawUp; 再按剩余时间收缩到 0.5。
- edge 逻辑是 `模型概率 - 市场归一化价格`; 早/中/晚阶段门槛约为 5%/10%/20%, 并要求最低模型概率约 55%/60%/65%。这是一套可读的筛选规则，不是已证明的 alpha。
- 源码核验到的仓库内容没有钱包签名、Polymarket CLOB 下单、仓位管理、成交回报、结算对账或真实 PnL 账本; 因此不能复现帖子所称的“自动延迟套利”。帖子中的 98% 胜率和单笔收益没有在仓库中得到独立证明。

### 与当前 OKX 项目的关系

1. **不接入当前 `$500` 账户**: Polymarket 15 分钟二元合约与 OKX BTC 现货/资金费率是不同市场、不同结算机制、不同风险模型; 混在一个净值里会污染评估。
2. 可借鉴: 将“模型概率 − 可成交市场价格”定义为 edge, 把交易费用、盘口深度、剩余时间和最低 edge 写进交易门槛; 这对未来 funding carry 的入场门槛也有启发。
3. 可作为独立 challenger: 只做 paper ledger, 记录每个 15m 市场的时点、Chainlink 价格、可成交 bid/ask、模型概率、edge、实际结算和滑点; 至少积累 500–1000 个市场后再评估。
4. **延迟套利必须先证明可交易**: 需要逐 tick 时间戳、Binance/Chainlink/Polymarket feed 延迟、实际 bid/ask、盘口容量、手续费、gas、结算规则和拒单率; 只看屏幕上的价格差不算套利。
5. 该方向最大风险: 15 分钟市场的二元结算、价差/盘口深度、数据源时间不同步、最后几分钟流动性消失、API/RPC 延迟、钱包和智能合约风险; 98% 胜率也可能因少量尾部亏损而整体负收益。

结论: 这个仓库**值得学习 edge、时间衰减概率、盘口数据和实时监控的写法**, 但不能证明帖子宣传的收益，更不能替换当前 OKX 量化/资金费率套利主线。若未来做它，应作为独立 Polymarket paper 项目，从被动记录和结算验证开始，禁止自动实盘。
