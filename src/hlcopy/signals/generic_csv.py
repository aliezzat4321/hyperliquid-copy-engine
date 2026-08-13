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
    """Remove exact export duplicates; real lifecycle reuse is gated downstream."""
    return (
        signal.coin.upper(),
        signal.direction,
        signal.opened_at_ms,
        signal.closed_at_ms,
        signal.entry_price,
        signal.exit_price,
    )


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


def _load_generic_closed_trades_text(text: str) -> GenericTradeImportResult:
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    if not headers:
        raise GenericTradeCsvError("CSV has no header row")
    mapping = detect_column_map(headers)
    signals: list[CopySignal] = []
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
            signals.append(signal)
        except GenericTradeCsvError as exc:
            rejected.append({"row": row_number, "error": str(exc)})

    signals.sort(key=lambda item: (item.opened_at_ms, item.signal_id))
    return GenericTradeImportResult(
        signals=tuple(signals),
        rejected_rows=tuple(rejected),
        column_map=mapping,
        duplicate_rows=tuple(duplicates),
    )


def load_generic_closed_trades_bytes(data: bytes) -> GenericTradeImportResult:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise GenericTradeCsvError("CSV is not valid UTF-8") from exc
    return _load_generic_closed_trades_text(text)


def load_generic_closed_trades(path: Path) -> GenericTradeImportResult:
    return load_generic_closed_trades_bytes(path.read_bytes())
