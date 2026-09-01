# Grok Bot Remote Node Runbook

This document deploys `crypto-quant` on an always-on remote server for research, paper accounting, a read-only dashboard, and scheduled reports. The remote node replaces the local ZCode schedule and local Streamlit process.

The repository is public:

```text
https://github.com/wkddkw/crypto-quant.git
```

## 1. Operating Boundary

The remote node is a research and paper-trading node only.

It may:

- Download the public repository.
- Read public market data and a GMGN API only when the account and terms explicitly permit automated read-only access.
- Run local paper accounting, research, tests, governance, dashboard, and reporting scripts.
- Write paper data and reports under its local `data/` directory.
- Deliver Markdown reports and dashboard URLs to the owner.

It must not:

- Store a seed phrase, private key, exchange trading secret, or wallet export.
- Connect a wallet, sign a transaction, place an order, transfer funds, or enable withdrawals.
- Scrape Fomo or bypass a provider's API terms, rate limits, login, or robots policy.
- Treat fixtures, backtests, invalid data, paper PnL, or an open position as real profit.
- Automatically tune strategy parameters, replace a wallet pool, promote a strategy, or allocate capital.
- Expose the dashboard publicly without authentication.

## 2. Server Layout

Use a dedicated non-root account, for example `quant`:

```text
/opt/crypto-quant/
  app/                    # Git clone
  reports/                # Published Grok research memos, optional
  logs/                   # Scheduler and service logs
```

Do not mount a browser profile, wallet extension, SSH private key, cloud credential directory, or local Mac data directory into this server.

## 3. Initial Setup

On an Ubuntu/Debian-like server:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip rsync
sudo useradd --create-home --shell /bin/bash quant
sudo mkdir -p /opt/crypto-quant /opt/crypto-quant/logs /opt/crypto-quant/reports
sudo chown -R quant:quant /opt/crypto-quant
sudo -iu quant
cd /opt/crypto-quant
git clone https://github.com/wkddkw/crypto-quant.git app
cd app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The test suite must pass before enabling scheduled jobs.

## 4. Configuration and Secrets

The repository intentionally does not contain real credentials or local data. The default `gmgn_config.json` is `fixture` mode and makes no GMGN HTTP requests.

Do not change `mode` to `live` until all of the following are known from GMGN official documentation and your plan explicitly allows it:

1. API base URL and version.
2. Read-only authentication method, header name, prefix, rate limit, and credential rotation policy.
3. Solana ranking endpoint, ranking period, stable pagination, and Top 100 completeness semantics.
4. Wallet event endpoint, cursor semantics, immutable event ID, event time, buy/sell semantics, mint, price, liquidity, and delay fields.
5. Permission to poll and store the returned data for paper research.

When ready, create a server-local environment file that is never committed:

```bash
install -m 600 /dev/null /opt/crypto-quant/app/.env
```

Example shape only; use values confirmed by official documentation:

```bash
GMGN_READONLY_API_KEY=replace_with_readonly_credential
```

Set the environment variable in the systemd service definition or a root-readable environment file. Do not paste it into `gmgn_config.json`, logs, chat messages, or Git.

In `gmgn_config.json`, fill the official API contract fields and set `mode` to `live` only after a fixture replay and one full observation day have been reviewed. Any missing live field must remain a fail-closed `blocked_config` condition.

## 5. Commands and Scheduling

All commands run from `/opt/crypto-quant/app`. They write only local paper data.

### Daily data refresh and strategy cycle

Use one serialized script so two ledger writers cannot overlap:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/crypto-quant/app

.venv/bin/python collector.py update
.venv/bin/python carry_trader.py run
.venv/bin/python paper_trader.py run
.venv/bin/python polymarket_data.py
.venv/bin/python polymarket_paper.py
.venv/bin/python gmgn_copy_paper.py run
.venv/bin/python governance.py review
.venv/bin/python daily_report.py
```

Save it as `/opt/crypto-quant/app/scripts/daily_cycle.sh`, make it executable, and run it once manually before scheduling.

Important:

- `collector.py update`, `carry_trader.py run`, `polymarket_paper.py`, and `gmgn_copy_paper.py run` write state. Never run multiple copies in parallel.
- `gmgn_copy_paper.py` must stay in fixture mode until official API configuration is complete.
- Polymarket is currently paused in governance. Its collector may run only to validate the repaired market filter; its old sample does not count toward performance.
- Carry remains observe-only. It may correctly stay empty for months when net edge is negative.
- The existing BTC paper runner is the retired `v0_full` baseline. Do not claim its paper PnL validates `trend_only`.

### systemd daily cycle

Create `/etc/systemd/system/crypto-quant-cycle.service`:

```ini
[Unit]
Description=Crypto quant paper research cycle
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=quant
WorkingDirectory=/opt/crypto-quant/app
EnvironmentFile=-/opt/crypto-quant/app/.env
ExecStart=/opt/crypto-quant/app/scripts/daily_cycle.sh
StandardOutput=append:/opt/crypto-quant/logs/daily-cycle.log
StandardError=append:/opt/crypto-quant/logs/daily-cycle.log
```

Create `/etc/systemd/system/crypto-quant-cycle.timer`:

```ini
[Unit]
Description=Run crypto quant paper cycle daily

[Timer]
OnCalendar=*-*-* 08:05:00 Asia/Shanghai
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-quant-cycle.timer
systemctl list-timers crypto-quant-cycle.timer
```

### Daily 18:00 Beijing report

The daily cycle already creates `data/daily_reports/YYYY-MM-DD.md` and `.json`. Create a separate report job at 18:00 Beijing time so it refreshes governance and sends the owner a summary through the Grok Bot's approved delivery channel.

The report job must run:

```bash
cd /opt/crypto-quant/app
.venv/bin/python governance.py review
.venv/bin/python daily_report.py
```

It must then read:

```text
data/daily_reports/YYYY-MM-DD.md
data/governance/report.md
```

and produce a concise Chinese report containing:

- Strategy status, paper equity, open positions, accepted/rejected signals, and main rejection reasons.
- Freshness, upstream errors, blocked configuration, and data-integrity warnings.
- Carry, Polymarket, GMGN, and BTC governance conclusion plus review date.
- Explicit note that all figures are research or paper figures and no real transaction was submitted.

Use the remote Grok Bot's native scheduler/delivery mechanism for this final step. Do not configure a notification URL or credential in the public repository.

## 6. Private Tailscale Access and Read-only Dashboard

The remote server does not need a public IP. Join it and the local Mac to the same Tailscale tailnet. Do not use Tailscale Funnel; Funnel makes a service publicly reachable and is outside this project's operating boundary.

### Install and join the remote node

On the remote Linux server:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=crypto-quant-grok --ssh
sudo tailscale status
```

The `tailscale up` command prints an authentication URL when interactive login is required. Authenticate it with the same Tailscale account/tailnet used by the local Mac. Use a reusable tagged auth key only when you control the tailnet policy and server lifecycle; never put that key in this repository or a Grok prompt.

On the local Mac, install and sign in to the Tailscale client using the same tailnet. Confirm both nodes can reach each other:

```bash
tailscale status
ping crypto-quant-grok
```

Use the MagicDNS name printed by `tailscale status`, or the remote node's `100.x.y.z` Tailscale address if MagicDNS is disabled.

### Start the dashboard privately

Keep Streamlit bound to the loopback interface on the remote node:

```bash
cd /opt/crypto-quant/app
.venv/bin/streamlit run dashboard.py \
  --server.address 127.0.0.1 \
  --server.port 8888 \
  --server.headless true \
  --browser.gatherUsageStats false
```

Expose only that local listener to the tailnet using Tailscale Serve:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8888
sudo tailscale serve status
```

Open the HTTPS URL shown by `tailscale serve status` from the local Mac. It is reachable only to tailnet members allowed by the tailnet ACL; it is not a public Internet endpoint. To remove the proxy later:

```bash
sudo tailscale serve --https=443 off
```

Set Tailscale ACLs so only the owner's local device or an owner group can reach `crypto-quant-grok:443`, and only approved admin devices can SSH to the remote node. Do not grant the Grok Bot ability to modify tailnet ACLs, add devices, or enable Funnel.

### Read remote reports and data from the local Mac

The dashboard reads data on the remote server. For a local copy of selected reports or a backup, use Tailscale SSH/rsync over the private hostname:

```bash
# Pull reports only; safe for routine local inspection.
rsync -avz quant@crypto-quant-grok:/opt/crypto-quant/app/data/daily_reports/ \
  /Users/dkw/Documents/crypto-quant-remote/daily_reports/

# Pull governance and research memos.
rsync -avz quant@crypto-quant-grok:/opt/crypto-quant/app/data/governance/ \
  /Users/dkw/Documents/crypto-quant-remote/governance/
rsync -avz quant@crypto-quant-grok:/opt/crypto-quant/app/data/research/ \
  /Users/dkw/Documents/crypto-quant-remote/research/
```

Use one-way remote-to-local pulls for inspection. Do not rsync local `data/` back to the active remote node because that could overwrite its paper ledger. When a full backup is required, first stop the remote cycle or copy to a timestamped local directory without `--delete`.

### Keep the dashboard running with systemd

Create `/etc/systemd/system/crypto-quant-dashboard.service`:

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

Enable and check it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-quant-dashboard.service
sudo systemctl status crypto-quant-dashboard.service
```

## 7. Git Update Procedure

The remote server is the operating copy. Before each daily cycle or during a controlled maintenance window:

```bash
cd /opt/crypto-quant/app
git fetch origin
git status --short
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Never use `git reset --hard` on a node that has unsynced paper data. The `data/` directory is ignored and must be backed up independently.

Recommended local data backup:

```bash
rsync -a --delete /opt/crypto-quant/app/data/ /opt/crypto-quant/backups/data-current/
```

Keep at least daily rotating backups. The public Git repository is not a backup of paper data because `data/` is intentionally ignored.

## 8. Grok Bot Role Instruction

Give the following instruction to the remote Grok Bot. Replace paths only when the server uses a different location.

```text
You operate the remote research node for crypto-quant.

Repository: /opt/crypto-quant/app
Research reports: /opt/crypto-quant/reports
Logs: /opt/crypto-quant/logs

Your role is read-only market research, paper-strategy monitoring, data-quality review, dashboard availability monitoring, and scheduled Chinese reporting.

You may run approved paper/research commands and write local reports under data/research/ or /opt/crypto-quant/reports/. You may update code only on a separate Git branch, run tests, and prepare a pull request or a patch for review.

You must not connect a wallet, store a private key, sign, submit a trade, transfer funds, use a withdrawal key, scrape Fomo, bypass a provider's terms, or modify the main branch without review.

Every conclusion must separate:
1. Verified facts.
2. Hypotheses.
3. Open questions.
4. Data source, timestamp, and sample size.
5. Whether the result is a fixture, backtest, paper result, or real result.

Current strategy states:
- btc_v0_full: retired historical baseline; its paper PnL is not validation for a promotion.
- btc_trend_only_shadow: needs fixed-rule shadow evidence.
- okx_funding_carry: observe-only; fixture records in historical events are not valid performance evidence.
- polymarket_complete_set: paused; old Hegseth matches are invalid and do not count.
- gmgn_solana_copy: Solana buy-only paper shadow; use fixture mode until a verified, authorized GMGN read-only API contract is configured.

Daily tasks:
1. Confirm dashboard service and daily-cycle timer health.
2. Run or inspect governance.py review and daily_report.py results.
3. Check data freshness, errors, stale timestamps, duplicate events, fixture contamination, and account/event inconsistencies.
4. Produce a Chinese memo with strategy status, paper equity, positions, signal acceptance/rejection, major risks, blocker items, and next review dates.
5. Propose at most three research hypotheses. Each requires a testable premise, data source, time range, sample target, cost model, and kill criterion.
6. Do not automatically alter strategy parameters, wallet pools, trading rules, or allocation.

At 18:00 Asia/Shanghai every day, deliver the paper-strategy report through the bot's configured channel. State clearly that it contains no real transaction result.
```

## 9. Operational Checks

Run these after deployment and after every code update:

```bash
cd /opt/crypto-quant/app
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python governance.py review
.venv/bin/python daily_report.py
.venv/bin/python gmgn_copy_paper.py status
curl -I http://127.0.0.1:8888
systemctl status crypto-quant-dashboard.service
systemctl status crypto-quant-cycle.timer
```

Expected safe behavior:

- Missing GMGN live configuration reports `blocked_config` rather than attempting HTTP or creating fills.
- Unavailable public inputs halt or pause new paper decisions instead of inventing exits.
- Daily reports preserve warnings about invalid Polymarket history and Carry fixture contamination.
- Dashboard remains read-only and does not expose a reset, credential, wallet, or execution control.
