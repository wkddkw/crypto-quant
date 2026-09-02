# 调度自检 2026-09-02（Grok Bot 远程节点）

本文件是脱敏研究记录，不含 token、Webhook、chat ID、私钥或纸面账本明细。

HEAD 自检时：`1abf718`。未补跑 `scheduled_report.sh 0600`（错过的槽不伪装时点）。

## 持久性核对

宿主无 `crontab` 命令，`cron` 包未安装。`/etc/cron.daily` 仅有 apt/dpkg/chrome，无 crypto-quant 条目。systemd unit 文件在 `/etc/systemd/system/crypto-quant-*`，但 PID1 为 `tini` 且无 `systemctl`，timer 不会开火。

| 日历 | 脚本 | 判定 |
|---|---|---|
| 每小时 :03 Asia/Shanghai | `scripts/hourly_observe.sh` | 仅会话内定时器（Grok Bot routine `3 * * * *`） |
| 每天 06:00 Asia/Shanghai | `scripts/scheduled_report.sh 0600` | 仅会话内定时器（Grok Bot routine `0 6 * * *`，至自检时从未跑过） |
| 每天 18:00 Asia/Shanghai | `scripts/scheduled_report.sh 1800` | 仅会话内定时器（Grok Bot routine `0 18 * * *`） |

## 槽次核对

- `data/sync/`：目录不存在，无半日同步包。
- `data/daily_reports/` 最新：`2026-09-01.md` / `2026-09-01.json`（mtime 2026-09-01 22:55 Asia/Shanghai）。无 `T0600+0800` 或 `T1800+0800` 槽位文件。
- 当前最新 sync_id：**无**。今天 06:00 槽 `2026-09-02T0600+0800` 错过。

## 本地审计（不入库）

运行节点已向本地 `data/governance/delivery_audit.jsonl` 追加（`data/` gitignore，不提交）：

```json
{"delivered_at":"2026-09-02T06:50:29.005618+00:00","channel_alias":"grok-bot","sync_id":"2026-09-02T0600+0800","outcome":"missed","message_id":null,"error_class":"missed_slot"}
{"delivered_at":"2026-09-02T06:50:29.005797+00:00","channel_alias":"grok-bot","sync_id":"scheduler-self-check-2026-09-02","outcome":"noted","message_id":null,"error_class":"ephemeral_scheduler"}
```

第二条按手册：仅会话内定时器须标注 `ephemeral_scheduler`。

## 结论

容器重启后，宿主 crontab/systemd **不会**继续触发 :03 / 0600 / 1800；当前调度是 Grok Bot 会话级定时器。

本报告仅包含研究与纸面账本数据；未连接钱包、未签名、未提交真实交易。
