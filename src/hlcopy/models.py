from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

D = Decimal


def decimal(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return D(default)
    return D(str(value))


@dataclass(frozen=True, slots=True)
class Fill:
    wallet_address: str
    tid: int
    oid: int | None
    tx_hash: str | None
    timestamp_ms: int
    coin: str
    side: str
    direction: str
    price: Decimal
    size: Decimal
    start_position: Decimal
    closed_pnl: Decimal
    fee: Decimal
    fee_token: str | None
    crossed: bool | None
    builder_fee: Decimal
    raw: dict[str, Any]

    @property
    def signed_size(self) -> Decimal:
        if self.side == "B":
            return self.size
        if self.side == "A":
            return -self.size
        raise ValueError(f"unknown side: {self.side!r}")

    @property
    def notional(self) -> Decimal:
        return self.price * self.size

    @classmethod
    def from_raw(cls, wallet_address: str, raw: dict[str, Any]) -> "Fill":
        return cls(
            wallet_address=wallet_address.lower(),
            tid=int(raw["tid"]),
            oid=int(raw["oid"]) if raw.get("oid") is not None else None,
            tx_hash=raw.get("hash"),
            timestamp_ms=int(raw["time"]),
            coin=str(raw["coin"]),
            side=str(raw["side"]),
            direction=str(raw.get("dir", "")),
            price=decimal(raw.get("px")),
            size=decimal(raw.get("sz")),
            start_position=decimal(raw.get("startPosition")),
            closed_pnl=decimal(raw.get("closedPnl")),
            fee=decimal(raw.get("fee")),
            fee_token=raw.get("feeToken"),
            crossed=raw.get("crossed"),
            builder_fee=decimal(raw.get("builderFee")),
            raw=raw,
        )
