from __future__ import annotations

from decimal import Decimal

from hlcopy.resolver.invo_builder_identifier import (
    BuilderCandidateVerification,
    BuilderFill,
    _best_builder_matches,
    _match_official_close,
    _select_verified_winner,
    parse_builder_csv,
)
from hlcopy.signals.invo import CopySignal

D = Decimal


def _signal(
    signal_id: str = "s1",
    *,
    coin: str = "ETH",
    direction: str = "LONG",
    close_ms: int = 1_777_000_000_000,
    exit_price: str = "2500",
) -> CopySignal:
    return CopySignal(
        signal_id=signal_id,
        source="invo_export",
        trader="alice",
        coin=coin,
        direction=direction,
        source_leverage=D("5"),
        allocation_fraction=D("0.1"),
        entry_price=D("2450"),
        exit_price=D(exit_price),
        opened_at_ms=close_ms - 60_000,
        closed_at_ms=close_ms,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={},
    )


def test_parse_builder_csv_exposes_actual_user_wallet() -> None:
    raw = (
        "time,user,coin,side,px,sz,crossed,special_trade_type,tif,is_trigger,"
        "counterparty,closed_pnl,twap_id,builder_fee\n"
        "2026-08-25T00:00:05Z,0xd6bbd9a3e9a736782a03ce5d0cb1ff532e0e160b,"
        "ETH,Ask,2481.3,0.3184,true,Na,Ioc,false,"
        "0x6f7aa825d69692aa57f65b60a596f077f2a8b561,0.09552,0,0.276516\n"
    ).encode()
    rows = parse_builder_csv(raw)
    assert len(rows) == 1
    assert rows[0].user == "0xd6bbd9a3e9a736782a03ce5d0cb1ff532e0e160b"
    assert rows[0].side == "ask"
    assert rows[0].coin == "ETH"


def test_builder_close_side_filters_opens_and_wrong_direction() -> None:
    signal = _signal(direction="LONG", exit_price="2500")
    good = BuilderFill(
        time_ms=signal.closed_at_ms + 2_000,
        user="0x1111111111111111111111111111111111111111",
        coin="ETH",
        side="ask",
        px=D("2500.5"),
        sz=D("1"),
        closed_pnl=D("1"),
        counterparty="0x2222222222222222222222222222222222222222",
        builder_fee=D("0.01"),
        execution_id="good",
    )
    opening_side = BuilderFill(
        time_ms=signal.closed_at_ms + 1_000,
        user="0x3333333333333333333333333333333333333333",
        coin="ETH",
        side="bid",
        px=D("2500"),
        sz=D("1"),
        closed_pnl=D("0"),
        counterparty="0x4444444444444444444444444444444444444444",
        builder_fee=D("0.01"),
        execution_id="wrong",
    )
    matches = _best_builder_matches(signal, [opening_side, good])
    assert list(matches) == [good.user]


def test_official_verification_requires_final_flatten_start_position() -> None:
    signal = _signal(exit_price="2500")
    user_rows = [
        {
            "coin": "ETH",
            "dir": "Close Long",
            "time": signal.closed_at_ms + 2_000,
            "px": "2500.2",
            "sz": "1",
            "startPosition": "2",
            "tid": 1,
        },
        {
            "coin": "ETH",
            "dir": "Close Long",
            "time": signal.closed_at_ms + 2_500,
            "px": "2500.1",
            "sz": "1",
            "startPosition": "1",
            "tid": 2,
        },
    ]
    matched = _match_official_close(signal, user_rows, expected_offset_ms=2_000)
    assert matched is not None
    row, _, _ = matched
    assert row["tid"] == 2


def test_verified_winner_requires_gap_and_quality() -> None:
    winner = BuilderCandidateVerification(
        address="0x1111111111111111111111111111111111111111",
        attempted=12,
        matched=8,
        ratio=D("0.6666666667"),
        clock_offset_mad_ms=1_200.0,
        median_price_bps=D("2.5"),
        matched_signal_ids=tuple(f"s{i}" for i in range(8)),
    )
    runner = BuilderCandidateVerification(
        address="0x2222222222222222222222222222222222222222",
        attempted=12,
        matched=5,
        ratio=D("0.4166666667"),
        clock_offset_mad_ms=1_000.0,
        median_price_bps=D("2"),
        matched_signal_ids=tuple(f"r{i}" for i in range(5)),
    )
    assert _select_verified_winner([runner, winner]) == winner

    too_close = BuilderCandidateVerification(
        address=runner.address,
        attempted=12,
        matched=7,
        ratio=D("0.5833333333"),
        clock_offset_mad_ms=1_000.0,
        median_price_bps=D("2"),
        matched_signal_ids=tuple(f"r{i}" for i in range(7)),
    )
    assert _select_verified_winner([winner, too_close]) is None
