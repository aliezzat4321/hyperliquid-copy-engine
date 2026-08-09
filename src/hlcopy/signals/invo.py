from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

D = Decimal
ZERO = D("0")
HUNDRED = D("100")


class InvoSignalError(ValueError):
    """Raised when an exported Invo trade cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class CopySignal:
    signal_id: str
    source: str
    trader: str
    coin: str
    direction: str
    source_leverage: Decimal
    allocation_fraction: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at_ms: int
    closed_at_ms: int
    entry_sim: Decimal | None
    last_sim: Decimal | None
    reason_closed: str
    liquidated: bool
    raw: dict[str, str]

    @property
    def signed_direction(self) -> Decimal:
        return D("1") if self.direction == "LONG" else D("-1")

    @property
    def underlying_return(self) -> Decimal:
        return self.signed_direction * (self.exit_price / self.entry_price - D("1"))

    @property
    def source_leveraged_return(self) -> Decimal:
        return self.underlying_return * self.source_leverage


def _decimal(value: str | None, *, field: str, allow_blank: bool = False) -> Decimal | None:
    if value is None or value.strip() == "":
        if allow_blank:
            return None
        raise InvoSignalError(f"{field} is required")
    try:
        return D(value)
    except InvalidOperation as exc:
        raise InvoSignalError(f"{field} is not numeric: {value!r}") from exc


def _timestamp_ms(value: str | None, *, field: str) -> int:
    if not value:
        raise InvoSignalError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvoSignalError(f"{field} is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def normalize_invo_row(row: dict[str, str]) -> CopySignal:
    signal_id = (row.get("trade_id") or "").strip()
    if not signal_id:
        raise InvoSignalError("trade_id is required")

    direction = (row.get("direction") or "").strip().upper()
    if direction not in {"LONG", "SHORT"}:
        raise InvoSignalError(f"unsupported direction {direction!r}")

    entry_price = _decimal(row.get("entry_price"), field="entry_price")
    exit_price = _decimal(row.get("closing_price"), field="closing_price")
    leverage = _decimal(row.get("leverage"), field="leverage")
    entry_size_pct = _decimal(row.get("entry_size"), field="entry_size")
    assert entry_price is not None and exit_price is not None
    assert leverage is not None and entry_size_pct is not None
    if entry_price <= ZERO or exit_price <= ZERO:
        raise InvoSignalError("entry_price and closing_price must be positive")
    if leverage <= ZERO:
        raise InvoSignalError("leverage must be positive")
    if entry_size_pct < ZERO:
        raise InvoSignalError("entry_size cannot be negative")

    opened_at_ms = _timestamp_ms(row.get("opened_at"), field="opened_at")
    closed_at_ms = _timestamp_ms(row.get("closed_at"), field="closed_at")
    if closed_at_ms < opened_at_ms:
        raise InvoSignalError("closed_at cannot precede opened_at")

    entry_sim = _decimal(row.get("entry_sim"), field="entry_sim", allow_blank=True)
    last_sim = _decimal(row.get("last_sim"), field="last_sim", allow_blank=True)

    trader = (
        (row.get("username") or "").strip()
        or (row.get("trader_name") or "").strip()
        or (row.get("trader_id") or "").strip()
        or "unknown"
    )
    coin = (row.get("ticker") or "").strip().upper()
    if not coin:
        raise InvoSignalError("ticker is required")

    return CopySignal(
        signal_id=signal_id,
        source="invo_export",
        trader=trader,
        coin=coin,
        direction=direction,
        source_leverage=leverage,
        allocation_fraction=entry_size_pct / HUNDRED,
        entry_price=entry_price,
        exit_price=exit_price,
        opened_at_ms=opened_at_ms,
        closed_at_ms=closed_at_ms,
        entry_sim=entry_sim,
        last_sim=last_sim,
        reason_closed=(row.get("reason_closed") or "").strip(),
        liquidated=_bool(row.get("is_liquidated")),
        raw=dict(row),
    )


@dataclass(frozen=True, slots=True)
class InvoImportResult:
    signals: tuple[CopySignal, ...]
    rejected_rows: tuple[dict[str, Any], ...]


def load_invo_closed_trades(
    path: Path,
    *,
    coins: set[str] | None = None,
    directions: set[str] | None = None,
    since_ms: int | None = None,
) -> InvoImportResult:
    selected_coins = {coin.upper() for coin in coins} if coins else None
    selected_directions = {direction.upper() for direction in directions} if directions else None
    signals: list[CopySignal] = []
    rejected: list[dict[str, Any]] = []

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=2):
            try:
                signal = normalize_invo_row(row)
            except InvoSignalError as exc:
                rejected.append({"row": row_number, "error": str(exc)})
                continue
            if selected_coins is not None and signal.coin not in selected_coins:
                continue
            if selected_directions is not None and signal.direction not in selected_directions:
                continue
            if since_ms is not None and signal.opened_at_ms < since_ms:
                continue
            signals.append(signal)

    signals.sort(key=lambda item: (item.opened_at_ms, item.signal_id))
    return InvoImportResult(signals=tuple(signals), rejected_rows=tuple(rejected))
