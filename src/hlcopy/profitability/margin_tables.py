from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

D = Decimal
ZERO = D("0")
TWO = D("2")


@dataclass(frozen=True, slots=True)
class MarginTier:
    lower_bound_usd: Decimal
    max_leverage: Decimal
    maintenance_margin_rate: Decimal
    maintenance_deduction_usd: Decimal

    def __post_init__(self) -> None:
        if self.lower_bound_usd < ZERO:
            raise ValueError("lower_bound_usd cannot be negative")
        if self.max_leverage <= ZERO:
            raise ValueError("max_leverage must be positive")
        if self.maintenance_margin_rate <= ZERO:
            raise ValueError("maintenance_margin_rate must be positive")
        if self.maintenance_deduction_usd < ZERO:
            raise ValueError("maintenance_margin_deduction_usd cannot be negative")


@dataclass(frozen=True, slots=True)
class CoinMarginTable:
    coin: str
    margin_table_id: int
    tiers: tuple[MarginTier, ...]

    def tier_for_notional(self, notional_usd: Decimal) -> MarginTier:
        if notional_usd < ZERO:
            raise ValueError("notional_usd cannot be negative")
        chosen = self.tiers[0]
        for tier in self.tiers:
            if tier.lower_bound_usd <= notional_usd:
                chosen = tier
            else:
                break
        return chosen


@dataclass(frozen=True, slots=True)
class MarginMetadataSnapshot:
    fetched_at_ns: int
    tables: tuple[CoinMarginTable, ...]
    dex: str = ""

    def __post_init__(self) -> None:
        if self.fetched_at_ns <= 0:
            raise ValueError("fetched_at_ns must be positive")

    def by_coin(self) -> dict[str, CoinMarginTable]:
        return {table.coin: table for table in self.tables}


def _d(value: object) -> Decimal:
    try:
        return D(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def _tier_rows(raw: object) -> tuple[MarginTier, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("margin table requires non-empty marginTiers")
    base: list[tuple[Decimal, Decimal]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("margin tier must be an object")
        lower = _d(item.get("lowerBound", "0"))
        leverage = _d(item.get("maxLeverage"))
        if lower < ZERO or leverage <= ZERO:
            raise ValueError("invalid margin tier values")
        base.append((lower, leverage))
    base.sort(key=lambda row: row[0])
    if base[0][0] != ZERO:
        raise ValueError("first margin tier must start at zero")

    tiers: list[MarginTier] = []
    previous_rate: Decimal | None = None
    deduction = ZERO
    for lower, leverage in base:
        maintenance_rate = (D("1") / leverage) / TWO
        if previous_rate is not None:
            deduction += lower * (maintenance_rate - previous_rate)
        tiers.append(
            MarginTier(
                lower_bound_usd=lower,
                max_leverage=leverage,
                maintenance_margin_rate=maintenance_rate,
                maintenance_deduction_usd=max(ZERO, deduction),
            )
        )
        previous_rate = maintenance_rate
    return tuple(tiers)


def _extract_meta_objects(payload: object) -> list[dict[str, Any]]:
    """Find perp metadata objects in meta/perpDexStatus/allPerpMetas responses."""
    found: list[dict[str, Any]] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("universe"), list) and isinstance(value.get("marginTables"), list):
                found.append(value)
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return found


def parse_margin_metadata(
    payload: object,
    *,
    fetched_at_ns: int,
    dex: str = "",
) -> MarginMetadataSnapshot:
    """Parse an official Hyperliquid perp metadata response into exact margin tiers.

    The parser is intentionally strict. Assets without an unambiguous table id or
    leverage are omitted rather than guessed. For table ids below 50 Hyperliquid defines
    a single tier whose max leverage equals the id; for explicit marginTables we derive
    maintenance deductions using the exchange's continuous tier formula.
    """
    metas = _extract_meta_objects(payload)
    if not metas:
        raise ValueError("no perp metadata object with universe and marginTables found")

    tables_out: list[CoinMarginTable] = []
    seen: set[str] = set()
    for meta in metas:
        explicit: dict[int, tuple[MarginTier, ...]] = {}
        for raw_table in meta.get("marginTables") or []:
            if not isinstance(raw_table, list) or len(raw_table) != 2:
                continue
            try:
                table_id = int(raw_table[0])
            except (TypeError, ValueError):
                continue
            body = raw_table[1]
            if not isinstance(body, dict):
                continue
            try:
                explicit[table_id] = _tier_rows(body.get("marginTiers"))
            except ValueError:
                continue

        for asset in meta.get("universe") or []:
            if not isinstance(asset, dict):
                continue
            coin = str(asset.get("name") or "").strip()
            if not coin or coin in seen:
                continue
            raw_id = asset.get("marginTableId")
            raw_max = asset.get("maxLeverage")
            table_id: int | None = None
            if raw_id is not None:
                try:
                    table_id = int(raw_id)
                except (TypeError, ValueError):
                    table_id = None
            if table_id is None and raw_max is not None:
                try:
                    table_id = int(_d(raw_max))
                except ValueError:
                    table_id = None
            if table_id is None:
                continue

            tiers = explicit.get(table_id)
            if tiers is None and 0 < table_id < 50:
                leverage = D(table_id)
                tiers = (
                    MarginTier(
                        lower_bound_usd=ZERO,
                        max_leverage=leverage,
                        maintenance_margin_rate=(D("1") / leverage) / TWO,
                        maintenance_deduction_usd=ZERO,
                    ),
                )
            if tiers is None:
                continue
            seen.add(coin)
            tables_out.append(CoinMarginTable(coin, table_id, tiers))

    if not tables_out:
        raise ValueError("no usable coin margin tables found")
    tables_out.sort(key=lambda table: table.coin)
    return MarginMetadataSnapshot(fetched_at_ns=fetched_at_ns, tables=tuple(tables_out), dex=dex)


def snapshot_table_at(
    snapshots: Iterable[MarginMetadataSnapshot],
    coin: str,
    at_ns: int,
) -> CoinMarginTable | None:
    chosen: CoinMarginTable | None = None
    chosen_ts = -1
    for snapshot in snapshots:
        if snapshot.fetched_at_ns > at_ns or snapshot.fetched_at_ns < chosen_ts:
            continue
        table = snapshot.by_coin().get(coin)
        if table is not None:
            chosen = table
            chosen_ts = snapshot.fetched_at_ns
    return chosen
