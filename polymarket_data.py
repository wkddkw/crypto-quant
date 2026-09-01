"""Public Polymarket market and order-book observer. Never places orders."""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from runtime_provenance import provenance

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "polymarket"
CONFIG = json.loads((ROOT / "polymarket_config.json").read_text())
PROXIES = {"http": CONFIG["proxy"], "https": CONFIG["proxy"]}


def now_ms():
    return int(time.time() * 1000)


def get(base, path, params=None):
    response = requests.get(base.rstrip("/") + path, params=params, proxies=PROXIES,
                            timeout=15, headers={"User-Agent": "crypto-quant-polymarket-paper/0.1"})
    response.raise_for_status()
    return response.json()


def normalize_market(raw):
    outcomes = raw.get("outcomes", [])
    tokens = raw.get("clobTokenIds", raw.get("tokens", []))
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    return {
        "market_id": str(raw.get("id", raw.get("conditionId", ""))),
        "condition_id": raw.get("conditionId", ""),
        "question": raw.get("question", ""),
        "slug": raw.get("slug", ""),
        "end_date": raw.get("endDate", raw.get("end_date", "")),
        "active": bool(raw.get("active", False)),
        "closed": bool(raw.get("closed", False)),
        "outcomes": outcomes,
        "token_ids": tokens,
        "resolution_source": raw.get("resolutionSource", ""),
        "observed_at": now_ms(),
    }


def is_target_market(market):
    """Require explicit BTC/ETH tokens and a near-term binary Up/Down contract."""
    text = f"{market['question']} {market['slug']}".lower()
    asset_match = re.search(r"(^|[^a-z0-9])(btc|bitcoin|eth|ethereum)([^a-z0-9]|$)", text)
    direction_match = re.search(r"\b(up|down)\b", text)
    if not asset_match or not direction_match or len(market["token_ids"]) != 2:
        return False
    try:
        end = datetime.fromisoformat(market["end_date"].replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    seconds_to_end = (end - datetime.now(timezone.utc)).total_seconds()
    return 0 < seconds_to_end <= CONFIG["max_market_duration_sec"]


def discover_markets():
    payload = get(CONFIG["gamma_url"], "/markets", {"active": "true", "closed": "false",
                                                      "limit": CONFIG["market_limit"], "offset": 0})
    raw_markets = payload.get("data", payload) if isinstance(payload, dict) else payload
    markets = [normalize_market(row) for row in raw_markets]
    return [market for market in markets if is_target_market(market)][:CONFIG["max_markets"]]


def orderbook(token_id):
    return get(CONFIG["clob_url"], "/book", {"token_id": token_id})


def levels(book, side):
    result = []
    for row in book.get(side, []):
        price = float(row.get("price", row.get("p", 0)))
        size = float(row.get("size", row.get("s", 0)))
        if price > 0 and size > 0:
            result.append((price, size))
    return result


def quote_market(market):
    token_quotes = []
    for token_id in market["token_ids"][:2]:
        book = orderbook(token_id)
        asks = levels(book, "asks")
        bids = levels(book, "bids")
        token_quotes.append({"token_id": token_id,
                             "best_ask": min((p for p, _ in asks), default=None),
                             "ask_depth": sum(s for _, s in asks),
                             "best_bid": max((p for p, _ in bids), default=None),
                             "bid_depth": sum(s for _, s in bids)})
    if len(token_quotes) < 2 or any(q["best_ask"] is None for q in token_quotes):
        return None
    gross = 1 - sum(q["best_ask"] for q in token_quotes)
    net = gross - CONFIG["fee_pct"] - CONFIG["slippage_pct"] - CONFIG["gas_usd"] / max(1, min(q["ask_depth"] for q in token_quotes))
    return {"market_id": market["market_id"], "condition_id": market["condition_id"],
            "question": market["question"], "slug": market["slug"],
            "observed_at": now_ms(), "token_quotes": token_quotes,
            "complete_set_cost": sum(q["best_ask"] for q in token_quotes),
            "gross_edge": gross, "net_edge": net,
            "mode": "complete_set_only", "paper_action": "observe_only"}


def append_jsonl(path, value):
    with path.open("a") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def run_once():
    prov = provenance("polymarket_complete_set", ROOT / "polymarket_config.json")
    DATA.mkdir(parents=True, exist_ok=True)
    observed = 0
    opportunities = 0
    errors = []
    markets_path = DATA / "markets.jsonl"
    quotes_path = DATA / "quotes.jsonl"
    decisions_path = DATA / "decisions.jsonl"
    for market in discover_markets():
        observed += 1
        append_jsonl(markets_path, market)
        try:
            quote = quote_market(market)
            if quote is None:
                continue
            append_jsonl(quotes_path, quote)
            action = "candidate" if quote["net_edge"] >= CONFIG["min_net_edge"] else "reject_net_edge"
            if action == "candidate":
                opportunities += 1
            append_jsonl(decisions_path, {"observed_at": quote["observed_at"], "market_id": quote["market_id"],
                                          "action": action, "gross_edge": quote["gross_edge"], "net_edge": quote["net_edge"],
                                          **prov})
        except Exception as exc:
            errors.append(f"{market['market_id']}: {exc}")
    status = {"updated_at": now_ms(), "markets_observed": observed,
              "opportunities": opportunities, "errors": errors, "paper_only": True, **prov}
    (DATA / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    return status


if __name__ == "__main__":
    print(json.dumps(run_once(), ensure_ascii=False, indent=2))
