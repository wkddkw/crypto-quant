# crypto-quant 远程 Grok Bot 完整部署与研究交接手册

> 版本：2026-09-01
>
> 代码仓库：[wkddkw/crypto-quant](https://github.com/wkddkw/crypto-quant)
>
> 远程节点定位：持续运行的研究、纸面账本、数据质量与报告节点
>
> 本机定位：通过 Tailscale 私有访问 Dashboard、查看 Grok Bot 日报、按需拉取备份
>
> 当前边界：**不连接钱包、不保存私钥、不签名、不下单、不使用真实资金。**

---

## 1. 总体架构

```text
GitHub public repository
    │
    │ git clone / git pull
    ▼
Remote always-on server (Grok Bot)
    ├── crypto-quant source code
    ├── local paper ledger: data/
    ├── daily research / governance reports
    ├── Streamlit dashboard on 127.0.0.1:8888
    ├── Tailscale Serve private HTTPS proxy
    └── Grok Bot daily report delivery
    │
    │ Tailscale private network / rsync pull
    ▼
Local Mac
    ├── Browser opens private Dashboard URL
    └── Pulls selected reports and timestamped backups
```

职责划分：

- **GitHub**：代码、测试、配置样例、策略规则、部署文档、研究提案。不能保存运行账本、真实 API 凭证、私钥或每日完整交易数据。
- **远程服务器**：唯一运行中的纸面研究节点。它拥有自己的本地 `data/`、日报、Dashboard 和日志。
- **本机**：不运行定时策略任务，不运行常驻 Dashboard。通过 Tailscale 查看远程节点，按需拉取报告或备份。
- **Grok Bot**：研究与运维协作者，可做数据检查、回测、报告、提出研究假设；不能执行交易或自动改策略。

---

## 2. 绝对安全边界

远程节点和 Grok Bot 允许：

1. 拉取公开仓库。
2. 读取公开市场数据。
3. 在官方条款明确允许的前提下读取 GMGN 官方只读 API。
4. 运行回测、fixture、纸面账本、数据质量检查和 Dashboard。
5. 在远程节点本地写入 `data/`、`logs/`、`reports/`。
6. 创建 Git 分支、提交代码、测试、文档和脱敏研究提案。

远程节点和 Grok Bot 禁止：

1. 保存、读取或要求助记词、私钥、钱包导出文件、交易所 API secret、提现密钥。
2. 连接钱包、签名、提交交易、撤单、转账、开合约、使用真实资金。
3. 抓取 Fomo 页面、绕过 Fomo/GMGN/交易所/第三方平台 API 条款、登录限制、速率限制或 robots 规则。
4. 自动调整参数、自动替换钱包池、自动晋级策略、自动分配资金。
5. 把回测、fixture、无效样本、未平仓浮盈或纸面收益写成真实收益。
6. 将 Streamlit 直接暴露到公网，或使用 `tailscale funnel`。
7. 在未审核的情况下直接向 `main` 分支推送代码。

每份日报和研究报告必须包含以下页脚：

```text
本报告仅包含研究与纸面账本数据；未连接钱包、未签名、未提交真实交易。
```

---

## 3. 当前策略结论与优先级

| 策略 | 当前状态 | 能说明什么 | 不能说明什么 | 下一验证门槛 |
|---|---|---|---|---|
| BTC `v0_full` | `retired` | 历史基准和旧纸面账户存在 | 不能证明当前生产候选有效 | 不晋级，仅保留比较 |
| BTC `trend_only` | `shadow` | 全样本回测优于买入持有 | Walk-forward 偏弱，且暂无专用 shadow runner | 至少 12 周固定规则、非重叠日频纸面验证；再做冻结 12 个月 OOS |
| OKX funding carry | `observe_only` | 当前负净 Edge 时保持空仓的过滤逻辑正确 | 尚无有效完整纸面往返交易 | 6 个月数据、30 个保守候选、20 笔可对账且扣成本仍正期望的纸面往返 |
| Polymarket complete-set | `paused` | 两腿公开盘口成本模型存在 | 旧样本误命中 Hegseth 长期政治市场，无效 | 修复后积累至少 500 个正确、已到期的 BTC/ETH 15 分钟市场 |
| GMGN Solana copy | `shadow` | 已有独立账本、去重、延迟、流动性、暴露与成本风控 | 现在仅 fixture；没有真实 API 数据或有效纸面跟单 PnL | 官方只读 API 和权限确认后，30 天完整快照和 100 个独立接受信号 |

### 当前已知数据问题

1. **BTC 日报聚合错误**：旧日报将 BTC `cash` 当成总权益。若存在 BTC 持仓，现金不等于权益；后续报告必须展示现金、持仓市值、标记权益和 PnL 四项。
2. **Carry fixture 污染**：历史事件中存在 `reason:"fixture"` 的记录，不能作为收益或策略表现证据。
3. **Polymarket 旧样本无效**：历史记录误匹配长期 Hegseth 市场，不能计入 15 分钟 BTC/ETH 策略样本。
4. **GMGN 未启用 live**：禁止猜测 endpoint，禁止页面抓取；未完成官方 API 契约时保持 fixture 或 `blocked_config`。
5. **回测不是生产证据**：回测仅生成假设，不能直接作为上线或增加资金的理由。

策略治理的权威配置文件：`strategy_registry.json`。

---

## 4. 远程服务器准备

以下以 Ubuntu/Debian 为例。使用独立非 root 用户 `quant`。

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip rsync curl
sudo useradd --create-home --shell /bin/bash quant
sudo mkdir -p /opt/crypto-quant /opt/crypto-quant/logs /opt/crypto-quant/reports /opt/crypto-quant/backups
sudo chown -R quant:quant /opt/crypto-quant
sudo -iu quant
```

目录结构：

```text
/opt/crypto-quant/
  app/                  # Git clone
  logs/                 # systemd 与任务日志
  reports/              # Grok 可选研究输出
  backups/              # 远程本地账本备份
```

拉取并安装：

```bash
cd /opt/crypto-quant
git clone https://github.com/wkddkw/crypto-quant.git app
cd app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

只有测试通过后才启动 Dashboard 或任何定时研究任务。

---

## 5. Tailscale 私有网络

远程服务器没有公网地址也可以访问。将远程服务器与本机 Mac 加入同一个 Tailscale tailnet。

### 5.1 远程服务器安装和登录

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=crypto-quant-grok --ssh
sudo tailscale status
```

首次运行会给出授权链接。使用与你本机 Mac 相同的 Tailscale 账户完成授权。

不要：

- 不要把 auth key 写入 Git、Markdown、聊天记录或 Grok Prompt。
- 不要给 Grok Bot 管理 Tailscale ACL、添加设备或开启 Funnel 的权限。
- 不要用 `tailscale funnel`。

### 5.2 本机加入 tailnet

在 Mac 安装 Tailscale 客户端，登录相同 tailnet，随后检查：

```bash
tailscale status
ping crypto-quant-grok
```

若没有 MagicDNS，使用 `tailscale status` 显示的 `100.x.y.z` 地址。

### 5.3 ACL 建议

仅允许：

- 你的本机或 owner 设备访问 `crypto-quant-grok:443`。
- 你的管理员设备通过 Tailscale SSH 访问远程节点。

Grok Bot 本身不应有修改 ACL、开启 Funnel 或接管网络的权限。

---

## 6. 远程 Dashboard

Dashboard 是只读页面，不包含交易执行控件。它仍包含本地研究和纸面账本信息，因此必须私有访问。

### 6.1 手动启动测试

```bash
cd /opt/crypto-quant/app
.venv/bin/streamlit run dashboard.py \
  --server.address 127.0.0.1 \
  --server.port 8888 \
  --server.headless true \
  --browser.gatherUsageStats false
```

必须监听 `127.0.0.1`，不要监听 `0.0.0.0`。

### 6.2 通过 Tailscale Serve 私有发布

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8888
sudo tailscale serve status
```

`tailscale serve status` 会显示一个类似下面的私有 URL：

```text
https://crypto-quant-grok.<tailnet-name>.ts.net/
```

在已经登录同一 tailnet 的本机浏览器打开该 URL。

关闭代理：

```bash
sudo tailscale serve --https=443 off
```

### 6.3 systemd 常驻 Dashboard

创建 `/etc/systemd/system/crypto-quant-dashboard.service`：

```ini
[Unit]
Description=Crypto quant read-only dashboard
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=quant
WorkingDirectory=/opt/crypto-quant/app
ExecStart=/opt/crypto-quant/app/.venv/bin/streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8888 --server.headless true --browser.gatherUsageStats false
Restart=on-failure
RestartSec=5
StandardOutput=append:/opt/crypto-quant/logs/dashboard.log
StandardError=append:/opt/crypto-quant/logs/dashboard.log

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-quant-dashboard.service
sudo systemctl status crypto-quant-dashboard.service
```

---

## 7. 数据源与 GMGN 配置

仓库默认 `gmgn_config.json` 是 `fixture` 模式。

在没有确认以下全部内容前，**不得**切换 `mode: "live"`：

1. GMGN 官方 API base URL 和版本。
2. 只读凭证的 header、prefix、有效期、轮换、速率限制。
3. Solana 排行榜 endpoint、Top 100 的固定排序、分页、时间周期和完整性语义。
4. 钱包事件 endpoint、cursor、事件唯一 ID、链上执行时间、buy/sell 语义、mint、价格、流动性、延迟和失败状态字段。
5. GMGN 服务条款允许自动化只读轮询、数据保存和纸面研究。

真实只读凭证只存在远程服务器本地：

```bash
install -m 600 /dev/null /opt/crypto-quant/app/.env
```

示例，仅展示字段结构：

```bash
GMGN_READONLY_API_KEY=replace_with_readonly_credential
```

`.env` 不可提交 Git、不可出现在日志、报告、聊天或 Dashboard 中。

启用 live 前流程：

```text
官方文档与权限确认
  -> 填写所有 API 契约字段
  -> fixture 测试通过
  -> 只观察 1 个完整日周期
  -> 审核分页/重复/延迟/完整性
  -> 启用纸面成交
  -> 至少 30 天 + 100 个独立接受信号
  -> governance review
```

---

## 8. 远程日常任务

当前用户要求：本机不运行 18:00 或每小时策略任务。远程节点是唯一运行的纸面研究节点，承担原本的每小时巡检、日报和研究任务。

启用以下远程任务：

1. **每小时第 03 分钟（北京时间）**：公开数据更新与三账本纸面巡检。
2. **每天 02:00 北京时间**：数据质量和测试任务。
3. **每天 18:00 北京时间**：治理、日报和 Grok Bot 中文摘要。

### 8.1 每小时三账本巡检（替代已关闭的本机 ZCode 任务）

已关闭的本机任务为“每小时虚拟操盘巡检(三账本:OKX+方向+Polymarket)”，执行时点是每小时第 `03` 分钟。远程节点必须使用仓库内的 systemd 模板替代它，不能再在本机创建 ZCode 定时任务。

该任务在一个 `flock` 锁内按顺序运行：

```text
collector.py update       # OKX 等公开数据增量更新
carry_trader.py run       # OKX funding carry 独立纸面账本
paper_trader.py run       # BTC 方向独立纸面账本
polymarket_data.py        # 修复过滤器后的只读市场观察
polymarket_paper.py       # Polymarket 观察账本，不提交交易
```

边界：

- Carry 仍为 `observe_only`，负净 Edge 时保持空仓是正常结果。
- BTC 当前运行的是退休的 `v0_full` 历史基准，不能将其 PnL 视为 `trend_only` 的验证结果。
- Polymarket 仍为 `paused`；小时任务只累计修复过滤器后的有效市场观察，旧 Hegseth 样本不计入业绩或样本门槛。
- **不运行 `gmgn_copy_paper.py run`**。GMGN 只有在官方文档、只读权限、API 契约、分页和延迟语义均确认后，才以单独的已批准节奏启动。
- 脚本锁冲突时会以退出码 `75` 正常跳过并留有 systemd 日志，不允许两个账本写入进程并发运行。

远程安装：

```bash
cd /opt/crypto-quant/app
git pull --ff-only
chmod +x scripts/hourly_observe.sh
sudo install -m 644 systemd/crypto-quant-hourly-observe.service \
  /etc/systemd/system/crypto-quant-hourly-observe.service
sudo install -m 644 systemd/crypto-quant-hourly-observe.timer \
  /etc/systemd/system/crypto-quant-hourly-observe.timer

# 先手动运行一轮，确认公共数据访问、账本和日志均正常。
sudo -u quant /opt/crypto-quant/app/scripts/hourly_observe.sh
journalctl -u crypto-quant-hourly-observe.service -n 100 --no-pager

sudo systemctl daemon-reload
sudo systemctl enable --now crypto-quant-hourly-observe.timer
systemctl list-timers crypto-quant-hourly-observe.timer
```

仓库文件：

```text
scripts/hourly_observe.sh
systemd/crypto-quant-hourly-observe.service
systemd/crypto-quant-hourly-observe.timer
```

### 8.2 每日质量检查

命令：

```bash
cd /opt/crypto-quant/app
.venv/bin/python collector.py status
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python governance.py review
.venv/bin/python daily_report.py
```

检查内容：

- 数据新鲜度与缺口。
- 未来时间戳。
- fixture 混入生产事件。
- 重复事件和重复开仓风险。
- 账户、事件、结算账本是否一致。
- Dashboard 服务和 Tailscale Serve 是否健康。

### 8.2 每日 18:00 报告

报告命令：

```bash
cd /opt/crypto-quant/app
.venv/bin/python governance.py review
.venv/bin/python daily_report.py
```

生成：

```text
data/daily_reports/YYYY-MM-DD.md
data/daily_reports/YYYY-MM-DD.json
data/governance/report.md
data/governance/status.json
```

Grok Bot 通过其自身支持的定时与通知方式交付中文摘要。不要把通知 token、Webhook 或 chat ID 提交到仓库。

### 8.3 systemd 报告服务和 Timer 模板

创建 `/opt/crypto-quant/app/scripts/grok_daily_report.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/crypto-quant/app
.venv/bin/python governance.py review
.venv/bin/python daily_report.py

# 此处由 Grok Bot 自己的发送能力读取当天 Markdown 并交付摘要。
# 不在此仓库中保存 webhook、bot token 或 chat ID。
```

```bash
chmod +x /opt/crypto-quant/app/scripts/grok_daily_report.sh
```

创建 `/etc/systemd/system/crypto-quant-report.service`：

```ini
[Unit]
Description=Crypto quant governance and daily paper report
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=quant
WorkingDirectory=/opt/crypto-quant/app
ExecStart=/opt/crypto-quant/app/scripts/grok_daily_report.sh
StandardOutput=append:/opt/crypto-quant/logs/daily-report.log
StandardError=append:/opt/crypto-quant/logs/daily-report.log
```

创建 `/etc/systemd/system/crypto-quant-report.timer`：

```ini
[Unit]
Description=Generate crypto quant report at 18:00 Asia/Shanghai

[Timer]
OnCalendar=*-*-* 18:00:00 Asia/Shanghai
Persistent=true

[Install]
WantedBy=timers.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-quant-report.timer
systemctl list-timers crypto-quant-report.timer
```

---

## 9. 每日 Grok Bot 报告模板

每天 18:00 北京时间向你发送：

```markdown
# 每日策略报告 YYYY-MM-DD（北京时间）

## 运行总览
- Dashboard: 正常/异常
- Tailscale Serve: 正常/异常
- 数据质量任务: 正常/异常
- 当前 Git commit: <commit>

## BTC direction
- 策略版本与治理状态
- 现金、持仓市值、标记权益、当日/累计纸面 PnL
- 当日信号、调仓、未平仓风险
- 数据新鲜度和异常

## OKX funding carry
- 当前 funding、基差、净 Edge、开仓/持有/暂停原因
- 现金、仓位、已结算 funding、成本、纸面 PnL
- fixture 污染或对账警告

## Polymarket
- 有效市场计数（旧无效 Hegseth 样本不计）
- 候选、拒绝、净 Edge、数据错误
- 当前是否仍 paused

## GMGN Solana copy
- 模式：fixture / blocked_config / observe-only / paper
- 钱包池数量、完整性、信号数量、接受/拒绝原因
- 数据延迟、重复事件、流动性/暴露拒绝
- 纸面仓位、成本、Pnl

## 治理与下一步
- 每条策略状态与下次复核日期
- 当前还缺少的样本量/观察时间
- 阻断项和需人工处理事项

## 最多三条研究假设
- 假设
- 数据来源与样本计划
- 成本/延迟模型
- 通过条件
- 淘汰条件

本报告仅包含研究与纸面账本数据；未连接钱包、未签名、未提交真实交易。
```

---

## 10. Grok Bot 完整角色指令

将以下内容直接交给远程 Grok Bot：

```text
你运行在 crypto-quant 的远程研究节点。

项目路径：/opt/crypto-quant/app
日志路径：/opt/crypto-quant/logs
可选研究输出：/opt/crypto-quant/reports

你是研究、数据质量、纸面策略监控、Dashboard 健康检查和每日中文报告协作者。

允许：
- 读取代码、公开配置、测试、公开数据、远程本地 data/ 纸面账本和本地报告。
- 运行测试、回测、治理、日报、数据质量检查和只读 Dashboard 健康检查。
- 在官方条款和凭证允许的前提下调用 GMGN 官方只读 API。
- 在独立 Git 分支提交代码、测试、脱敏 fixture、研究文档和研究提案。
- 生成 data/research/ 或 /opt/crypto-quant/reports/ 下的研究报告。

禁止：
- 读取、索取、保存或使用钱包私钥、助记词、交易 API secret、提现凭证。
- 连接钱包、签名、下单、撤单、转账、真实资金操作。
- 自动抓取 Fomo 或绕过任何供应商条款、登录、限流和 robots 规则。
- 修改 Tailscale ACL、添加设备、使用 Tailscale Funnel 或公开 Dashboard。
- 直接推送 main 分支。
- 自动调整参数、替换钱包池、改变策略规则、晋级策略或分配资金。

每个结论必须分为：
1. 已验证事实。
2. 假设。
3. 待验证项。
4. 数据来源、截止时间与样本量。
5. 结果类型：fixture、回测、纸面或真实结果。

当前策略状态：
- btc_v0_full：retired，只是历史基准。
- btc_trend_only_shadow：需要固定规则 shadow 验证。
- okx_funding_carry：observe_only；历史 fixture 事件不能计为表现。
- polymarket_complete_set：paused；旧 Hegseth 样本全部无效。
- gmgn_solana_copy：Solana 买入方向的 paper shadow；官方 API 契约未完成前只能 fixture 或 blocked_config。

每天任务：
1. 检查 Dashboard systemd 服务、Tailscale Serve 和报告 timer。
2. 检查数据新鲜度、未来时间戳、重复事件、fixture 污染和账本不一致。
3. 运行或审阅 governance.py review 与 daily_report.py。
4. 在每天 18:00 Asia/Shanghai 交付中文报告。
5. 提出最多三条研究假设。每条必须包含假设、可测试条件、数据源、时间范围、样本量、成本模型、通过条件和淘汰条件。
6. 不得直接给出买卖指令，也不得修改任何策略或配置使其自动交易。

所有报告必须包含：
“本报告仅包含研究与纸面账本数据；未连接钱包、未签名、未提交真实交易。”
```

---

## 11. GitHub 协作与同步规则

### 11.1 `main` 分支

- `main` 是审核后的公共代码和文档。
- 远程服务器更新代码：

```bash
cd /opt/crypto-quant/app
git fetch origin
git status --short
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

- 不要在运行账本有未备份数据时执行 `git reset --hard`。

### 11.2 Grok Bot 修改代码

- 使用分支：`grok/research-YYYY-MM-DD`。
- 只能提交代码、测试、无敏感信息的 fixture、文档和脱敏研究摘要。
- 不能提交 `data/`、`.env`、日志、真实钱包地址、真实交易记录、API key 或任意凭证。
- 提交前必须运行测试。
- PR 内容必须包含：变更目的、影响文件、测试结果、数据截止时间、成本假设、未提交真实交易声明。

### 11.3 报告不进入公开 Git

远程运行报告、纸面账户、原始市场数据和 API 响应全部留在远程 `data/`，不提交到公共仓库。

只有人工审核过且已脱敏的研究摘要可放到分支的 `docs/research/`。

---

## 12. 本机读取远程数据

本机通过 Tailscale 私有网络从远程**单向拉取**报告：

```bash
mkdir -p /Users/dkw/Documents/crypto-quant-remote/{daily_reports,governance,research}

rsync -avz \
  quant@crypto-quant-grok:/opt/crypto-quant/app/data/daily_reports/ \
  /Users/dkw/Documents/crypto-quant-remote/daily_reports/

rsync -avz \
  quant@crypto-quant-grok:/opt/crypto-quant/app/data/governance/ \
  /Users/dkw/Documents/crypto-quant-remote/governance/

rsync -avz \
  quant@crypto-quant-grok:/opt/crypto-quant/app/data/research/ \
  /Users/dkw/Documents/crypto-quant-remote/research/
```

原则：

- 平时只做远程到本机的单向拉取。
- 不要把本机旧 `data/` rsync 回正在运行的远程节点。
- 做完整备份时复制到时间戳目录，避免使用会删除远程数据的方向或参数。

---

## 13. 远程本地备份

远程 `data/` 是唯一运行账本，Git 不备份它。

建议每天本机备份：

```bash
mkdir -p /opt/crypto-quant/backups
rsync -a /opt/crypto-quant/app/data/ \
  /opt/crypto-quant/backups/data-$(date +%F)/
```

建议保留至少：

- 14 天逐日备份。
- 8 周逐周备份。
- 6 个月逐月备份。

备份后可检查：

```bash
du -sh /opt/crypto-quant/app/data /opt/crypto-quant/backups/*
```

---

## 14. 两个月后迁移清单

迁移前不要先关旧节点。按以下顺序执行：

1. 记录旧服务器当前 Git commit：

```bash
cd /opt/crypto-quant/app
git rev-parse HEAD
python3 --version
.venv/bin/pip freeze > /opt/crypto-quant/reports/pip-freeze.txt
```

2. 备份并安全迁移：
   - `/opt/crypto-quant/app/data/`
   - `/opt/crypto-quant/app/.env`，通过安全秘密通道，不通过 Git/聊天。
   - `/etc/systemd/system/crypto-quant-dashboard.service`
   - `/etc/systemd/system/crypto-quant-hourly-observe.service`
   - `/etc/systemd/system/crypto-quant-hourly-observe.timer`
   - `/etc/systemd/system/crypto-quant-report.service`
   - `/etc/systemd/system/crypto-quant-report.timer`
   - Tailscale Serve 配置与 tailnet ACL 说明。
   - `/opt/crypto-quant/logs/` 中必要日志。

3. 在新服务器：
   - 拉取相同 Git commit。
   - 建立 Python 环境并安装相同依赖。
   - 恢复 `data/` 和 `.env`。
   - 加入 Tailscale。
   - 运行完整测试、治理、日报和 Dashboard 健康检查。

4. 核对迁移前后：
   - 账户 JSON 的状态、现金、仓位数和权益。
   - JSONL 事件数和最近事件 ID。
   - 每日排名快照数。
   - 最近日报和治理状态。

5. 新节点连续成功生成至少一份日报并能通过 Tailscale 访问 Dashboard 后，再停用旧节点：

```bash
sudo systemctl disable --now crypto-quant-dashboard.service
sudo systemctl disable --now crypto-quant-hourly-observe.timer
sudo systemctl disable --now crypto-quant-report.timer
sudo tailscale serve --https=443 off
```

---

## 15. 故障检查命令

```bash
cd /opt/crypto-quant/app

# 代码与测试
.venv/bin/python -m unittest discover -s tests -v

# 策略治理与日报
.venv/bin/python governance.py review
.venv/bin/python daily_report.py

# GMGN 状态
.venv/bin/python gmgn_copy_paper.py status

# 服务状态
sudo systemctl status crypto-quant-dashboard.service
sudo systemctl status crypto-quant-hourly-observe.timer
sudo systemctl status crypto-quant-report.timer
sudo tailscale serve status
sudo tailscale status

# Dashboard 本机健康
curl -I http://127.0.0.1:8888

# 日志
journalctl -u crypto-quant-dashboard.service -n 100 --no-pager
journalctl -u crypto-quant-hourly-observe.service -n 100 --no-pager
tail -n 100 /opt/crypto-quant/logs/daily-report.log
```

安全失败预期：

- GMGN live 配置不完整时，状态必须是 `blocked_config`，不得偷偷发 HTTP 或创建纸面成交。
- 上游数据异常时，策略必须暂停新决策或进入 halt，不能伪造退出价格。
- 报告必须持续提示 Carry fixture 污染和 Polymarket 旧样本无效。
- Dashboard 必须保持只读，没有账户重置、密钥、钱包或交易控件。
