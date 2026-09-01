# crypto-quant

币圈量化复利/套利系统。设计与路线图见 [DESIGN.md](DESIGN.md)。

本仓库公开只读。提交权限仅限所有者 `wkddkw` 与其授权的远程 Grok Bot（授权范围、分支规范与红线见 [GROK_REMOTE_COMPLETE_GUIDE_CN.md](GROK_REMOTE_COMPLETE_GUIDE_CN.md) §11.2 与 [GROK_REMOTE_RUNBOOK.md](GROK_REMOTE_RUNBOOK.md) §8）；外部贡献一律通过 fork PR，默认不合并。

## 快速开始

```bash
cd /Users/dkw/.zcode/workspace/default/crypto-quant
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python collector.py full     # 首次全量拉取(约 3-5 分钟)
.venv/bin/python collector.py update   # 日常增量更新
.venv/bin/python collector.py status   # 查看数据状态与缺口

.venv/bin/python paper_trader.py run   # 虚拟操盘一轮($500 模拟账户)
.venv/bin/python paper_trader.py report # 只看报告(data/paper/report.md)
```

## 虚拟操盘(paper trading)

$500 模拟账户, v0 打分制策略(见 DESIGN.md §14), 只做 BTC 现货 long/flat。
状态/报告/决策日志在 `data/paper/`。远程常驻节点通过 `systemd` 每小时第 03 分钟（北京时间）巡检一次（采集 → 信号 → 调仓 → 报告）；本机不再运行定时任务。远程安装见 [GROK_REMOTE_COMPLETE_GUIDE_CN.md](GROK_REMOTE_COMPLETE_GUIDE_CN.md)。

## Funding Carry 模拟账本

独立的 OKX `现货多 + 永续空` funding carry 纸面账本，和上述方向账户完全分开。使用、风险门槛和产物见 [CARRY.md](CARRY.md)。

```bash
.venv/bin/python carry_trader.py init
.venv/bin/python carry_trader.py run
.venv/bin/python carry_trader.py report
```

## GMGN Solana 聪明钱纸面跟随

GMGN 模块是独立的 Solana 链上“聪明钱”纸面跟随研究线。它每日冻结一次 Top 100 钱包，只处理当日钱包池内的买入信号，并在模拟成本、信号时效、流动性、冷却期和组合风险限制都满足时才记录纸面开仓。它不会读取 Fomo 页面、不连接钱包、不保存私钥、不签名，也不会提交真实订单。

```bash
.venv/bin/python gmgn_copy_paper.py init
.venv/bin/python gmgn_copy_paper.py run
.venv/bin/python gmgn_copy_paper.py report
.venv/bin/python gmgn_copy_paper.py status
```

默认 `gmgn_config.json` 是本地 fixture 模式，用于验证账本、筛选与报告。只有确认 GMGN 官方 API 的 endpoint、鉴权方式、排行和交易事件 schema，并设置对应环境变量后，才可将其改为 `live`；任何契约字段或密钥缺失都会写为 `blocked_config`，不发请求、不产生纸面成交。产物位于 `data/gmgn_solana_paper/`，完全独立于其他账本。

## 策略治理与日报

当前没有一条策略可进入实盘。`strategy_registry.json` 固定每条策略的观察窗口、晋级条件和暂停/淘汰规则，`governance.py` 只读生成治理结论，绝不调参或下单。

```bash
.venv/bin/python governance.py review
.venv/bin/python daily_report.py
```

日报按北京时间保存为 `data/daily_reports/YYYY-MM-DD.md` 与 JSON，涵盖每个渠道的数据新鲜度、运行错误、信号与拒绝原因、纸面权益/持仓/成本以及策略复核状态。远程节点在每天 18:00 北京时间生成并由 Grok Bot 交付摘要；本机不再运行 ZCode 定时报告。

## 本地可视化仪表盘

仪表盘只读取远程节点的 `data/` 产物，不访问交易 API、不保存密钥、不下单，也没有重置或策略修改控件。远程每小时任务只更新公开数据和独立纸面账本；通过 Tailscale 打开页面后刷新即可读取最新落盘结果。

```bash
.venv/bin/streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8888
```

浏览器打开 `http://127.0.0.1:8888`。页面包含：carry 与方向盘分账总览、资金费/基差/净 edge、方向信号/预测结算、近期 OKX carry 回放、研究曲线和数据健康状态。Carry 与方向账户收益永不合并；Deribit funding 仅显示为研究代理。

## Polymarket 15 分钟研究模式

新增的 Polymarket 模式是独立的只读盘口研究和纸面账本，不连接钱包、不签名、不下单。它通过 Gamma 获取市场生命周期，通过 CLOB 获取公开 UP/DOWN 订单簿，记录完整集合套利的可成交成本、深度、净 Edge 和接口错误；Temporal Arb 默认关闭，不会把单腿方向暴露当作无风险套利。

```bash
.venv/bin/python polymarket_data.py
.venv/bin/python polymarket_paper.py
```

产物位于 `data/polymarket/`，仪表盘页面为“Polymarket 15分钟研究”。该模式单独按约 10 分钟观察一次；现有 OKX carry 和 BTC 方向盘任务仍保持独立。它当前处于暂停状态：旧样本错误命中了长期 Hegseth 市场，不能计入表现；新版准入要求明确 BTC/ETH、二元 Up/Down 和 30 分钟内到期。累计至少 500–1000 个有效市场、并验证完整成交率、单腿暴露和扣成本净 PnL 后，才评估是否继续投入。


- 交易所公共数据(OKX)和恐惧贪婪指数**必须走本机代理** `127.0.0.1:10808`(config.json 可改);Coin Metrics 直连。
- 数据落在 `data/*.parquet`;状态报告在 `data/status.md`。
- 全系统 UTC:OKX 日线收盘 = 北京时间早上 8 点。远程 scheduler 使用 `systemd` timer，部署步骤见 [GROK_REMOTE_COMPLETE_GUIDE_CN.md](GROK_REMOTE_COMPLETE_GUIDE_CN.md)。

## 目录

```
DESIGN.md          设计大纲(引擎、因子、回测规范、路线图、评审记录)
collector.py       P0 数据采集器
config.json        代理 / 品种 / 采集参数(不进任何密钥)
data/              parquet 数据 + status.md(已 gitignore)
```
