from decimal import Decimal

from hlcopy.resolver.public_trade_index import (
    HistoricalCandidateVerification,
    HistoricalVerification,
    _public_trade_matches,
    candidate_is_unique,
    select_historical_winner,
)
from hlcopy.resolver.reverse_index import CandidateFingerprint
from hlcopy.resolver.sqd_fills import SqdFill, aggregate_close_fills, match_episode
from hlcopy.signals.invo import CopySignal

D = Decimal
USER = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"


def _signal(direction: str = "LONG") -> CopySignal:
    return CopySignal(
        signal_id="s1",
        source="generic_closed_trades_csv",
        trader="alice",
        coin="BTC",
        direction=direction,
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


def _fill(
    *,
    user: str = USER,
    px: str,
    sz: str,
    time_ms: int,
    direction: str,
    oid: str,
) -> SqdFill:
    return SqdFill(
        block_number=1,
        user=user,
        coin="BTC",
        px=D(px),
        sz=D(sz),
        side="A" if "Long" in direction else "B",
        direction=direction,
        time_ms=time_ms,
        oid=oid,
        closed_pnl=D("0"),
        tid=f"tid-{time_ms}",
    )


def _candidate(address: str, matches: int, score: str) -> CandidateFingerprint:
    return CandidateFingerprint(
        address=address,
        matched_anchors=matches,
        total_anchors=8,
        match_ratio=D(matches) / D("8"),
        median_clock_offset_ms=0.0,
        clock_offset_mad_ms=0.0,
        median_offset_gap_ms=0.0,
        median_price_bps=D("1"),
        score=D(score),
        matches=(),
    )


def _historical(
    address: str,
    *,
    discovery_matches: int,
    discovery_score: str,
    matched: int,
    attempted: int = 12,
) -> HistoricalCandidateVerification:
    verification = HistoricalVerification(
        attempted=attempted,
        matched=matched,
        ratio=D(matched) / D(attempted),
        matched_signal_ids=tuple(f"s{i}" for i in range(matched)),
        evidence=(),
    )
    return HistoricalCandidateVerification(
        address=address,
        discovery_matches=discovery_matches,
        discovery_score=D(discovery_score),
        verification=verification,
    )


def test_partial_close_fills_are_aggregated_before_price_matching() -> None:
    fills = [
        _fill(px="109", sz="0.5", time_ms=1_999_900, direction="Close Long", oid="7"),
        _fill(px="111", sz="0.5", time_ms=2_000_000, direction="Close Long", oid="7"),
    ]
    closes = aggregate_close_fills(fills, direction="LONG")
    assert any(close.avg_price == D("110") and close.size == D("1.0") for close in closes)

    matches = _public_trade_matches(
        _signal(),
        fills,
        window_ms=5_000,
        max_price_bps=D("5"),
    )
    assert set(matches) == {USER}


def test_wrong_close_direction_does_not_create_candidate() -> None:
    fills = [
        _fill(px="110", sz="1", time_ms=2_000_000, direction="Close Short", oid="8")
    ]
    matches = _public_trade_matches(
        _signal("LONG"),
        fills,
        window_ms=5_000,
        max_price_bps=D("5"),
    )
    assert matches == {}


def test_full_position_episode_reconstructs_from_partial_fills() -> None:
    fills = [
        _fill(px="99", sz="0.5", time_ms=1_000_000, direction="Open Long", oid="1"),
        _fill(px="101", sz="0.5", time_ms=1_000_100, direction="Long > Long", oid="2"),
        _fill(px="109", sz="0.5", time_ms=1_999_900, direction="Close Long", oid="7"),
        _fill(px="111", sz="0.5", time_ms=2_000_000, direction="Close Long", oid="7"),
    ]
    evidence = match_episode(
        _signal(),
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.10"),
        entry_price_tolerance_bps=D("5"),
    )
    assert evidence.matched
    assert evidence.reconstructed_size == D("1.0")
    assert evidence.reconstructed_entry == D("100")


def test_candidate_must_beat_runner_up_on_matches_and_score() -> None:
    best = _candidate(USER, 6, "90")
    tied = _candidate(OTHER, 6, "60")
    close = _candidate("0x3333333333333333333333333333333333333333", 5, "80")
    clear = _candidate("0x4444444444444444444444444444444444444444", 4, "70")

    assert not candidate_is_unique((best, tied), min_score_gap=D("15"))
    assert not candidate_is_unique((best, close), min_score_gap=D("15"))
    assert candidate_is_unique((best, clear), min_score_gap=D("15"))


def test_held_out_episodes_can_overrule_close_only_discovery_leader() -> None:
    close_only_leader = _historical(
        USER,
        discovery_matches=5,
        discovery_score="90",
        matched=0,
    )
    episode_winner = _historical(
        OTHER,
        discovery_matches=4,
        discovery_score="70",
        matched=4,
    )
    runner_up = _historical(
        "0x3333333333333333333333333333333333333333",
        discovery_matches=4,
        discovery_score="65",
        matched=1,
    )
    winner = select_historical_winner(
        (episode_winner, close_only_leader, runner_up),
        min_matches=3,
        min_ratio=D("0.20"),
        min_match_gap=2,
    )
    assert winner is not None
    assert winner.address == OTHER


def test_historical_winner_must_be_unique() -> None:
    first = _historical(USER, discovery_matches=5, discovery_score="90", matched=4)
    second = _historical(OTHER, discovery_matches=4, discovery_score="80", matched=3)
    assert (
        select_historical_winner(
            (first, second),
            min_matches=3,
            min_ratio=D("0.20"),
            min_match_gap=2,
        )
        is None
    )
