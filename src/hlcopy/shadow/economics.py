from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from hlcopy.hyperliquid.http_client import HyperliquidHttpClient

D = Decimal
ZERO = D("0")
ONE = D("1")
TWO = D("2")


@dataclass(frozen=True, slots=True)
class FundingRateEvent:
    coin: str
    timestamp_ms: int
    funding_rate: Decimal


async def fetch_funding_events(
    client: HyperliquidHttpClient,
    *,
    coin: str,
    start_ms: int,
    end_ms: int,
) -> tuple[FundingRateEvent, ...]:
    pages = await client.funding_history_by_time(coin, start_ms, end_ms)
    events: dict[tuple[int, str], FundingRateEvent] = {}
    for page in pages:
        rows = page.response_payload
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                timestamp = int(row["time"])
                rate = D(str(row["fundingRate"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
            if start_ms < timestamp <= end_ms:
                events[(timestamp, str(rate))] = FundingRateEvent(
                    coin=coin,
                    timestamp_ms=timestamp,
                    funding_rate=rate,
                )
    return tuple(sorted(events.values(), key=lambda event: event.timestamp_ms))


@dataclass(frozen=True, slots=True)
class AssetContextPoint:
    coin: str
    observed_ts_ms: int
    oracle_px: Decimal
    mark_px: Decimal


class ParquetAssetContextProvider:
    """Prospective oracle/mark path using exchange timestamp when available, else local receipt."""

    def __init__(self, market_dir: Path) -> None:
        self.market_dir = market_dir
        self._cache: dict[str, list[AssetContextPoint]] = {}

    def _load_coin(self, coin: str) -> list[AssetContextPoint]:
        if coin in self._cache:
            return self._cache[coin]
        files = sorted(
            self.market_dir.glob(f"date=*/coin={coin}/channel=activeAssetCtx/*.parquet")
        )
        if not files:
            self._cache[coin] = []
            return []
        frame = pl.concat(
            [pl.read_parquet(path) for path in files],
            how="diagonal_relaxed",
        ).sort("received_at_ns")
        points: list[AssetContextPoint] = []
        for row in frame.iter_rows(named=True):
            received_at_ns = row.get("received_at_ns")
            if (
                received_at_ns is None
                or row.get("oracle_px") is None
                or row.get("mark_px") is None
            ):
                continue
            exchange_ts = row.get("exchange_ts_ms")
            observed_ts_ms = (
                int(exchange_ts)
                if exchange_ts is not None
                else int(received_at_ns) // 1_000_000
            )
            points.append(
                AssetContextPoint(
                    coin=coin,
                    observed_ts_ms=observed_ts_ms,
                    oracle_px=D(str(row["oracle_px"])),
                    mark_px=D(str(row["mark_px"])),
                )
            )
        points.sort(key=lambda point: point.observed_ts_ms)
        self._cache[coin] = points
        return points

    def first_at_or_after(self, coin: str, timestamp_ms: int) -> AssetContextPoint | None:
        points = self._load_coin(coin)
        lo = 0
        hi = len(points)
        while lo < hi:
            mid = (lo + hi) // 2
            if points[mid].observed_ts_ms < timestamp_ms:
                lo = mid + 1
            else:
                hi = mid
        return points[lo] if lo < len(points) else None

    def between(self, coin: str, start_ms: int, end_ms: int) -> tuple[AssetContextPoint, ...]:
        return tuple(
            point
            for point in self._load_coin(coin)
            if start_ms <= point.observed_ts_ms <= end_ms
        )


@dataclass(frozen=True, slots=True)
class MarginTier:
    lower_bound: Decimal
    max_leverage: Decimal
    maintenance_rate: Decimal
    maintenance_deduction: Decimal


@dataclass(frozen=True, slots=True)
class AssetMarginSpec:
    coin: str
    size_decimals: int
    max_leverage: Decimal
    tiers: tuple[MarginTier, ...]
    metadata_fetched_at_ms: int
    metadata_path: str

    def maintenance_margin(self, notional: Decimal) -> Decimal:
        if notional < ZERO:
            raise ValueError("notional cannot be negative")
        if not self.tiers:
            raise ValueError(f"no maintenance tiers for {self.coin}")
        tier = self.tiers[0]
        for candidate in self.tiers:
            if notional >= candidate.lower_bound:
                tier = candidate
            else:
                break
        return notional * tier.maintenance_rate - tier.maintenance_deduction


class PointInTimeMarginMetadata:
    """Select the latest metadata snapshot known at or before an episode entry."""

    def __init__(self, metadata_dir: Path) -> None:
        self.metadata_dir = metadata_dir
        self._snapshots = self._load_snapshots()

    def _load_snapshots(self) -> tuple[tuple[int, Path, dict[str, Any]], ...]:
        snapshots: list[tuple[int, Path, dict[str, Any]]] = []
        for path in sorted(self.metadata_dir.glob("meta_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                fetched_at = int(payload["fetched_at_ms"])
                meta = payload["response_payload"]
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if isinstance(meta, dict):
                snapshots.append((fetched_at, path, meta))
        return tuple(sorted(snapshots, key=lambda item: item[0]))

    def asset_spec_at(self, coin: str, timestamp_ms: int) -> AssetMarginSpec | None:
        selected: tuple[int, Path, dict[str, Any]] | None = None
        for snapshot in self._snapshots:
            if snapshot[0] <= timestamp_ms:
                selected = snapshot
            else:
                break
        if selected is None:
            return None
        fetched_at, path, meta = selected
        return parse_asset_margin_spec(meta, coin, fetched_at, str(path))


def parse_asset_margin_spec(
    meta: dict[str, Any],
    coin: str,
    fetched_at_ms: int,
    metadata_path: str,
) -> AssetMarginSpec | None:
    universe = meta.get("universe")
    margin_tables = meta.get("marginTables")
    if not isinstance(universe, list):
        return None
    asset = next(
        (row for row in universe if isinstance(row, dict) and str(row.get("name")) == coin),
        None,
    )
    if asset is None:
        return None
    max_leverage = D(str(asset.get("maxLeverage")))
    table_id = asset.get("marginTableId")
    table_payload: dict[str, Any] | None = None
    if table_id is not None and isinstance(margin_tables, list):
        for raw in margin_tables:
            if not isinstance(raw, list) or len(raw) != 2:
                continue
            if int(raw[0]) == int(table_id) and isinstance(raw[1], dict):
                table_payload = raw[1]
                break
    if table_payload is None:
        raw_tiers: list[dict[str, Any]] = [
            {"lowerBound": "0", "maxLeverage": str(max_leverage)}
        ]
    else:
        raw = table_payload.get("marginTiers")
        if not isinstance(raw, list) or not raw:
            return None
        raw_tiers = [row for row in raw if isinstance(row, dict)]
    raw_tiers.sort(key=lambda row: D(str(row.get("lowerBound", "0"))))

    tiers: list[MarginTier] = []
    previous_rate = ZERO
    deduction = ZERO
    for index, raw_tier in enumerate(raw_tiers):
        lower_bound = D(str(raw_tier["lowerBound"]))
        tier_max_leverage = D(str(raw_tier["maxLeverage"]))
        if tier_max_leverage <= ZERO:
            raise ValueError("margin tier maxLeverage must be positive")
        maintenance_rate = ONE / (TWO * tier_max_leverage)
        if index > 0:
            deduction += lower_bound * (maintenance_rate - previous_rate)
        tiers.append(
            MarginTier(
                lower_bound=lower_bound,
                max_leverage=tier_max_leverage,
                maintenance_rate=maintenance_rate,
                maintenance_deduction=deduction,
            )
        )
        previous_rate = maintenance_rate
    return AssetMarginSpec(
        coin=coin,
        size_decimals=int(asset.get("szDecimals", 0)),
        max_leverage=max_leverage,
        tiers=tuple(tiers),
        metadata_fetched_at_ms=fetched_at_ms,
        metadata_path=metadata_path,
    )


def follower_funding_cashflow(
    *,
    direction: str,
    quantity: Decimal,
    oracle_px: Decimal,
    funding_rate: Decimal,
) -> Decimal:
    """Positive means received; Hyperliquid positive funding charges longs and pays shorts."""
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    side = ONE if direction == "LONG" else D("-1")
    return -side * quantity * oracle_px * funding_rate


@dataclass(frozen=True, slots=True)
class IsolatedPathResult:
    liquidated: bool
    liquidation_timestamp_ms: int | None
    liquidation_mark_px: Decimal | None
    cumulative_funding_usd: Decimal
    min_equity_usd: Decimal
    max_maintenance_margin_usd: Decimal


def simulate_isolated_path(
    *,
    direction: str,
    quantity: Decimal,
    entry_vwap: Decimal,
    leverage: Decimal,
    margin_spec: AssetMarginSpec,
    context_points: tuple[AssetContextPoint, ...],
    funding_events: tuple[FundingRateEvent, ...],
    max_context_forward_ms: int = 5_000,
) -> IsolatedPathResult:
    if leverage <= ZERO:
        raise ValueError("leverage must be positive")
    if leverage > margin_spec.max_leverage:
        raise ValueError(
            f"requested leverage {leverage} exceeds {margin_spec.coin} max {margin_spec.max_leverage}"
        )
    if not context_points:
        raise ValueError("isolated liquidation path requires mark/oracle context")
    side = ONE if direction == "LONG" else D("-1")
    initial_notional = quantity * entry_vwap
    isolated_margin = initial_notional / leverage
    cumulative_funding = ZERO
    min_equity = isolated_margin
    max_maintenance = margin_spec.maintenance_margin(initial_notional)
    funding_index = 0
    ordered_funding = sorted(funding_events, key=lambda event: event.timestamp_ms)

    for point in sorted(context_points, key=lambda item: item.observed_ts_ms):
        while (
            funding_index < len(ordered_funding)
            and ordered_funding[funding_index].timestamp_ms <= point.observed_ts_ms
        ):
            event = ordered_funding[funding_index]
            if point.observed_ts_ms - event.timestamp_ms > max_context_forward_ms:
                raise ValueError("funding event lacks sufficiently close oracle context")
            cumulative_funding += follower_funding_cashflow(
                direction=direction,
                quantity=quantity,
                oracle_px=point.oracle_px,
                funding_rate=event.funding_rate,
            )
            funding_index += 1

        unrealized = side * quantity * (point.mark_px - entry_vwap)
        equity = isolated_margin + unrealized + cumulative_funding
        notional = quantity * point.mark_px
        maintenance = margin_spec.maintenance_margin(notional)
        min_equity = min(min_equity, equity)
        max_maintenance = max(max_maintenance, maintenance)
        if equity <= maintenance:
            return IsolatedPathResult(
                liquidated=True,
                liquidation_timestamp_ms=point.observed_ts_ms,
                liquidation_mark_px=point.mark_px,
                cumulative_funding_usd=cumulative_funding,
                min_equity_usd=min_equity,
                max_maintenance_margin_usd=max_maintenance,
            )

    if funding_index != len(ordered_funding):
        raise ValueError("funding event extends beyond available mark/oracle context")
    return IsolatedPathResult(
        liquidated=False,
        liquidation_timestamp_ms=None,
        liquidation_mark_px=None,
        cumulative_funding_usd=cumulative_funding,
        min_equity_usd=min_equity,
        max_maintenance_margin_usd=max_maintenance,
    )
