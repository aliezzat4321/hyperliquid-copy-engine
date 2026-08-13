from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from hlcopy.signals.invo import CopySignal

D = Decimal
ZERO = D("0")
HUNDRED = D("100")
BPS = D("10000")
DEFAULT_NEAR_DUPLICATE_ENTRY_TIME_MS = 300_000
DEFAULT_NEAR_DUPLICATE_CLOSE_TIME_MS = 30_000
DEFAULT_NEAR_DUPLICATE_ENTRY_PRICE_BPS = D("15")
DEFAULT_NEAR_DUPLICATE_EXIT_PRICE_BPS = D("35")


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "trade_id": ("trade_id", "id", "position_id"),
    "trader": ("username", "trader_name", "trader_id", "trader", "user"),
    "coin": ("ticker", "coin", "symbol", "asset"),
    "direction": ("direction", "position_side", "position_direction"),
    "leverage": ("leverage", "lev"),
    "entry_price": ("entry_price", "avg_entry_price", "open_price"),
    "exit_price": ("closing_price", "exit_price", "avg_exit_price", "close_price"),
    "opened_at": ("opened_at", "open_time", "opened_time", "start_time"),
    "closed_at": ("closed_at", "close_time", "closed_time", "end_time"),
    "entry_size": ("entry_size", "allocation_pct", "size_pct", "position_size_pct"),
    "entry_sim": ("entry_sim",),
    "last_sim": ("last_sim",),
    "reason_closed": ("reason_closed", "close_reason"),
    "is_liquidated": ("is_liquidated", "liquidated"),
}

REQUIRED_FIELDS = (
    "coin",
    "direction",
    "entry_price",
    "exit_price",
    "opened_at",
    "closed_at",
)


class GenericTradeCsvError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GenericTradeImportResult:
    signals: tuple[CopySignal, ...]
    rejected_rows: tuple[dict[str, Any], ...]
    column_map: dict[str, str]
    duplicate_rows: tuple[dict[str, Any], ...] = ()
    overlapping_rows: tuple[dict[str, Any], ...] = ()


def _lookup_header(headers: list[str], aliases: tuple[str, ...]) -> str | None:
    lowered = {header.strip().lower(): header for header in headers}
    for alias in aliases:
        found = lowered.get(alias.lower())
        if found is not None:
            return found
    return None


def detect_column_map(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for canonical, aliases in FIELD_ALIASES.items():
        found = _lookup_header(headers, aliases)
        if found is not None:
            mapping[canonical] = found
    missing = [field for field in REQUIRED_FIELDS if field not in mapping]
    if missing:
        hint = ""
        lowered = {header.strip().lower() for header in headers}
        if "side" in lowered and "direction" in missing:
            hint = (
                "; generic 'side' is intentionally not treated as position direction "
                "because it may describe the closing order side"
            )
        raise GenericTradeCsvError(
            "could not auto-detect required trade columns: " + ", ".join(missing) + hint
        )
    return mapping


def _value(row: dict[str, str], mapping: dict[str, str], field: str) -> str:
    column = mapping.get(field)
    return str(row.get(column, "") if column is not None else "").strip()


def _decimal(value: str, *, field: str, default: Decimal | None = None) -> Decimal:
    if not value:
        if default is not None:
            return default
        raise GenericTradeCsvError(f"{field} is required")
    try:
        return D(value)
    except InvalidOperation as exc:
        raise GenericTradeCsvError(f"{field} is not numeric: {value!r}") from exc


def _timestamp_ms(value: str, *, field: str) -> int:
    if not value:
        raise GenericTradeCsvError(f"{field} is required")
    if value.isdigit():
        numeric = int(value)
        return numeric if numeric > 10_000_000_000 else numeric * 1000
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GenericTradeCsvError(f"{field} is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _direction(value: str) -> str:
    normalized = value.strip().upper()
    if normalized == "LONG":
        return "LONG"
    if normalized == "SHORT":
        return "SHORT"
    raise GenericTradeCsvError(
        f"unsupported position direction {value!r}; expected explicit LONG or SHORT"
    )


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _episode_fingerprint(signal: CopySignal) -> tuple[object, ...]:
    return (
        signal.coin.upper(),
        signal.direction,
        signal.opened_at_ms,
        signal.closed_at_ms,
        signal.entry_price,
        signal.exit_price,
    )


def _representative_key(row: tuple[int, CopySignal]) -> tuple[int, int, int, str]:
    row_number, signal = row
    duration = signal.closed_at_ms - signal.opened_at_ms
    return (-duration, signal.opened_at_ms, row_number, signal.signal_id)


def _price_bps(left: Decimal, right: Decimal) -> Decimal:
    if left <= ZERO or right <= ZERO:
        return D("Infinity")
    return abs(right / left - D("1")) * BPS


def _intervals_overlap(left: CopySignal, right: CopySignal) -> bool:
    return max(left.opened_at_ms, right.opened_at_ms) < min(
        left.closed_at_ms,
        right.closed_at_ms,
    )


def _near_equivalent_source_episode(
    left: CopySignal,
    right: CopySignal,
    *,
    entry_time_tolerance_ms: int,
    close_time_tolerance_ms: int,
    entry_price_tolerance_bps: Decimal,
    exit_price_tolerance_bps: Decimal,
) -> bool:
    return (
        left.direction == right.direction
        and abs(left.opened_at_ms - right.opened_at_ms) <= entry_time_tolerance_ms
        and abs(left.closed_at_ms - right.closed_at_ms) <= close_time_tolerance_ms
        and _price_bps(left.entry_price, right.entry_price) <= entry_price_tolerance_bps
        and _price_bps(left.exit_price, right.exit_price) <= exit_price_tolerance_bps
    )


def _collapse_overlapping_source_evidence(
    rows: list[tuple[int, CopySignal]],
    *,
    entry_time_tolerance_ms: int,
    close_time_tolerance_ms: int,
    entry_price_tolerance_bps: Decimal,
    exit_price_tolerance_bps: Decimal,
) -> tuple[list[CopySignal], list[dict[str, Any]]]:
    """Collapse evidence that cannot safely be treated as independent positions."""
    by_coin: dict[str, list[tuple[int, CopySignal]]] = {}
    for row in rows:
        by_coin.setdefault(row[1].coin.upper(), []).append(row)

    representatives: list[CopySignal] = []
    collapsed: list[dict[str, Any]] = []
    for coin_rows in by_coin.values():
        count = len(coin_rows)
        neighbors: list[set[int]] = [set() for _ in range(count)]
        for left_index in range(count):
            left = coin_rows[left_index][1]
            for right_index in range(left_index + 1, count):
                right = coin_rows[right_index][1]
                related = _intervals_overlap(left, right) or _near_equivalent_source_episode(
                    left,
                    right,
                    entry_time_tolerance_ms=entry_time_tolerance_ms,
                    close_time_tolerance_ms=close_time_tolerance_ms,
                    entry_price_tolerance_bps=entry_price_tolerance_bps,
                    exit_price_tolerance_bps=exit_price_tolerance_bps,
                )
                if related:
                    neighbors[left_index].add(right_index)
                    neighbors[right_index].add(left_index)

        visited: set[int] = set()
        for start_index in range(count):
            if start_index in visited:
                continue
            stack = [start_index]
            component_indexes: list[int] = []
            while stack:
                index = stack.pop()
                if index in visited:
                    continue
                visited.add(index)
                component_indexes.append(index)
                stack.extend(neighbors[index] - visited)

            component = [coin_rows[index] for index in component_indexes]
            representative_row, representative = min(component, key=_representative_key)
            representatives.append(representative)
            for row_number, signal in component:
                if row_number == representative_row:
                    continue
                collapsed.append(
                    {
                        "row": row_number,
                        "signal_id": signal.signal_id,
                        "representative_row": representative_row,
                        "representative_signal_id": representative.signal_id,
                        "coin": signal.coin,
                        "reason": "overlapping_or_near_equivalent_source_position_evidence",
                    }
                )

    representatives.sort(key=lambda item: (item.opened_at_ms, item.signal_id))
    collapsed.sort(key=lambda item: int(item["row"]))
    return representatives, collapsed


def normalize_generic_row(
    row: dict[str, str],
    *,
    mapping: dict[str, str],
    row_number: int,
) -> CopySignal:
    coin = _value(row, mapping, "coin").upper()
    if not coin:
        raise GenericTradeCsvError("coin is required")
    direction = _direction(_value(row, mapping, "direction"))
    entry_price = _decimal(_value(row, mapping, "entry_price"), field="entry_price")
    exit_price = _decimal(_value(row, mapping, "exit_price"), field="exit_price")
    if entry_price <= ZERO or exit_price <= ZERO:
        raise GenericTradeCsvError("entry_price and exit_price must be positive")

    opened_at_ms = _timestamp_ms(_value(row, mapping, "opened_at"), field="opened_at")
    closed_at_ms = _timestamp_ms(_value(row, mapping, "closed_at"), field="closed_at")
    if closed_at_ms < opened_at_ms:
        raise GenericTradeCsvError("closed_at cannot precede opened_at")

    leverage = _decimal(_value(row, mapping, "leverage"), field="leverage", default=D("1"))
    if leverage <= ZERO:
        raise GenericTradeCsvError("leverage must be positive")

    entry_size_raw = _value(row, mapping, "entry_size")
    entry_size = _decimal(entry_size_raw, field="entry_size", default=HUNDRED)
    allocation_fraction = entry_size / HUNDRED if entry_size > D("1") else entry_size
    if allocation_fraction < ZERO:
        raise GenericTradeCsvError("entry_size cannot be negative")

    signal_id = _value(row, mapping, "trade_id") or f"row-{row_number}"
    trader = _value(row, mapping, "trader") or "unknown"

    entry_sim_raw = _value(row, mapping, "entry_sim")
    last_sim_raw = _value(row, mapping, "last_sim")
    entry_sim = _decimal(entry_sim_raw, field="entry_sim") if entry_sim_raw else None
    last_sim = _decimal(last_sim_raw, field="last_sim") if last_sim_raw else None

    return CopySignal(
        signal_id=signal_id,
        source="generic_closed_trades_csv",
        trader=trader,
        coin=coin,
        direction=direction,
        source_leverage=leverage,
        allocation_fraction=allocation_fraction,
        entry_price=entry_price,
        exit_price=exit_price,
        opened_at_ms=opened_at_ms,
        closed_at_ms=closed_at_ms,
        entry_sim=entry_sim,
        last_sim=last_sim,
        reason_closed=_value(row, mapping, "reason_closed"),
        liquidated=_bool(_value(row, mapping, "is_liquidated")),
        raw=dict(row),
    )


def _load_generic_closed_trades_text(
    text: str,
    *,
    near_duplicate_entry_time_ms: int,
    near_duplicate_close_time_ms: int,
    near_duplicate_entry_price_bps: Decimal,
    near_duplicate_exit_price_bps: Decimal,
) -> GenericTradeImportResult:
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    if not headers:
        raise GenericTradeCsvError("CSV has no header row")
    mapping = detect_column_map(headers)
    parsed: list[tuple[int, CopySignal]] = []
    rejected: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    first_row_by_episode: dict[tuple[object, ...], int] = {}
    for row_number, row in enumerate(reader, start=2):
        try:
            signal = normalize_generic_row(row, mapping=mapping, row_number=row_number)
            fingerprint = _episode_fingerprint(signal)
            first_row = first_row_by_episode.get(fingerprint)
            if first_row is not None:
                duplicates.append(
                    {
                        "row": row_number,
                        "duplicate_of_row": first_row,
                        "signal_id": signal.signal_id,
                        "reason": "exact_normalized_episode_duplicate",
                    }
                )
                continue
            first_row_by_episode[fingerprint] = row_number
            parsed.append((row_number, signal))
        except GenericTradeCsvError as exc:
            rejected.append({"row": row_number, "error": str(exc)})

    signals, overlapping = _collapse_overlapping_source_evidence(
        parsed,
        entry_time_tolerance_ms=max(0, near_duplicate_entry_time_ms),
        close_time_tolerance_ms=max(0, near_duplicate_close_time_ms),
        entry_price_tolerance_bps=max(ZERO, near_duplicate_entry_price_bps),
        exit_price_tolerance_bps=max(ZERO, near_duplicate_exit_price_bps),
    )
    return GenericTradeImportResult(
        signals=tuple(signals),
        rejected_rows=tuple(rejected),
        column_map=mapping,
        duplicate_rows=tuple(duplicates),
        overlapping_rows=tuple(overlapping),
    )


def load_generic_closed_trades_bytes(
    data: bytes,
    *,
    near_duplicate_entry_time_ms: int = DEFAULT_NEAR_DUPLICATE_ENTRY_TIME_MS,
    near_duplicate_close_time_ms: int = DEFAULT_NEAR_DUPLICATE_CLOSE_TIME_MS,
    near_duplicate_entry_price_bps: Decimal = DEFAULT_NEAR_DUPLICATE_ENTRY_PRICE_BPS,
    near_duplicate_exit_price_bps: Decimal = DEFAULT_NEAR_DUPLICATE_EXIT_PRICE_BPS,
) -> GenericTradeImportResult:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GenericTradeCsvError("CSV is not valid UTF-8") from exc
    return _load_generic_closed_trades_text(
        text,
        near_duplicate_entry_time_ms=near_duplicate_entry_time_ms,
        near_duplicate_close_time_ms=near_duplicate_close_time_ms,
        near_duplicate_entry_price_bps=near_duplicate_entry_price_bps,
        near_duplicate_exit_price_bps=near_duplicate_exit_price_bps,
    )


def load_generic_closed_trades(
    path: Path,
    *,
    near_duplicate_entry_time_ms: int = DEFAULT_NEAR_DUPLICATE_ENTRY_TIME_MS,
    near_duplicate_close_time_ms: int = DEFAULT_NEAR_DUPLICATE_CLOSE_TIME_MS,
    near_duplicate_entry_price_bps: Decimal = DEFAULT_NEAR_DUPLICATE_ENTRY_PRICE_BPS,
    near_duplicate_exit_price_bps: Decimal = DEFAULT_NEAR_DUPLICATE_EXIT_PRICE_BPS,
) -> GenericTradeImportResult:
    return load_generic_closed_trades_bytes(
        path.read_bytes(),
        near_duplicate_entry_time_ms=near_duplicate_entry_time_ms,
        near_duplicate_close_time_ms=near_duplicate_close_time_ms,
        near_duplicate_entry_price_bps=near_duplicate_entry_price_bps,
        near_duplicate_exit_price_bps=near_duplicate_exit_price_bps,
    )
