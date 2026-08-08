from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from hlcopy.models import Fill

D = Decimal
ZERO = D("0")


class PositionReconstructionError(RuntimeError):
    pass


@dataclass(slots=True)
class PositionEpisode:
    wallet_address: str
    coin: str
    direction: str
    opened_at_ms: int | None
    closed_at_ms: int | None = None
    avg_entry: Decimal | None = None
    avg_exit: Decimal | None = None
    max_abs_size: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    entry_notional: Decimal = ZERO
    exit_notional: Decimal = ZERO
    fill_count: int = 0
    complete_start: bool = True
    fill_tids: list[int] = field(default_factory=list)

    @property
    def holding_seconds(self) -> float | None:
        if self.opened_at_ms is None or self.closed_at_ms is None:
            return None
        return max(0.0, (self.closed_at_ms - self.opened_at_ms) / 1000.0)

    @property
    def net_pnl_before_funding(self) -> Decimal:
        return self.realized_pnl - self.fees - self.funding


@dataclass(slots=True)
class InstrumentState:
    wallet_address: str
    coin: str
    qty: Decimal = ZERO
    episode: PositionEpisode | None = None

    def _bootstrap_if_needed(self, fill: Fill) -> None:
        if self.episode is not None or self.qty != ZERO:
            return
        if fill.start_position == ZERO:
            return
        self.qty = fill.start_position
        self.episode = PositionEpisode(
            wallet_address=self.wallet_address,
            coin=self.coin,
            direction="LONG" if self.qty > 0 else "SHORT",
            opened_at_ms=None,
            avg_entry=None,
            max_abs_size=abs(self.qty),
            complete_start=False,
        )

    def _assert_start_position(self, fill: Fill) -> None:
        if self.qty != fill.start_position:
            raise PositionReconstructionError(
                f"{self.wallet_address} {self.coin} tid={fill.tid}: reconstructed start "
                f"{self.qty} != source startPosition {fill.start_position}"
            )

    def apply(self, fill: Fill) -> list[PositionEpisode]:
        self._bootstrap_if_needed(fill)
        self._assert_start_position(fill)
        delta = fill.signed_size
        if delta == ZERO:
            return []

        before = self.qty
        after = before + delta
        completed: list[PositionEpisode] = []

        if before == ZERO:
            self.episode = PositionEpisode(
                wallet_address=self.wallet_address,
                coin=self.coin,
                direction="LONG" if delta > 0 else "SHORT",
                opened_at_ms=fill.timestamp_ms,
                avg_entry=fill.price,
                max_abs_size=abs(after),
                entry_notional=fill.notional,
                fees=fill.fee + fill.builder_fee,
                fill_count=1,
                fill_tids=[fill.tid],
            )
            self.qty = after
            return completed

        episode = self.episode
        if episode is None:
            raise PositionReconstructionError("non-zero position without an active episode")

        episode.fill_count += 1
        episode.fill_tids.append(fill.tid)

        same_direction_delta = before * delta > 0
        if same_direction_delta:
            episode.max_abs_size = max(episode.max_abs_size, abs(after))
            old_abs = abs(before)
            add_abs = abs(delta)
            if episode.avg_entry is not None:
                episode.avg_entry = (
                    episode.avg_entry * old_abs + fill.price * add_abs
                ) / (old_abs + add_abs)
            episode.entry_notional += fill.notional
            episode.fees += fill.fee + fill.builder_fee
            self.qty = after
            return completed

        closing_qty = min(abs(before), abs(delta))
        opening_qty = max(ZERO, abs(delta) - abs(before))
        total_qty = abs(delta)
        close_fraction = closing_qty / total_qty
        open_fraction = opening_qty / total_qty if opening_qty else ZERO
        total_fee = fill.fee + fill.builder_fee

        episode.realized_pnl += fill.closed_pnl
        episode.fees += total_fee * close_fraction
        episode.exit_notional += fill.price * closing_qty
        prior_closed_notional = episode.exit_notional - fill.price * closing_qty
        prior_closed_qty = ZERO
        if episode.avg_exit is not None and episode.avg_exit != ZERO:
            prior_closed_qty = prior_closed_notional / episode.avg_exit
        closed_qty_total = prior_closed_qty + closing_qty
        episode.avg_exit = (
            (prior_closed_notional + fill.price * closing_qty) / closed_qty_total
            if closed_qty_total
            else fill.price
        )

        if after == ZERO or before * after < ZERO:
            episode.closed_at_ms = fill.timestamp_ms
            completed.append(episode)
            self.episode = None

        if opening_qty > ZERO:
            self.episode = PositionEpisode(
                wallet_address=self.wallet_address,
                coin=self.coin,
                direction="LONG" if after > 0 else "SHORT",
                opened_at_ms=fill.timestamp_ms,
                avg_entry=fill.price,
                max_abs_size=opening_qty,
                entry_notional=fill.price * opening_qty,
                fees=total_fee * open_fraction,
                fill_count=1,
                fill_tids=[fill.tid],
            )
        self.qty = after
        return completed
