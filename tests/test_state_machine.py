from decimal import Decimal

import pytest

from hlcopy.models import Fill
from hlcopy.positions.reconstruction import reconstruct_positions
from hlcopy.positions.state_machine import PositionReconstructionError


def fill(tid, time, side, size, start, px="100", pnl="0", fee="0"):
    raw = {
        "tid": tid,
        "oid": tid,
        "hash": f"0x{tid:064x}",
        "time": time,
        "coin": "BTC",
        "side": side,
        "dir": "",
        "px": px,
        "sz": size,
        "startPosition": start,
        "closedPnl": pnl,
        "fee": fee,
        "feeToken": "USDC",
        "crossed": True,
    }
    return Fill.from_raw("0x" + "1" * 40, raw)


def test_open_add_reduce_close_episode():
    fills = [
        fill(1, 1000, "B", "1", "0", "100", fee="0.1"),
        fill(2, 2000, "B", "0.5", "1", "110", fee="0.05"),
        fill(3, 3000, "A", "0.4", "1.5", "120", pnl="6", fee="0.04"),
        fill(4, 5000, "A", "1.1", "1.1", "125", pnl="20", fee="0.11"),
    ]
    episodes, states = reconstruct_positions(fills)
    assert states["BTC"].qty == 0
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.complete_start is True
    assert episode.direction == "LONG"
    assert episode.max_abs_size == Decimal("1.5")
    assert episode.realized_pnl == Decimal("26")
    assert episode.fees == Decimal("0.30")
    assert episode.holding_seconds == 4.0


def test_single_fill_reversal_closes_and_opens_new_episode():
    fills = [
        fill(1, 1000, "B", "1", "0"),
        fill(2, 2000, "A", "1.5", "1", "90", pnl="-10", fee="0.15"),
        fill(3, 3000, "B", "0.5", "-0.5", "80", pnl="5", fee="0.05"),
    ]
    episodes, states = reconstruct_positions(fills)
    assert len(episodes) == 2
    assert episodes[0].direction == "LONG"
    assert episodes[1].direction == "SHORT"
    assert states["BTC"].qty == 0
    assert episodes[1].avg_entry == Decimal("90")


def test_truncated_history_is_marked_incomplete_and_excluded_later():
    fills = [
        fill(1, 1000, "A", "1", "2", "110", pnl="10"),
        fill(2, 2000, "A", "1", "1", "120", pnl="20"),
        fill(3, 3000, "B", "1", "0", "100"),
        fill(4, 4000, "A", "1", "1", "130", pnl="30"),
    ]
    episodes, _ = reconstruct_positions(fills)
    assert len(episodes) == 2
    assert episodes[0].complete_start is False
    assert episodes[1].complete_start is True


def test_start_position_mismatch_is_fatal_data_quality_error():
    fills = [fill(1, 1000, "B", "1", "0"), fill(2, 2000, "A", "1", "2")]
    with pytest.raises(PositionReconstructionError):
        reconstruct_positions(fills)


def test_sub_nanounit_start_position_noise_is_not_a_false_rejection():
    fills = [
        fill(1, 1000, "B", "2171230.5", "0"),
        fill(2, 2000, "B", "0.5", "2171230.5000000002"),
        fill(3, 3000, "A", "2171231", "2171231.0", pnl="5"),
    ]

    episodes, states = reconstruct_positions(fills)

    assert states["BTC"].qty == 0
    assert len(episodes) == 1
    assert episodes[0].fill_tids == [1, 2, 3]


def test_real_position_difference_above_tolerance_still_fails_closed():
    fills = [
        fill(1, 1000, "B", "1", "0"),
        fill(2, 2000, "A", "1", "1.000001"),
    ]

    with pytest.raises(PositionReconstructionError):
        reconstruct_positions(fills)


def test_same_timestamp_fills_follow_position_chain_not_tid():
    fills = [
        fill(300, 1000, "B", "1", "0", px="100"),
        fill(100, 1000, "B", "1", "1", px="101"),
        fill(200, 1000, "A", "2", "2", px="102", pnl="3"),
    ]

    episodes, states = reconstruct_positions(fills)

    assert states["BTC"].qty == 0
    assert len(episodes) == 1
    assert episodes[0].complete_start is True
    assert episodes[0].fill_tids == [300, 100, 200]


def test_same_timestamp_precision_noise_keeps_position_chain_connected():
    fills = [
        fill(300, 1000, "B", "1", "0"),
        fill(100, 1000, "B", "1", "1.0000000004"),
        fill(200, 1000, "A", "2", "2", pnl="3"),
    ]

    episodes, states = reconstruct_positions(fills)

    assert states["BTC"].qty == 0
    assert len(episodes) == 1
    assert episodes[0].fill_tids == [300, 100, 200]


def test_disconnected_same_timestamp_fills_fail_closed():
    fills = [
        fill(1, 1000, "B", "1", "0"),
        fill(2, 1000, "B", "1", "5"),
    ]

    with pytest.raises(PositionReconstructionError, match="position-state trail"):
        reconstruct_positions(fills)


def test_reversal_does_not_inflate_closed_episode_max_size():
    fills = [
        fill(1, 1_000, "B", "1", "0", px="100"),
        fill(2, 2_000, "A", "3", "1", px="90", pnl="-10"),
    ]
    episodes, states = reconstruct_positions(fills)
    assert len(episodes) == 1
    assert episodes[0].direction == "LONG"
    assert episodes[0].max_abs_size == Decimal("1")
    assert states["BTC"].qty == Decimal("-2")
    assert states["BTC"].episode is not None
    assert states["BTC"].episode.max_abs_size == Decimal("2")
