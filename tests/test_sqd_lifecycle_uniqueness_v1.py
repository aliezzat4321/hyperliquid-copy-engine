from decimal import Decimal

import pytest

from hlcopy.resolver.public_trade_index import (
    PublicTradeDiscoveryConfig,
    _dedupe_reused_execution_matches,
    _episode_is_covered,
    verify_candidate_historically,
)
from hlcopy.resolver.reverse_index import AnchorMatch
from hlcopy.resolver.sqd_position_aware import SqdFill
from hlcopy.signals.invo import CopySignal

D = Decimal
USER = "0x1111111111111111111111111111111111111111"


def _anchor(signal_id: str, quality: str) -> AnchorMatch:
    return AnchorMatch(
        signal_id=signal_id,
        user=USER,
        trade_id="final-flatten:tid:shared-close",
        open_offset_ms=0,
        close_offset_ms=0,
        offset_gap_ms=0,
        entry_price_bps=D("0"),
        exit_price_bps=D("0"),
        quality=D(quality),
    )


def _signal(signal_id: str, *, time_shift_ms: int, price_shift: str) -> CopySignal:
    return CopySignal(
        signal_id=signal_id,
        source="generic_closed_trades_csv",
        trader="unknown",
        coin="BTC",
        direction="LONG",
        source_leverage=D("2"),
        allocation_fraction=D("1"),
        entry_price=D("100") + D(price_shift),
        exit_price=D("110") + D(price_shift),
        opened_at_ms=1_000_000 + time_shift_ms,
        closed_at_ms=2_000_000 + time_shift_ms,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={"position_size": "1"},
    )


def _fills() -> list[SqdFill]:
    return [
        SqdFill(
            block_number=1,
            user=USER,
            coin="BTC",
            px=D("100"),
            sz=D("1"),
            side="B",
            direction="Open Long",
            time_ms=1_000_000,
            oid="open",
            closed_pnl=D("0"),
            tid="open-tid",
            start_position=D("0"),
        ),
        SqdFill(
            block_number=2,
            user=USER,
            coin="BTC",
            px=D("110"),
            sz=D("1"),
            side="A",
            direction="Close Long",
            time_ms=2_000_000,
            oid="close",
            closed_pnl=D("10"),
            tid="close-tid",
            start_position=D("1"),
        ),
    ]


class _FakeClient:
    async def coverage_end_ms(self) -> int:
        return 3_000_000

    async def fills_between_times(self, **_: object) -> list[SqdFill]:
        return _fills()


def test_same_execution_can_only_supply_one_discovery_anchor_vote() -> None:
    deduped = _dedupe_reused_execution_matches(
        {
            "a": {USER: _anchor("a", "90")},
            "b": {USER: _anchor("b", "95")},
            "c": {USER: _anchor("c", "85")},
        }
    )

    kept = [signal_id for signal_id, rows in deduped.items() if USER in rows]
    assert kept == ["b"]


@pytest.mark.asyncio
async def test_near_duplicate_rows_cannot_reuse_one_lifecycle_for_three_matches() -> None:
    signals = (
        _signal("a", time_shift_ms=0, price_shift="0"),
        _signal("b", time_shift_ms=1, price_shift="0.01"),
        _signal("c", time_shift_ms=-1, price_shift="-0.01"),
    )
    config = PublicTradeDiscoveryConfig(
        historical_verify_trades=12,
        historical_entry_time_tolerance_ms=300_000,
        historical_time_tolerance_ms=25_000,
        historical_entry_price_tolerance_bps=D("15"),
        historical_price_tolerance_bps=D("35"),
        historical_max_size_ratio_error=D("0.45"),
    )

    result = await verify_candidate_historically(
        address=USER,
        signals=signals,
        excluded_signal_ids=set(),
        coverage_start_ms=0,
        client=_FakeClient(),  # type: ignore[arg-type]
        config=config,
    )

    assert result.attempted == 3
    assert result.matched == 1
    assert len(result.matched_signal_ids) == 1
    duplicate_rejections = [
        item
        for item in result.evidence
        if getattr(item, "rejection_reason", None) == "duplicate_lifecycle_reuse"
    ]
    assert len(duplicate_rejections) == 2


def test_episode_coverage_requires_complete_tolerance_windows() -> None:
    signal = _signal("coverage", time_shift_ms=0, price_shift="0")
    entry_margin = 300_000
    close_margin = 25_000

    assert _episode_is_covered(
        signal,
        coverage_start_ms=signal.opened_at_ms - entry_margin,
        coverage_end_ms=signal.closed_at_ms + close_margin,
        entry_margin_ms=entry_margin,
        close_margin_ms=close_margin,
    )
    assert not _episode_is_covered(
        signal,
        coverage_start_ms=signal.opened_at_ms - entry_margin + 1,
        coverage_end_ms=signal.closed_at_ms + close_margin,
        entry_margin_ms=entry_margin,
        close_margin_ms=close_margin,
    )
    assert not _episode_is_covered(
        signal,
        coverage_start_ms=signal.opened_at_ms - entry_margin,
        coverage_end_ms=signal.closed_at_ms + close_margin - 1,
        entry_margin_ms=entry_margin,
        close_margin_ms=close_margin,
    )
