# 持久调度改造（方案2）2026-09-02

脱敏记录：无 token、Webhook、chat ID、私钥。节点 HEAD 操作时为 `1abf718`。

## 步骤证据

### 0. 权限

- `id`：`uid=1000(box) gid=1000(box)`
- `sudo -n true`：退出码 0（免密 sudo 可用）
- 结论：方案2可行，未停止。

### 1. 安装并启动 cron

- `sudo apt-get install -y cron`：成功（顺带装入 systemd 用户态包；PID1 仍为 `tini`/`pod-daemon`）。
- 安装阶段 `policy-rc.d denied execution of start`，随后 `sudo service cron start` 成功。
- `pgrep -ax cron`：先为 `/usr/sbin/cron`（pid 375414），后为 `/usr/sbin/cron -L 15`（pid 376209）。

### 2. root crontab（容器时钟 UTC）

`sudo crontab -l` 原文：

```
3  *  * * *  sudo -u quant env CRYPTO_QUANT_NO_PROXY=1 /opt/crypto-quant/app/scripts/hourly_observe.sh
0  10 * * *  sudo -u quant env CRYPTO_QUANT_NO_PROXY=1 /opt/crypto-quant/app/scripts/scheduled_report.sh 1800
0  22 * * *  sudo -u quant env CRYPTO_QUANT_NO_PROXY=1 /opt/crypto-quant/app/scripts/scheduled_report.sh 0600
```

对应 Asia/Shanghai：每小时 :03、18:00（UTC 10:00）、06:00（UTC 22:00）。`env` 在 `sudo -u quant` 之后。

### 3. 会话定时器

原三条「直接跑脚本」的 Grok 定时器已删除。改为：

- 每天 08:00 Asia/Shanghai：`pgrep -x cron || sudo service cron start`（仅拉起 crond）。
- 投递仍由 Grok 在 06:10 / 18:10 读取 `data/sync`（不执行 `scheduled_report.sh`）。

### 4. 本记录截稿时尚未发生的证据

- 下一个 :03（北京 15:03 / UTC 07:03）的 `grep CRON /var/log/syslog` 与 parquet mtime：截稿时为北京 14:59，尚未到点。另：本机无 `/var/log/syslog`（rsyslog 因 logrotate 502 未装上），计划用 `journalctl` 或 cron `-L 15` 日志替代，不能编造 syslog 行。
- 北京 18:00 的 `data/sync/2026-09-02T1800+0800.*` 与投递审计：待当晚槽位。

### crond 是否随容器重启自启

**否。** PID1 是 `tini` / `pod-daemon`，不是 systemd。`cron.service` 的 symlink 对当前 PID1 无效。预期重启后 crond 不会自动起来，要靠第3步会话守护补拉。守护是每天 08:00 一次，因此重启后最长可能空窗约 24 小时（剩余单点）。

## 成功 / 失败

| 项 | 结果 |
|---|---|
| 免密 sudo | 成功 |
| apt 安装 cron | 成功 |
| 启动 crond | 成功（非 systemd 自启） |
| 写入 root crontab | 成功 |
| `/var/log/syslog` CRON 行 | **失败/缺失**：无 rsyslog，无该文件 |
| crond 随容器重启自启 | **否**（符合预期） |
| :03 / 1800 首次开火 | 截稿时未到点 |

## 剩余单点

1. 容器重启后 crond 不自启；Grok 守护每天只检查一次（08:00），空窗最长约一天。
2. 无 `/var/log/syslog`，CRON 审计依赖 journal 或任务副作用（parquet mtime）。
3. crontab 与 `/var/spool/cron/crontabs/root` 在「更新 Grok Bot 电脑」重建实例后需重装（和 apt 包一样不随 box update 保留软件）。
4. 会话守护本身仍是 bot 平台定时器：平台若不唤醒，crond 补拉也不会发生。

本报告仅包含研究与纸面账本数据；未连接钱包、未签名、未提交真实交易。
