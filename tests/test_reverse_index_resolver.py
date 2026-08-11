from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from hlcopy.resolver.reverse_index import (
    IndexedCompletedTrade,
    _best_matches_for_anchor,
    _episode_matches_signal,
    parse_completed_trade,
    rank_candidates,
)
from hlcopy.signals.invo import CopySignal

D = Decimal


def _signal(
    signal_id: str,
    *,
    coin: str = "BTC",
    direction: str = "LONG",
    opened_at_ms: int = 1_000_000,
    closed_at_ms: int = 1_060_000,
    entry_price: str = "100",
    exit_price: str = "101",
) -> CopySignal:
    return CopySignal(
        signal_id=signal_id,
        source="invo_export",
        trader="bones",
        coin=coin,
        direction=direction,
        source_leverage=D("10"),
        allocation_fraction=D("0.01"),
        entry_price=D(entry_price),
        exit_price=D(exit_price),
        opened_at_ms=opened_at_ms,
        closed_at_ms=closed_at_ms,
        entry_sim=None,
        last_sim=None,
        reason_closed="user_closed",
        liquidated=False,
        raw={},
    )


def _trade(
    user: str,
    trade_id: str,
    *,
    start_ms: int,
    end_ms: int,
    entry: str,
    exit: str,
) -> IndexedCompletedTrade:
    return IndexedCompletedTrade(
        user=user,
        coin="BTC",
        direction="LONG",
        start_ms=start_ms,
        end_ms=end_ms,
        entry_price=D(entry),
        exit_price=D(exit),
        trade_id=trade_id,
        raw={},
    )


def test_parse_hypedexer_completed_trade() -> None:
    parsed = parse_completed_trade(
        {
            "user": "0x" + "a" * 40,
            "coin": "xyz:LLY",
            "direction": "long",
            "start_time": "2026-08-11T20:45:18.699000+00:00",
            "end_time": "2026-08-11T20:46:18.699000+00:00",
            "entry_price": 1213.4,
            "exit_price": 1214.0,
            "trade_id": "trade_xyz:LLY_test",
        }
    )
    assert parsed is not None
    assert parsed.user == "0x" + "a" * 40
    assert parsed.coin == "XYZ:LLY"
    assert parsed.direction == "LONG"
    assert parsed.start_ms == int(
        datetime(2026, 8, 11, 20, 45, 18, 699000, tzinfo=UTC).timestamp() * 1000
    )


def test_candidate_intersection_surfaces_consistent_wallet_and_clock_offset() -> None:
    good = "0x" + "1" * 40
    noise_a = "0x" + "2" * 40
    noise_b = "0x" + "3" * 40
    signals = [
        _signal("s1", opened_at_ms=1_000_000, closed_at_ms=1_060_000),
        _signal("s2", opened_at_ms=2_000_000, closed_at_ms=2_090_000),
        _signal("s3", opened_at_ms=3_000_000, closed_at_ms=3_120_000),
    ]
    matches = {}
    for index, signal in enumerate(signals):
        trades = [
            _trade(
                good,
                f"good-{index}",
                start_ms=signal.opened_at_ms + 18_000,
                end_ms=signal.closed_at_ms + 18_000,
                entry=str(signal.entry_price),
                exit=str(signal.exit_price),
            ),
            _trade(
                noise_a if index == 0 else noise_b,
                f"noise-{index}",
                start_ms=signal.opened_at_ms + 1_000,
                end_ms=signal.closed_at_ms + 1_000,
                entry=str(signal.entry_price),
                exit=str(signal.exit_price),
            ),
        ]
        matches[signal.signal_id] = _best_matches_for_anchor(
            signal,
            trades,
            window_ms=120_000,
            max_price_bps=D("25"),
        )

    ranked = rank_candidates(matches, total_anchors=3)
    assert ranked[0].address == good
    assert ranked[0].matched_anchors == 3
    assert ranked[0].median_clock_offset_ms == 18_000.0
    assert ranked[0].clock_offset_mad_ms == 0.0


def test_official_episode_verification_uses_position_vwap_and_clock_offset() -> None:
    signal = _signal(
        "heldout",
        coin="kBONK",
        direction="SHORT",
        opened_at_ms=5_000_000,
        closed_at_ms=5_300_000,
        entry_price="0.01234",
        exit_price="0.01230",
    )
    episode = SimpleNamespace(
        coin="kBONK",
        direction="SHORT",
        opened_at_ms=5_018_000,
        closed_at_ms=5_318_000,
        avg_entry=D("0.01234"),
        avg_exit=D("0.01230"),
    )
    assert _episode_matches_signal(
        signal,
        episode,
        clock_offset_ms=18_000.0,
        time_tolerance_ms=1_000,
        price_tolerance_bps=D("2"),
    )
