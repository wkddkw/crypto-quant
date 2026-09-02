# Grok node runtime notes (2026-09-02)

Observed on the always-on research node while preparing branch
`grok/fix-2026-09-02-grok-node-runtime`. Operating copy remained on `main`
at `3a50d18`. Observation window: 2026-09-02 around 09:10–09:20 Asia/Shanghai
(UTC+8).

This note records host facts and a proposed fallback. It does not change
strategy parameters, `strategy_registry.json` status/promotion/kill fields,
or GMGN live mode.

## Verified facts

1. **No systemd as init.** PID 1 is `tini` 0.19.0 (`/tini -- /pod-daemon ...`).
   `systemctl` is not installed. This is a Grok Bot container (Debian 13),
   not a systemd Ubuntu host from the runbook examples.
2. **Unit files cannot fire.** `crypto-quant-*.service` / `*.timer` files exist
   under `/etc/systemd/system/`, including hourly observe (`*:03:00 Asia/Shanghai`)
   and report slots (`06:00` / `18:00 Asia/Shanghai`). Without systemd they are
   documentation only.
3. **No local SOCKS/HTTP proxy on 10808.** Committed `*_config.json` proxy
   strings remain `http://127.0.0.1:10808`. Nothing listens on that port.
   Public-data clients that always send traffic through that proxy stall or
   fail closed on connect. One `scripts/hourly_observe.sh` invocation was
   observed holding `data/.hourly-observe.lock` with `collector.py update`
   in flight on the operating copy; this branch does not attach to that
   process.
4. **US egress.** Direct public HTTPS from this host exits in the United
   States (Cloudflare). No user-device IPs are recorded here. Tailnet
   hostname already used in the runbook: `crypto-quant-grok`.
5. **GMGN remains fixture.** `gmgn_config.json` `mode` is `fixture`. Live
   contract fields (`base_url`, auth, ranking/trade endpoints) are null.
   Fixture ranking/events are local JSON. No GMGN HTTP is attempted.
6. **Streamlit is loopback-only.** Dashboard process listens on
   `127.0.0.1:8888` (`--server.address 127.0.0.1`). Tailscale Serve publishes
   that listener privately. Funnel is not enabled.
7. **No host crontab for these calendars.** No `quant` crontab entries.
   `flock` is available (`/usr/bin/flock`). Scripts
   `hourly_observe.sh` and `scheduled_report.sh` were `100644` in git (not
   executable in the tree) even though the operating copy had a local
   chmod-only dirty state that was not committed from `main`.

## What was installed

- Git clone `/opt/crypto-quant/app` as user `quant`, revision `3a50d18`
  (`Restrict Grok Bot to branch-and-PR access with owner merges`).
- Python 3.13.5 venv at `/opt/crypto-quant/app/.venv` with
  `requirements.txt` (`requests`, `pandas`, `pyarrow`, `numpy`, `streamlit`).
- OS packages used by the node: `git`, `python3`/`python3-venv`, `rsync`,
  `util-linux` (flock), Tailscale 1.102.3.
- Tailscale node name `crypto-quant-grok`; dashboard started with the
  runbook loopback flags; logs under `/opt/crypto-quant/logs/`.
- systemd unit files copied into `/etc/systemd/system/` (inert here).

## What cannot fire on this host

- systemd timers for hourly observe and 06:00/18:00 reports.
- Any path that requires `systemctl` / `journalctl`.
- Traffic via `127.0.0.1:10808` until a local proxy exists (it does not).
- GMGN live polling (fixture / missing live contract → fail closed by
  design; not enabled).
- Two ledger writers at once: `flock -n -E 75` on
  `data/.hourly-observe.lock` is unchanged and still required.

## Proposed fallback (this branch)

1. **HTTP transport** for public-data clients (`collector.py`,
   `carry_trader.py`, `paper_trader.py`, `polymarket_data.py`):
   - `CRYPTO_QUANT_NO_PROXY=1` → direct only.
   - else try the configured proxy; on connect/proxy failure retry once
     without proxy; fail closed if both fail.
   - committed proxy strings in `*_config.json` are not edited.
   - GMGN adapter / live mode is not rewired.
2. **Scheduling:** hosts without systemd (this container, PID 1 tini) must
   still run the **same scripts** on the **same calendars** via the host
   scheduler: hourly `:03`, `06:00`, and `18:00` Asia/Shanghai.
   `flock` lock unchanged. No Tailscale Funnel. No Streamlit on `0.0.0.0`.
3. **Git file mode:** mark `scripts/hourly_observe.sh` and
   `scripts/scheduled_report.sh` executable (`0755`) in git.

## Open questions

- Which host scheduler (container job runner vs cron vs bot-owned timer)
  will invoke the scripts while PID 1 remains tini.
- Whether a local 10808 proxy will ever exist on this node, or whether
  `CRYPTO_QUANT_NO_PROXY=1` should be the steady-state env for US egress.

本报告仅包含研究与纸面账本数据；未连接钱包、未签名、未提交真实交易。

This note contains research and paper-ledger observations only; no wallet
was connected, no transaction was signed, and no real trade was submitted.
