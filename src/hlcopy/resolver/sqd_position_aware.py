from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.resolver.sqd_fills import (
    EpisodeEvidence,
    SqdHyperliquidFillsClient as BaseSqdHyperliquidFillsClient,
    aggregate_close_fills,
    signal_position_size,
)

D = Decimal
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class SqdFill:
    block_number: int
    user: str
    coin: str
    px: Decimal
    sz: Decimal
    side: str
    direction: str
    time_ms: int
    oid: str
    closed_pnl: Decimal
    tid: str
    start_position: Decimal | None

    @classmethod
    def from_raw(cls, block_number: int, row: dict[str, Any]) -> SqdFill | None:
        try:
            user = str(row.get("user") or "").lower()
            if not user.startswith("0x") or len(user) != 42:
                return None
            px = D(str(row.get("px")))
            sz = D(str(row.get("sz")))
            time_ms = int(row.get("time"))
            raw_start = row.get("startPosition")
            start_position = D(str(raw_start)) if raw_start is not None else None
            if px <= 0 or sz <= 0 or time_ms <= 0:
                return None
            return cls(
                block_number=block_number,
                user=user,
                coin=canonical_coin(str(row.get("coin") or "")),
                px=px,
                sz=sz,
                side=str(row.get("side") or ""),
                direction=str(row.get("dir") or ""),
                time_ms=time_ms,
                oid=str(row.get("oid") or ""),
                closed_pnl=D(str(row.get("closedPnl", row.get("closed_pnl", 0)) or 0)),
                tid=str(row.get("tid") or ""),
                start_position=start_position,
            )
        except (ArithmeticError, TypeError, ValueError):
            return None


class SqdHyperliquidFillsClient(BaseSqdHyperliquidFillsClient):
    async def _stream_range(
        self,
        *,
        from_block: int,
        to_block: int,
        coin: str,
        user: str | None = None,
    ) -> list[SqdFill]:
        current = from_block
        output: list[SqdFill] = []
        for _ in range(100):
            fill_filter: dict[str, object] = {"coin": [wire_coin(coin)]}
            if user is not None:
                fill_filter["user"] = [user.lower()]
            payload = {
                "type": "hyperliquidFills",
                "fromBlock": current,
                "toBlock": to_block,
                "fields": {
                    "block": {"number": True, "timestamp": True},
                    "fill": {
                        "user": True,
                        "coin": True,
                        "px": True,
                        "sz": True,
                        "side": True,
                        "dir": True,
                        "time": True,
                        "oid": True,
                        "closedPnl": True,
                        "crossed": True,
                        "tid": True,
                        "startPosition": True,
                    },
                },
                "fills": [fill_filter],
            }
            rows = await self._stream(payload)
            if not rows:
                break
            last_block = current - 1
            for row in rows:
                header = row.get("header")
                if not isinstance(header, dict):
                    continue
                block_number = int(header["number"])
                last_block = max(last_block, block_number)
                for raw in row.get("fills", []):
                    if not isinstance(raw, dict):
                        continue
                    fill = SqdFill.from_raw(block_number, raw)
                    if fill is not None:
                        output.append(fill)
            if last_block >= to_block:
                break
            if last_block < current:
                raise RuntimeError("SQD stream did not advance")
            current = last_block + 1
        else:
            raise RuntimeError("SQD stream exceeded continuation limit")
        return output


def _price_bps(left: Decimal, right: Decimal) -> Decimal:
    if left <= 0 or right <= 0:
        return D("Infinity")
    return abs(right / left - D("1")) * BPS


def _failed(
    signal_id: str,
    *,
    close_time_error_ms: int | None = None,
    close_price_bps: Decimal | None = None,
    close_size_ratio_error: Decimal | None = None,
    entry_time_error_ms: int | None = None,
    entry_price_bps: Decimal | None = None,
    entry_size_ratio_error: Decimal | None = None,
    reconstructed_entry: Decimal | None = None,
    reconstructed_size: Decimal | None = None,
) -> EpisodeEvidence:
    return EpisodeEvidence(
        signal_id=signal_id,
        matched=False,
        close_time_error_ms=close_time_error_ms,
        close_price_bps=close_price_bps,
        close_size_ratio_error=close_size_ratio_error,
        entry_time_error_ms=entry_time_error_ms,
        entry_price_bps=entry_price_bps,
        entry_size_ratio_error=entry_size_ratio_error,
        reconstructed_entry=reconstructed_entry,
        reconstructed_size=reconstructed_size,
    )


def match_episode(
    signal: Any,
    fills: list[SqdFill],
    *,
    close_time_tolerance_ms: int,
    close_price_tolerance_bps: Decimal,
    max_size_ratio_error: Decimal,
    entry_time_tolerance_ms: int,
    entry_price_tolerance_bps: Decimal,
) -> EpisodeEvidence:
    source_size = signal_position_size(signal)
    closes = aggregate_close_fills(fills, direction=signal.direction)
    close_options: list[tuple[Decimal, Any, Decimal | None]] = []
    for close in closes:
        time_error = min(
            abs(close.first_time_ms - signal.closed_at_ms),
            abs(close.last_time_ms - signal.closed_at_ms),
        )
        price_bps = _price_bps(signal.exit_price, close.avg_price)
        if time_error > close_time_tolerance_ms or price_bps > close_price_tolerance_bps:
            continue
        size_error = None
        if source_size is not None:
            size_error = abs(close.size / source_size - D("1"))
            if size_error > max_size_ratio_error:
                continue
        cost = D(time_error) / D("1000") + price_bps
        if size_error is not None:
            cost += size_error * D("10")
        close_options.append((cost, close, size_error))
    if not close_options:
        return _failed(signal.signal_id)

    _, close, close_size_error = min(close_options, key=lambda item: item[0])
    close_time_error = min(
        abs(close.first_time_ms - signal.closed_at_ms),
        abs(close.last_time_ms - signal.closed_at_ms),
    )
    close_price_bps = _price_bps(signal.exit_price, close.avg_price)
    target_size = source_size if source_size is not None else close.size

    if signal.direction == "LONG":
        open_name, add_name, reduce_name = "open long", "long > long", "close long"
        opposite_open_name, flip_name = "open short", "long > short"
    else:
        open_name, add_name, reduce_name = "open short", "short > short", "close short"
        opposite_open_name, flip_name = "open long", "short > long"

    # The identity gate is anchored to an observed flat -> open transition.
    # startPosition is the actual position immediately before this fill. A later
    # same-direction add has a non-zero startPosition and can never become the
    # episode boundary merely because it is closer to the exported opened_at.
    boundaries = [
        fill
        for fill in fills
        if fill.time_ms < close.first_time_ms
        and fill.direction.lower() == open_name
        and fill.start_position == D("0")
        and abs(fill.time_ms - signal.opened_at_ms) <= entry_time_tolerance_ms
    ]
    if not boundaries:
        return _failed(
            signal.signal_id,
            close_time_error_ms=close_time_error,
            close_price_bps=close_price_bps,
            close_size_ratio_error=close_size_error,
        )
    boundary = min(
        boundaries,
        key=lambda fill: (abs(fill.time_ms - signal.opened_at_ms), fill.time_ms, fill.tid),
    )
    entry_time_error = abs(boundary.time_ms - signal.opened_at_ms)

    total = D("0")
    notional = D("0")
    for fill in sorted(
        (
            row
            for row in fills
            if boundary.time_ms <= row.time_ms < close.first_time_ms
        ),
        key=lambda row: (row.time_ms, row.tid),
    ):
        text = fill.direction.lower()
        if text in {open_name, add_name}:
            total += fill.sz
            notional += fill.sz * fill.px
            continue
        if text in {opposite_open_name, flip_name}:
            return _failed(
                signal.signal_id,
                close_time_error_ms=close_time_error,
                close_price_bps=close_price_bps,
                close_size_ratio_error=close_size_error,
                entry_time_error_ms=entry_time_error,
            )
        if text == reduce_name and total > 0:
            average_entry = notional / total
            total = max(D("0"), total - fill.sz)
            notional = average_entry * total
            if total == 0:
                return _failed(
                    signal.signal_id,
                    close_time_error_ms=close_time_error,
                    close_price_bps=close_price_bps,
                    close_size_ratio_error=close_size_error,
                    entry_time_error_ms=entry_time_error,
                )

    if total <= 0:
        return _failed(
            signal.signal_id,
            close_time_error_ms=close_time_error,
            close_price_bps=close_price_bps,
            close_size_ratio_error=close_size_error,
            entry_time_error_ms=entry_time_error,
        )
    reconstructed_entry = notional / total
    entry_bps = _price_bps(signal.entry_price, reconstructed_entry)
    entry_size_error = abs(total / target_size - D("1"))
    matched = (
        entry_time_error <= entry_time_tolerance_ms
        and entry_bps <= entry_price_tolerance_bps
        and entry_size_error <= max_size_ratio_error
    )
    return EpisodeEvidence(
        signal_id=signal.signal_id,
        matched=matched,
        close_time_error_ms=close_time_error,
        close_price_bps=close_price_bps,
        close_size_ratio_error=close_size_error,
        entry_time_error_ms=entry_time_error,
        entry_price_bps=entry_bps,
        entry_size_ratio_error=entry_size_error,
        reconstructed_entry=reconstructed_entry,
        reconstructed_size=total,
    )
