from decimal import Decimal

from hlcopy.resolver.sqd_fills import SqdFill, match_episode
from hlcopy.signals.invo import CopySignal

D = Decimal
USER = "0x1111111111111111111111111111111111111111"


def _fill(*, px: str, sz: str, time_ms: int, direction: str, oid: str) -> SqdFill:
    return SqdFill(
        block_number=1,
        user=USER,
        coin="BTC",
        px=D(px),
        sz=D(sz),
        side="A",
        direction=direction,
        time_ms=time_ms,
        oid=oid,
        closed_pnl=D("0"),
        tid=f"tid-{time_ms}",
    )


def _signal() -> CopySignal:
    return CopySignal(
        signal_id="split-open",
        source="generic_closed_trades_csv",
        trader="alice",
        coin="BTC",
        direction="LONG",
        source_leverage=D("2"),
        allocation_fraction=D("0.25"),
        entry_price=D("100"),
        exit_price=D("110"),
        opened_at_ms=1_000_000,
        closed_at_ms=2_000_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={"position_size": "1.0"},
    )


def _match(fills: list[SqdFill]):
    return match_episode(
        _signal(),
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.10"),
        entry_time_tolerance_ms=5_000,
        entry_price_tolerance_bps=D("5"),
    )


def test_split_open_same_oid_is_one_entry_event() -> None:
    fills = [
        _fill(px="99", sz="0.5", time_ms=1_000_000, direction="Open Long", oid="1"),
        _fill(px="101", sz="0.5", time_ms=1_000_050, direction="Open Long", oid="1"),
        _fill(px="109", sz="0.5", time_ms=1_999_900, direction="Close Long", oid="7"),
        _fill(px="111", sz="0.5", time_ms=2_000_000, direction="Close Long", oid="7"),
    ]
    evidence = _match(fills)
    assert evidence.matched
    assert evidence.entry_time_error_ms == 0
    assert evidence.reconstructed_size == D("1.0")
    assert evidence.reconstructed_entry == D("100")


def test_distinct_same_direction_open_orders_build_one_continuous_position() -> None:
    fills = [
        _fill(px="99", sz="0.5", time_ms=1_000_000, direction="Open Long", oid="1"),
        _fill(px="101", sz="0.5", time_ms=1_500_000, direction="Open Long", oid="2"),
        _fill(px="109", sz="0.5", time_ms=1_999_900, direction="Close Long", oid="7"),
        _fill(px="111", sz="0.5", time_ms=2_000_000, direction="Close Long", oid="7"),
    ]
    evidence = _match(fills)
    assert evidence.matched
    assert evidence.reconstructed_size == D("1.0")
    assert evidence.reconstructed_entry == D("100")
