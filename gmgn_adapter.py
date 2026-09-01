"""Config-driven read-only GMGN adapter with a deterministic fixture mode."""
import json
import os
import time
from pathlib import Path

import requests

from gmgn_models import RankedWallet, TradeEvent, ValidationError

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "gmgn_config.json").read_text())


class AdapterBlocked(RuntimeError):
    pass


def load_fixture(name):
    path = ROOT / CONFIG["fixture"][name]
    return json.loads(path.read_text())


def required_live_fields():
    gmgn = CONFIG["gmgn"]
    fields = [
        ("gmgn.base_url", gmgn.get("base_url")),
        ("gmgn.auth.credential_env", gmgn["auth"].get("credential_env")),
        ("gmgn.auth.header_name", gmgn["auth"].get("header_name")),
        ("gmgn.ranking_contract.endpoint", gmgn["ranking_contract"].get("endpoint")),
        ("gmgn.trade_feed_contract.endpoint", gmgn["trade_feed_contract"].get("endpoint")),
    ]
    missing = [name for name, value in fields if not value]
    credential_env = gmgn["auth"].get("credential_env")
    if credential_env and not os.getenv(credential_env):
        missing.append(f"env:{credential_env}")
    return missing


def _live_get(contract, params):
    missing = required_live_fields()
    if missing:
        raise AdapterBlocked("blocked_config:" + ",".join(missing))
    gmgn = CONFIG["gmgn"]
    auth = gmgn["auth"]
    token = os.environ[auth["credential_env"]]
    headers = {
        "User-Agent": "crypto-quant-gmgn-paper/0.1",
        auth["header_name"]: f"{auth.get('header_prefix') or ''}{token}",
    }
    url = gmgn["base_url"].rstrip("/") + contract["endpoint"]
    proxies = {"http": CONFIG["proxy"], "https": CONFIG["proxy"]} if CONFIG.get("proxy") else None
    error = None
    for attempt in range(CONFIG["http"]["max_attempts"]):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                proxies=proxies,
                timeout=CONFIG["http"]["timeout_sec"],
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            error = exc
            if attempt + 1 < CONFIG["http"]["max_attempts"]:
                time.sleep(CONFIG["http"]["backoff_sec"] * (attempt + 1))
    raise RuntimeError(f"gmgn_request_failed:{error}")


def rank_wallets():
    if CONFIG["mode"] == "fixture":
        rows = load_fixture("ranking_path")["wallets"]
    elif CONFIG["mode"] == "live":
        contract = CONFIG["gmgn"]["ranking_contract"]
        params = {
            contract["chain_parameter"]: "solana",
            contract["period_parameter"]: CONFIG["universe"]["ranking_period"],
        }
        payload = _live_get(contract, params)
        rows = payload.get("data", payload)
    else:
        raise AdapterBlocked("blocked_config:mode")
    wallets = [RankedWallet.from_dict(row) for row in rows]
    wallets = [wallet for wallet in wallets if wallet.rank <= CONFIG["universe"]["daily_rank_limit"]]
    if len({wallet.wallet_address for wallet in wallets}) != len(wallets):
        raise ValidationError("duplicate_ranked_wallet")
    return sorted(wallets, key=lambda wallet: wallet.rank)


def list_trade_events(wallet, since_ms=0):
    if CONFIG["mode"] == "fixture":
        rows = load_fixture("trade_events_path")["events"]
        rows = [
            row for row in rows
            if row.get("wallet_address") == wallet and int(row.get("executed_at", 0)) > since_ms
        ]
    elif CONFIG["mode"] == "live":
        contract = CONFIG["gmgn"]["trade_feed_contract"]
        params = {
            contract["wallet_parameter"]: wallet,
            contract["since_parameter"]: since_ms,
        }
        payload = _live_get(contract, params)
        rows = payload.get("data", payload)
    else:
        raise AdapterBlocked("blocked_config:mode")
    return [TradeEvent.from_dict(row) for row in rows]
