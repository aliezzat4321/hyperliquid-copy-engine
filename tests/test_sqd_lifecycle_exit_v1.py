from decimal import Decimal

from hlcopy.resolver.public_trade_index import _public_trade_matches
from hlcopy.resolver.sqd_position_aware import SqdFill, match_episode
from hlcopy.signals.invo import CopySignal

D = Decimal
USER = "0x1111111111111111111111111111111111111111"


def _fill(
    *,
    px: str,
    sz: str,
    time_ms: int,
    direction: str,
    oid: str,
    start_position: str,
    tid: str | None = None,
    block_number: int = 1,
) -> SqdFill:
    return SqdFill(
        block_number=block_number,
        user=USER,
        coin="BTC",
        px=D(px),
        sz=D(sz),
        side="A",
        direction=direction,
        time_ms=time_ms,
        oid=oid,
        closed_pnl=D("0"),
        tid=tid or f"tid-{time_ms}",
        start_position=D(start_position),
    )


def _signal() -> CopySignal:
    return CopySignal(
        signal_id="lifecycle",
        source="generic_closed_trades_csv",
        trader="alice",
        coin="BTC",
        direction="LONG",
        source_leverage=D("2"),
        allocation_fraction=D("0.25"),
        entry_price=D("100"),
        exit_price=D("108"),
        opened_at_ms=1_000_000,
        closed_at_ms=2_000_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={"position_size": "1.0"},
    )


def test_exit_vwap_spans_separate_close_orders_until_final_flatten() -> None:
    fills = [
        _fill(
            px="100",
            sz="1",
            time_ms=1_000_000,
            direction="Open Long",
            oid="open",
            start_position="0",
        ),
        _fill(
            px="120",
            sz="0.4",
            time_ms=1_900_000,
            direction="Close Long",
            oid="close-a",
            start_position="1.0",
        ),
        _fill(
            px="100",
            sz="0.6",
            time_ms=2_000_000,
            direction="Close Long",
            oid="close-b",
            start_position="0.6",
        ),
    ]

    evidence = match_episode(
        _signal(),
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.05"),
        entry_time_tolerance_ms=5_000,
        entry_price_tolerance_bps=D("5"),
    )

    assert evidence.matched
    assert evidence.close_time_error_ms == 0
    assert evidence.close_price_bps == 0
    assert evidence.reconstructed_entry == D("100")
    assert evidence.reconstructed_size == D("1.0")


def test_same_block_split_closes_keep_sqd_execution_order_not_tid_order() -> None:
    signal = CopySignal(
        signal_id="split-close",
        source="generic_closed_trades_csv",
        trader="alice",
        coin="BTC",
        direction="SHORT",
        source_leverage=D("40"),
        allocation_fraction=D("1"),
        entry_price=D("64229.16484375"),
        exit_price=D("64193"),
        opened_at_ms=1_000_000,
        closed_at_ms=2_000_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={"position_size": "0.0128"},
    )
    fills = [
        _fill(
            px="64079",
            sz="0.00637",
            time_ms=1_000_000,
            direction="Open Short",
            oid="open-a",
            start_position="0",
            tid="100",
        ),
        _fill(
            px="64378",
            sz="0.00643",
            time_ms=1_500_000,
            direction="Open Short",
            oid="open-b",
            start_position="-0.00637",
            tid="200",
        ),
        # These are deliberately supplied in execution order but with tids that
        # sort in the opposite lexical order. startPosition proves the sequence.
        _fill(
            px="64193",
            sz="0.00689",
            time_ms=2_000_000,
            direction="Close Short",
            oid="close",
            start_position="-0.0128",
            tid="900",
            block_number=2,
        ),
        _fill(
            px="64193",
            sz="0.00591",
            time_ms=2_000_000,
            direction="Close Short",
            oid="close",
            start_position="-0.00591",
            tid="100",
            block_number=2,
        ),
    ]

    evidence = match_episode(
        signal,
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.05"),
        entry_time_tolerance_ms=5_000,
        entry_price_tolerance_bps=D("5"),
    )

    assert evidence.matched
    assert evidence.rejection_reason is None
    assert evidence.reconstructed_size == D("0.0128")


def test_discovery_rejects_final_flatten_outside_price_and_size_limits() -> None:
    # The first partial reduction happened outside the close discovery window.
    # The remaining final fill is 740+ bps away from the exported lifecycle VWAP
    # and only 60% of the exported size, so strict single-cluster matching fails.
    final_fill = _fill(
        px="100",
        sz="0.6",
        time_ms=2_000_000,
        direction="Close Long",
        oid="close-b",
        start_position="0.6",
    )

    matches = _public_trade_matches(
        _signal(),
        [final_fill],
        window_ms=5_000,
        max_price_bps=D("5"),
        max_size_ratio_error=D("0.05"),
    )

    assert matches == {}


def test_final_flatten_fallback_enforces_price_and_size_independently() -> None:
    wrong_price = _fill(
        px="100",
        sz="1",
        time_ms=2_000_000,
        direction="Close Long",
        oid="wrong-price",
        start_position="1",
    )
    wrong_size = _fill(
        px="108",
        sz="0.6",
        time_ms=2_000_000,
        direction="Close Long",
        oid="wrong-size",
        start_position="0.6",
    )

    assert _public_trade_matches(
        _signal(),
        [wrong_price],
        window_ms=5_000,
        max_price_bps=D("5"),
        max_size_ratio_error=D("0.05"),
    ) == {}
    assert _public_trade_matches(
        _signal(),
        [wrong_size],
        window_ms=5_000,
        max_price_bps=D("5"),
        max_size_ratio_error=D("0.05"),
    ) == {}


def test_partial_reduction_does_not_end_episode_before_later_add_and_flatten() -> None:
    fills = [
        _fill(
            px="100",
            sz="1",
            time_ms=1_000_000,
            direction="Open Long",
            oid="open",
            start_position="0",
        ),
        _fill(
            px="110",
            sz="0.5",
            time_ms=1_400_000,
            direction="Close Long",
            oid="reduce",
            start_position="1.0",
        ),
        _fill(
            px="100",
            sz="0.5",
            time_ms=1_500_000,
            direction="Long > Long",
            oid="add",
            start_position="0.5",
        ),
        _fill(
            px="106",
            sz="1.0",
            time_ms=2_000_000,
            direction="Close Long",
            oid="final",
            start_position="1.0",
        ),
    ]
    signal = _signal()
    signal = CopySignal(
        signal_id=signal.signal_id,
        source=signal.source,
        trader=signal.trader,
        coin=signal.coin,
        direction=signal.direction,
        source_leverage=signal.source_leverage,
        allocation_fraction=signal.allocation_fraction,
        entry_price=D("100"),
        exit_price=D("107.3333333333333333333333333"),
        opened_at_ms=signal.opened_at_ms,
        closed_at_ms=signal.closed_at_ms,
        entry_sim=signal.entry_sim,
        last_sim=signal.last_sim,
        reason_closed=signal.reason_closed,
        liquidated=signal.liquidated,
        raw={"position_size": "1.5"},
    )

    evidence = match_episode(
        signal,
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.05"),
        entry_time_tolerance_ms=5_000,
        entry_price_tolerance_bps=D("5"),
    )

    assert evidence.matched
    assert evidence.reconstructed_size == D("1.5")
