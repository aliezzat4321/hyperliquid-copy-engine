from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.resolver.sqd_fills import EpisodeEvidence, signal_position_size
from hlcopy.resolver.sqd_fills import (
    SqdHyperliquidFillsClient as BaseSqdHyperliquidFillsClient,
)

D = Decimal
BPS = D("10000")
POSITION_EPSILON = D("0.000000000001")


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


@dataclass(frozen=True, slots=True)
class LifecycleEpisodeEvidence(EpisodeEvidence):
    lifecycle_id: str | None = None
    boundary_execution_id: str | None = None
    final_execution_id: str | None = None
    rejection_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        row = EpisodeEvidence.to_dict(self)
        row["lifecycle_id"] = self.lifecycle_id
        row["boundary_execution_id"] = self.boundary_execution_id
        row["final_execution_id"] = self.final_execution_id
        row["rejection_reason"] = self.rejection_reason
        return row


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


def fill_execution_id(fill: SqdFill) -> str:
    if fill.tid:
        return f"tid:{fill.tid}"
    return f"oid:{fill.oid}:t:{fill.time_ms}:sz:{fill.sz}:px:{fill.px}"


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
    lifecycle_id: str | None = None,
    boundary_execution_id: str | None = None,
    final_execution_id: str | None = None,
    rejection_reason: str | None = None,
) -> LifecycleEpisodeEvidence:
    return LifecycleEpisodeEvidence(
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
        lifecycle_id=lifecycle_id,
        boundary_execution_id=boundary_execution_id,
        final_execution_id=final_execution_id,
        rejection_reason=rejection_reason,
    )


def reject_lifecycle(
    evidence: LifecycleEpisodeEvidence,
    *,
    reason: str,
) -> LifecycleEpisodeEvidence:
    return LifecycleEpisodeEvidence(
        signal_id=evidence.signal_id,
        matched=False,
        close_time_error_ms=evidence.close_time_error_ms,
        close_price_bps=evidence.close_price_bps,
        close_size_ratio_error=evidence.close_size_ratio_error,
        entry_time_error_ms=evidence.entry_time_error_ms,
        entry_price_bps=evidence.entry_price_bps,
        entry_size_ratio_error=evidence.entry_size_ratio_error,
        reconstructed_entry=evidence.reconstructed_entry,
        reconstructed_size=evidence.reconstructed_size,
        lifecycle_id=evidence.lifecycle_id,
        boundary_execution_id=evidence.boundary_execution_id,
        final_execution_id=evidence.final_execution_id,
        rejection_reason=reason,
    )


def reject_duplicate_lifecycle(
    evidence: LifecycleEpisodeEvidence,
) -> LifecycleEpisodeEvidence:
    return reject_lifecycle(evidence, reason="duplicate_lifecycle_reuse")


def _direction_names(direction: str) -> tuple[str, str, str, str, str]:
    if direction == "LONG":
        return "open long", "long > long", "close long", "open short", "long > short"
    return "open short", "short > short", "close short", "open long", "short > long"


def _start_position_matches(fill: SqdFill, current_size: Decimal) -> bool:
    if fill.start_position is None:
        return True
    tolerance = max(POSITION_EPSILON, abs(current_size) * D("0.000000001"))
    return abs(abs(fill.start_position) - current_size) <= tolerance


def _ordered_wallet_coin_fills(
    fills: list[SqdFill],
    *,
    user: str,
    coin: str,
    end_ms: int,
) -> list[tuple[int, SqdFill]]:
    indexed = [
        (index, row)
        for index, row in enumerate(fills)
        if row.user == user and row.coin == coin and row.time_ms <= end_ms
    ]
    indexed.sort(key=lambda item: (item[1].time_ms, item[1].block_number, item[0]))
    return indexed


def _match_from_boundary(
    signal: Any,
    fills: list[SqdFill],
    *,
    boundary: SqdFill,
    close_time_tolerance_ms: int,
    close_price_tolerance_bps: Decimal,
    max_size_ratio_error: Decimal,
    entry_time_tolerance_ms: int,
    entry_price_tolerance_bps: Decimal,
) -> LifecycleEpisodeEvidence:
    open_name, add_name, reduce_name, opposite_open_name, flip_name = _direction_names(
        signal.direction
    )
    entry_time_error = abs(boundary.time_ms - signal.opened_at_ms)
    source_size = signal_position_size(signal)
    boundary_execution_id = fill_execution_id(boundary)
    if source_size is None:
        return _failed(
            signal.signal_id,
            entry_time_error_ms=entry_time_error,
            boundary_execution_id=boundary_execution_id,
            rejection_reason="missing_absolute_source_size",
        )

    ordered = _ordered_wallet_coin_fills(
        fills,
        user=boundary.user,
        coin=boundary.coin,
        end_ms=signal.closed_at_ms + close_time_tolerance_ms,
    )
    boundary_position = next(
        (position for position, (_, row) in enumerate(ordered) if row is boundary),
        None,
    )
    if boundary_position is None:
        return _failed(
            signal.signal_id,
            entry_time_error_ms=entry_time_error,
            boundary_execution_id=boundary_execution_id,
            rejection_reason="boundary_not_in_ordered_stream",
        )
    rows = [row for _, row in ordered[boundary_position:]]

    current_size = D("0")
    current_entry_notional = D("0")
    gross_entry_size = D("0")
    gross_entry_notional = D("0")
    close_size = D("0")
    close_notional = D("0")
    close_last_ms: int | None = None

    for fill in rows:
        text = fill.direction.lower()
        if text not in {
            open_name,
            add_name,
            reduce_name,
            opposite_open_name,
            flip_name,
        }:
            continue
        if not _start_position_matches(fill, current_size):
            return _failed(
                signal.signal_id,
                entry_time_error_ms=entry_time_error,
                reconstructed_size=gross_entry_size or None,
                boundary_execution_id=boundary_execution_id,
                rejection_reason="position_continuity_mismatch",
            )

        if text in {open_name, add_name}:
            if current_size == 0 and fill is not boundary:
                return _failed(
                    signal.signal_id,
                    entry_time_error_ms=entry_time_error,
                    reconstructed_size=gross_entry_size or None,
                    boundary_execution_id=boundary_execution_id,
                    rejection_reason="unexpected_reopen",
                )
            current_size += fill.sz
            current_entry_notional += fill.sz * fill.px
            gross_entry_size += fill.sz
            gross_entry_notional += fill.sz * fill.px
            continue

        if text in {opposite_open_name, flip_name}:
            return _failed(
                signal.signal_id,
                entry_time_error_ms=entry_time_error,
                reconstructed_size=gross_entry_size or None,
                boundary_execution_id=boundary_execution_id,
                rejection_reason="position_flip_before_expected_close",
            )

        if current_size <= 0 or fill.sz > current_size + POSITION_EPSILON:
            return _failed(
                signal.signal_id,
                entry_time_error_ms=entry_time_error,
                reconstructed_size=gross_entry_size or None,
                boundary_execution_id=boundary_execution_id,
                rejection_reason="invalid_reduction_size",
            )

        average_entry = current_entry_notional / current_size
        close_size += fill.sz
        close_notional += fill.sz * fill.px
        close_last_ms = fill.time_ms if close_last_ms is None else max(close_last_ms, fill.time_ms)
        current_size = max(D("0"), current_size - fill.sz)
        current_entry_notional = average_entry * current_size

        if current_size > POSITION_EPSILON:
            continue

        current_size = D("0")
        final_execution_id = fill_execution_id(fill)
        lifecycle_id = (
            f"{boundary.user}:{boundary.coin}:{signal.direction}:"
            f"{boundary_execution_id}->{final_execution_id}"
        )
        if close_size <= 0 or gross_entry_size <= 0 or close_last_ms is None:
            return _failed(
                signal.signal_id,
                entry_time_error_ms=entry_time_error,
                lifecycle_id=lifecycle_id,
                boundary_execution_id=boundary_execution_id,
                final_execution_id=final_execution_id,
            )

        close_time_error = abs(close_last_ms - signal.closed_at_ms)
        reconstructed_exit = close_notional / close_size
        close_price_bps = _price_bps(signal.exit_price, reconstructed_exit)
        reconstructed_entry = gross_entry_notional / gross_entry_size
        entry_price_bps = _price_bps(signal.entry_price, reconstructed_entry)
        target_size = source_size
        close_size_error = abs(close_size / target_size - D("1"))
        entry_size_error = abs(gross_entry_size / target_size - D("1"))
        lifecycle_balance_error = abs(gross_entry_size - close_size) / gross_entry_size

        matched = (
            entry_time_error <= entry_time_tolerance_ms
            and close_time_error <= close_time_tolerance_ms
            and entry_price_bps <= entry_price_tolerance_bps
            and close_price_bps <= close_price_tolerance_bps
            and close_size_error <= max_size_ratio_error
            and entry_size_error <= max_size_ratio_error
            and lifecycle_balance_error <= POSITION_EPSILON
        )
        return LifecycleEpisodeEvidence(
            signal_id=signal.signal_id,
            matched=matched,
            close_time_error_ms=close_time_error,
            close_price_bps=close_price_bps,
            close_size_ratio_error=close_size_error,
            entry_time_error_ms=entry_time_error,
            entry_price_bps=entry_price_bps,
            entry_size_ratio_error=entry_size_error,
            reconstructed_entry=reconstructed_entry,
            reconstructed_size=gross_entry_size,
            lifecycle_id=lifecycle_id,
            boundary_execution_id=boundary_execution_id,
            final_execution_id=final_execution_id,
            rejection_reason=None if matched else "lifecycle_tolerance_mismatch",
        )

    return _failed(
        signal.signal_id,
        entry_time_error_ms=entry_time_error,
        reconstructed_size=gross_entry_size or None,
        boundary_execution_id=boundary_execution_id,
        rejection_reason="no_final_flatten",
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
) -> LifecycleEpisodeEvidence:
    if signal_position_size(signal) is None:
        return _failed(signal.signal_id, rejection_reason="missing_absolute_source_size")
    open_name, _, _, _, _ = _direction_names(signal.direction)
    order_index = {id(fill): index for index, fill in enumerate(fills)}
    boundaries = [
        fill
        for fill in fills
        if fill.direction.lower() == open_name
        and fill.start_position == D("0")
        and fill.time_ms <= signal.closed_at_ms + close_time_tolerance_ms
        and abs(fill.time_ms - signal.opened_at_ms) <= entry_time_tolerance_ms
    ]
    if not boundaries:
        return _failed(signal.signal_id, rejection_reason="no_flat_to_open_boundary")

    boundaries.sort(
        key=lambda fill: (
            abs(fill.time_ms - signal.opened_at_ms),
            fill.time_ms,
            fill.block_number,
            order_index.get(id(fill), 0),
        )
    )
    best_failure: LifecycleEpisodeEvidence | None = None
    for boundary in boundaries:
        evidence = _match_from_boundary(
            signal,
            fills,
            boundary=boundary,
            close_time_tolerance_ms=close_time_tolerance_ms,
            close_price_tolerance_bps=close_price_tolerance_bps,
            max_size_ratio_error=max_size_ratio_error,
            entry_time_tolerance_ms=entry_time_tolerance_ms,
            entry_price_tolerance_bps=entry_price_tolerance_bps,
        )
        if evidence.matched:
            return evidence
        if best_failure is None:
            best_failure = evidence
    return best_failure or _failed(signal.signal_id)
