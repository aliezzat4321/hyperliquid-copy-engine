import asyncio
from decimal import Decimal

from hlcopy.resolver.public_trade_index import (
    HistoricalCandidateVerification,
    HistoricalVerification,
    PublicTradeDiscoveryConfig,
    _public_trade_matches,
    candidate_is_unique,
    discover_candidates,
    select_historical_winner,
    verify_candidate_historically,
    verify_candidate_shortlist,
)
from hlcopy.resolver.reverse_index import AnchorMatch, CandidateFingerprint
from hlcopy.resolver.sqd_fills import aggregate_close_fills
from hlcopy.resolver.sqd_position_aware import SqdFill, match_episode
from hlcopy.signals.invo import CopySignal

D = Decimal
USER = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"


def _signal(
    direction: str = "LONG",
    *,
    signal_id: str = "s1",
    entry_price: str = "100",
    exit_price: str = "110",
    opened_at_ms: int = 1_000_000,
    closed_at_ms: int = 2_000_000,
    raw: dict[str, str] | None = None,
) -> CopySignal:
    return CopySignal(
        signal_id=signal_id,
        source="generic_closed_trades_csv",
        trader="alice",
        coin="BTC",
        direction=direction,
        source_leverage=D("2"),
        allocation_fraction=D("0.25"),
        entry_price=D(entry_price),
        exit_price=D(exit_price),
        opened_at_ms=opened_at_ms,
        closed_at_ms=closed_at_ms,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={"position_size": "1.0"} if raw is None else raw,
    )


def _fill(
    *,
    user: str = USER,
    px: str,
    sz: str,
    time_ms: int,
    direction: str,
    oid: str,
    start_position: str | None = None,
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
        start_position=D(start_position) if start_position is not None else None,
    )


def _candidate(address: str, matches: int, score: str) -> CandidateFingerprint:
    anchor_matches = tuple(
        AnchorMatch(
            signal_id=f"anchor-{index}",
            user=address,
            trade_id=f"final-flatten:tid:{address}:{index}",
            open_offset_ms=0,
            close_offset_ms=0,
            offset_gap_ms=0,
            entry_price_bps=D("0"),
            exit_price_bps=D("0"),
            quality=D("1"),
        )
        for index in range(matches)
    )
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
        matches=anchor_matches,
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


class _FakeSqdClient:
    def __init__(self, *, start_ms: int = 500_000, end_ms: int = 2_500_000) -> None:
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.around_calls: list[int] = []
        self.between_calls = 0

    async def coverage_start_ms(self) -> int:
        return self.start_ms

    async def coverage_end_ms(self) -> int:
        return self.end_ms

    async def fills_around(
        self,
        *,
        timestamp_ms: int,
        coin: str,
        window_ms: int,
        user: str | None = None,
    ) -> list[SqdFill]:
        del coin, window_ms, user
        assert self.start_ms <= timestamp_ms <= self.end_ms
        self.around_calls.append(timestamp_ms)
        return []

    async def fills_between_times(
        self,
        *,
        start_ms: int,
        end_ms: int,
        coin: str,
        user: str,
    ) -> list[SqdFill]:
        del coin, user
        assert self.start_ms <= start_ms <= self.end_ms
        assert self.start_ms <= end_ms <= self.end_ms
        self.between_calls += 1
        return []


def test_partial_close_fills_are_aggregated_before_price_matching() -> None:
    fills = [
        _fill(
            px="109",
            sz="0.5",
            time_ms=1_999_900,
            direction="Close Long",
            oid="7",
            start_position="1.0",
        ),
        _fill(
            px="111",
            sz="0.5",
            time_ms=2_000_000,
            direction="Close Long",
            oid="7",
            start_position="0.5",
        ),
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
    fills = [_fill(px="110", sz="1", time_ms=2_000_000, direction="Close Short", oid="8")]
    matches = _public_trade_matches(
        _signal("LONG"),
        fills,
        window_ms=5_000,
        max_price_bps=D("5"),
    )
    assert matches == {}


def test_full_position_episode_reconstructs_from_partial_fills() -> None:
    fills = [
        _fill(
            px="99", sz="0.5", time_ms=1_000_000, direction="Open Long", oid="1", start_position="0"
        ),
        _fill(
            px="101",
            sz="0.5",
            time_ms=1_000_100,
            direction="Long > Long",
            oid="2",
            start_position="0.5",
        ),
        _fill(
            px="109",
            sz="0.5",
            time_ms=1_999_900,
            direction="Close Long",
            oid="7",
            start_position="1",
        ),
        _fill(
            px="111",
            sz="0.5",
            time_ms=2_000_000,
            direction="Close Long",
            oid="7",
            start_position="0.5",
        ),
    ]
    evidence = match_episode(
        _signal(),
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.10"),
        entry_time_tolerance_ms=5_000,
        entry_price_tolerance_bps=D("5"),
    )
    assert evidence.matched
    assert evidence.entry_time_error_ms == 0
    assert evidence.reconstructed_size == D("1.0")
    assert evidence.reconstructed_entry == D("100")


def test_no_source_size_still_requires_entry_reconstruction() -> None:
    fills = [
        _fill(
            px="99", sz="0.5", time_ms=1_000_000, direction="Open Long", oid="1", start_position="0"
        ),
        _fill(
            px="101",
            sz="0.5",
            time_ms=1_000_100,
            direction="Long > Long",
            oid="2",
            start_position="0.5",
        ),
        _fill(
            px="109",
            sz="0.5",
            time_ms=1_999_900,
            direction="Close Long",
            oid="7",
            start_position="1",
        ),
        _fill(
            px="111",
            sz="0.5",
            time_ms=2_000_000,
            direction="Close Long",
            oid="7",
            start_position="0.5",
        ),
    ]
    evidence = match_episode(
        _signal(raw={}),
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.10"),
        entry_time_tolerance_ms=5_000,
        entry_price_tolerance_bps=D("5"),
    )
    assert evidence.matched
    assert evidence.reconstructed_size == D("1.0")
    assert evidence.reconstructed_entry == D("100")

    wrong_entry = match_episode(
        _signal(entry_price="105", raw={}),
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.10"),
        entry_time_tolerance_ms=5_000,
        entry_price_tolerance_bps=D("5"),
    )
    assert not wrong_entry.matched
    assert wrong_entry.entry_price_bps is not None


def test_episode_rejects_right_price_at_wrong_entry_time() -> None:
    fills = [
        _fill(
            px="100", sz="1", time_ms=600_000, direction="Open Long", oid="1", start_position="0"
        ),
        _fill(
            px="110", sz="1", time_ms=2_000_000, direction="Close Long", oid="7", start_position="1"
        ),
    ]
    evidence = match_episode(
        _signal(opened_at_ms=1_000_000),
        fills,
        close_time_tolerance_ms=5_000,
        close_price_tolerance_bps=D("5"),
        max_size_ratio_error=D("0.10"),
        entry_time_tolerance_ms=30_000,
        entry_price_tolerance_bps=D("5"),
    )
    assert not evidence.matched
    assert evidence.entry_time_error_ms is None


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


def test_winner_gap_is_applied_before_runner_up_threshold_filtering() -> None:
    first = _historical(USER, discovery_matches=5, discovery_score="90", matched=3)
    second = _historical(OTHER, discovery_matches=4, discovery_score="80", matched=2)
    assert (
        select_historical_winner(
            (first, second),
            min_matches=3,
            min_ratio=D("0.20"),
            min_match_gap=2,
        )
        is None
    )


def test_threshold_reaching_finalists_over_cap_fail_closed_before_history_scan() -> None:
    ranked = tuple(_candidate(f"0x{index:040x}", 3, str(100 - index)) for index in range(1, 8))
    signals = (_signal(signal_id="held-out", opened_at_ms=900_000),)
    client = _FakeSqdClient()
    config = PublicTradeDiscoveryConfig(
        min_discovery_matches=3,
        max_candidates_to_verify=6,
        historical_verify_trades=1,
        historical_entry_time_tolerance_ms=300_000,
    )
    results = asyncio.run(
        verify_candidate_shortlist(
            ranked=ranked,
            signals=signals,
            excluded_signal_ids=set(),
            coverage_start_ms=client.start_ms,
            client=client,
            config=config,
        )
    )
    assert results == ()
    assert client.between_calls == 0


def test_all_threshold_reaching_finalists_are_verified_within_cap() -> None:
    ranked = tuple(_candidate(f"0x{index:040x}", 3, str(100 - index)) for index in range(1, 8))
    signals = (_signal(signal_id="held-out", opened_at_ms=900_000),)
    client = _FakeSqdClient()
    config = PublicTradeDiscoveryConfig(
        min_discovery_matches=3,
        max_candidates_to_verify=7,
        historical_verify_trades=1,
        historical_entry_time_tolerance_ms=300_000,
    )
    results = asyncio.run(
        verify_candidate_shortlist(
            ranked=ranked,
            signals=signals,
            excluded_signal_ids=set(),
            coverage_start_ms=client.start_ms,
            client=client,
            config=config,
        )
    )
    assert len(results) == 7
    assert {item.address for item in results} == {item.address for item in ranked}
    assert client.between_calls == 7


def test_discovery_excludes_evidence_beyond_finalized_head() -> None:
    client = _FakeSqdClient(end_ms=2_500_000)
    signals = (
        _signal(signal_id="a", opened_at_ms=900_000, closed_at_ms=1_500_000),
        _signal(signal_id="b", opened_at_ms=1_000_000, closed_at_ms=1_800_000),
        _signal(signal_id="c", opened_at_ms=1_100_000, closed_at_ms=2_000_000),
        _signal(signal_id="future", opened_at_ms=2_600_000, closed_at_ms=3_000_000),
    )
    result = asyncio.run(discover_candidates(signals, client=client))
    assert result.coverage_end_ms == 2_500_000
    assert {signal.signal_id for signal in result.anchors} == {"a", "b", "c"}
    assert all(timestamp <= result.coverage_end_ms for timestamp in client.around_calls)


def test_historical_verification_excludes_entry_before_coverage() -> None:
    client = _FakeSqdClient(start_ms=900_000, end_ms=2_500_000)
    signals = (
        _signal(signal_id="uncovered", opened_at_ms=800_000, closed_at_ms=1_500_000),
        _signal(signal_id="covered", opened_at_ms=1_300_000, closed_at_ms=1_800_000),
    )
    result = asyncio.run(
        verify_candidate_historically(
            address=USER,
            signals=signals,
            excluded_signal_ids=set(),
            coverage_start_ms=client.start_ms,
            client=client,
            config=PublicTradeDiscoveryConfig(historical_verify_trades=10),
        )
    )
    assert result.attempted == 1
    assert result.evidence[0].signal_id == "covered"


def test_historical_verification_enforces_configured_lookback() -> None:
    hour_ms = 60 * 60 * 1000
    client = _FakeSqdClient(start_ms=0, end_ms=10 * hour_ms)
    signals = (
        _signal(
            signal_id="outside-lookback",
            opened_at_ms=1 * hour_ms,
            closed_at_ms=2 * hour_ms,
        ),
        _signal(
            signal_id="inside-lookback",
            opened_at_ms=8 * hour_ms,
            closed_at_ms=9 * hour_ms,
        ),
    )

    result = asyncio.run(
        verify_candidate_historically(
            address=USER,
            signals=signals,
            excluded_signal_ids=set(),
            coverage_start_ms=client.start_ms,
            client=client,
            config=PublicTradeDiscoveryConfig(
                historical_verify_trades=10,
                historical_lookback_hours=6,
            ),
        )
    )

    assert result.attempted == 1
    assert result.evidence[0].signal_id == "inside-lookback"


def test_historical_lookback_is_anchored_before_discovery_exclusion() -> None:
    hour_ms = 60 * 60 * 1000
    client = _FakeSqdClient(start_ms=0, end_ms=110 * hour_ms)
    signals = (
        _signal(
            signal_id="old-held-out",
            opened_at_ms=1 * hour_ms,
            closed_at_ms=2 * hour_ms,
        ),
        _signal(
            signal_id="recent-anchor",
            opened_at_ms=99 * hour_ms,
            closed_at_ms=100 * hour_ms,
        ),
    )

    result = asyncio.run(
        verify_candidate_historically(
            address=USER,
            signals=signals,
            excluded_signal_ids={"recent-anchor"},
            coverage_start_ms=client.start_ms,
            client=client,
            config=PublicTradeDiscoveryConfig(
                historical_verify_trades=10,
                historical_lookback_hours=6,
            ),
        )
    )

    assert result.attempted == 0
    assert result.evidence == ()
