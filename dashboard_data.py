"""Read-only adapters for the local crypto-quant dashboard."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def utc(ts):
    if ts is None or pd.isna(ts):
        return None
    return datetime.fromtimestamp(float(ts) / 1000, timezone.utc)


def load_json(path):
    try:
        return json.loads(Path(path).read_text()), None
    except FileNotFoundError:
        return {}, "文件不存在"
    except (json.JSONDecodeError, OSError) as exc:
        return {}, f"读取失败: {exc}"


def load_csv(path):
    try:
        return pd.read_csv(path), None
    except FileNotFoundError:
        return pd.DataFrame(), "文件不存在"
    except (pd.errors.ParserError, OSError) as exc:
        return pd.DataFrame(), f"读取失败: {exc}"


def load_parquet(path):
    try:
        return pd.read_parquet(path), None
    except FileNotFoundError:
        return pd.DataFrame(), "文件不存在"
    except (OSError, ValueError, ImportError) as exc:
        return pd.DataFrame(), f"读取失败: {exc}"


def dated_frame(frame, column="ts"):
    result = frame.copy()
    if column in result:
        if pd.api.types.is_numeric_dtype(result[column]):
            result["time"] = pd.to_datetime(result[column], unit="ms", utc=True)
        else:
            result["time"] = pd.to_datetime(result[column], utc=True, errors="coerce")
    return result


def latest_row(frame):
    if frame.empty:
        return {}
    return frame.iloc[-1].to_dict()


def equity(account, asset_key, price):
    return float(account.get("cash", 0)) + float(account.get(asset_key, 0)) * float(price or 0)


def paper_state():
    account, error = load_json(DATA / "paper" / "account.json")
    predictions, prediction_error = load_csv(DATA / "paper" / "predictions.csv")
    decisions = pd.DataFrame(account.get("decisions", []))
    history = pd.DataFrame(account.get("equity_history", []))
    if not history.empty and "ts" in history:
        history["time"] = pd.to_datetime(history["ts"], format="%Y-%m-%d %H:%M", utc=True, errors="coerce")
    scored = predictions[predictions.get("scored", pd.Series(dtype=str)).astype(str) == "1"]
    directional = scored[scored.get("pred", pd.Series(dtype=str)) != "flat"]
    accuracy = None if directional.empty else float((directional["hit"].astype(str) == "1").mean())
    return {"account": account, "error": error, "predictions": predictions,
            "prediction_error": prediction_error, "decisions": decisions, "history": history,
            "last": latest_row(decisions), "accuracy": accuracy, "scored": len(directional)}


def carry_state():
    account, error = load_json(DATA / "carry" / "account.json")
    snapshots, snapshot_error = load_csv(DATA / "carry" / "snapshots.csv")
    events, event_error = load_jsonl(DATA / "carry" / "events.jsonl")
    funding, funding_error = load_csv(DATA / "carry" / "funding_ledger.csv")
    snapshots = dated_frame(snapshots, "observed_at")
    history = pd.DataFrame(account.get("equity_history", []))
    history = dated_frame(history)
    return {"account": account, "error": error, "snapshots": snapshots,
            "snapshot_error": snapshot_error, "last": latest_row(snapshots),
            "events": events, "event_error": event_error, "funding": funding,
            "funding_error": funding_error, "history": history}


def load_jsonl(path):
    try:
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        return pd.DataFrame(rows), None
    except FileNotFoundError:
        return pd.DataFrame(), "文件不存在"
    except (json.JSONDecodeError, OSError) as exc:
        return pd.DataFrame(), f"读取失败: {exc}"


def polymarket_state():
    root = DATA / "polymarket"
    account, account_error = load_json(root / "account.json")
    status, status_error = load_json(root / "status.json")
    markets, markets_error = load_jsonl(root / "markets.jsonl")
    quotes, quotes_error = load_jsonl(root / "quotes.jsonl")
    decisions, decisions_error = load_jsonl(root / "decisions.jsonl")
    events, events_error = load_jsonl(root / "events.jsonl")
    settlements, settlements_error = load_csv(root / "settlements.csv")
    if not markets.empty:
        markets = dated_frame(markets, "observed_at")
    if not quotes.empty:
        quotes = dated_frame(quotes, "observed_at")
    if not decisions.empty:
        decisions = dated_frame(decisions, "observed_at")
    if not events.empty:
        events = dated_frame(events, "observed_at")
    if not settlements.empty:
        settlements = dated_frame(settlements, "settled_at")
    return {"account": account, "account_error": account_error,
            "status": status, "status_error": status_error,
            "markets": markets, "markets_error": markets_error,
            "quotes": quotes, "quotes_error": quotes_error,
            "decisions": decisions, "decisions_error": decisions_error,
            "events": events, "events_error": events_error,
            "settlements": settlements, "settlements_error": settlements_error,
            "report_error": None}


def gmgn_state():
    root = DATA / "gmgn_solana_paper"
    account, account_error = load_json(root / "account.json")
    status, status_error = load_json(root / "status.json")
    decisions, decisions_error = load_jsonl(root / "decisions.jsonl")
    events, events_error = load_jsonl(root / "events.jsonl")
    if not decisions.empty:
        decisions = dated_frame(decisions, "observed_at")
    if not events.empty:
        events = dated_frame(events, "observed_at")
    history = dated_frame(pd.DataFrame(account.get("equity_history", [])))
    return {"account": account, "account_error": account_error, "status": status,
            "status_error": status_error, "decisions": decisions,
            "decisions_error": decisions_error, "events": events,
            "events_error": events_error, "history": history}


def governance_state():
    status, error = load_json(DATA / "governance" / "status.json")
    return {"status": status, "error": error}


def replay_state():
    frame, error = load_csv(DATA / "carry_replay" / "settlement_replay.csv")
    return {"rows": dated_frame(frame), "error": error}


def backtest_state():
    summary, error = load_csv(DATA / "backtest" / "summary.csv")
    curves = {}
    for path in sorted((DATA / "backtest").glob("curve_*.csv")):
        frame, curve_error = load_csv(path)
        if not curve_error:
            curves[path.stem.removeprefix("curve_")] = dated_frame(frame)
    return {"summary": summary, "error": error, "curves": curves}


def data_health():
    rows = []
    for path in sorted(DATA.glob("*.parquet")):
        frame, error = load_parquet(path)
        row = {"file": path.name, "status": error or "ok", "rows": len(frame),
               "source": "Deribit research proxy" if path.name.startswith("deribit_") else "OKX / research input"}
        if not frame.empty and "ts" in frame:
            ts = pd.to_numeric(frame["ts"], errors="coerce").dropna().sort_values()
            step = 28_800_000 if "funding" in path.name else 86_400_000
            row.update({"from": utc(ts.iloc[0]), "to": utc(ts.iloc[-1]),
                        "gaps": int((ts.diff().dropna() != step).sum()),
                        "modified": datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)})
        rows.append(row)
    return pd.DataFrame(rows)
