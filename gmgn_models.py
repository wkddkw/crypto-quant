"""Validation and normalization for GMGN Solana paper-copy inputs.

These models intentionally describe only read-only ranking and trade data. They
do not contain wallet credentials or order-submission fields.
"""
from dataclasses import dataclass
from typing import Optional


class ValidationError(ValueError):
    pass


def require(value, field):
    if value is None or value == "":
        raise ValidationError(f"missing_{field}")
    return value


@dataclass(frozen=True)
class RankedWallet:
    wallet_address: str
    rank: int
    score: float

    @classmethod
    def from_dict(cls, row):
        address = str(require(row.get("wallet_address"), "wallet_address"))
        rank = int(require(row.get("rank"), "rank"))
        score = float(require(row.get("score"), "score"))
        if rank < 1:
            raise ValidationError("invalid_rank")
        return cls(address, rank, score)


@dataclass(frozen=True)
class TradeEvent:
    event_id: str
    chain: str
    wallet_address: str
    executed_at: int
    asset_mint: str
    asset_symbol: str
    side: str
    trade_usd: float
    price_usd: Optional[float]
    liquidity_usd: Optional[float]
    raw: dict

    @classmethod
    def from_dict(cls, row):
        signature = row.get("tx_signature")
        index = row.get("instruction_index")
        event_id = row.get("event_id") or (
            f"{signature}:{index}" if signature is not None and index is not None else None
        )
        return cls(
            event_id=str(require(event_id, "event_id")),
            chain=str(require(row.get("chain"), "chain")).lower(),
            wallet_address=str(require(row.get("wallet_address"), "wallet_address")),
            executed_at=int(require(row.get("executed_at"), "executed_at")),
            asset_mint=str(require(row.get("asset_mint"), "asset_mint")),
            asset_symbol=str(row.get("asset_symbol") or "UNKNOWN"),
            side=str(require(row.get("side"), "side")).lower(),
            trade_usd=float(require(row.get("trade_usd"), "trade_usd")),
            price_usd=None if row.get("price_usd") is None else float(row["price_usd"]),
            liquidity_usd=None if row.get("liquidity_usd") is None else float(row["liquidity_usd"]),
            raw=dict(row),
        )

    def as_dict(self):
        return {
            "event_id": self.event_id,
            "chain": self.chain,
            "wallet_address": self.wallet_address,
            "executed_at": self.executed_at,
            "asset_mint": self.asset_mint,
            "asset_symbol": self.asset_symbol,
            "side": self.side,
            "trade_usd": self.trade_usd,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "raw": self.raw,
        }
