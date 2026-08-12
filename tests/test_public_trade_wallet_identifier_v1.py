from decimal import Decimal
from pathlib import Path

import pytest

from hlcopy.resolver.public_trade_index import (
    _aws_cp,
    _public_trade_matches,
    candidate_is_unique,
)
from hlcopy.resolver.reverse_index import CandidateFingerprint
from hlcopy.signals.invo import CopySignal

D = Decimal


def _signal(direction: str) -> CopySignal:
    return CopySignal(
        signal_id="s1",
        source="invo_export",
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
        raw={},
    )


def _row() -> dict[str, object]:
    return {
        "coin": "BTC",
        "time": "1970-01-01T00:33:20+00:00",
        "px": "110.01",
        "hash": "0xabc",
        "side_info": [
            {"user": "0x1111111111111111111111111111111111111111"},
            {"user": "0x2222222222222222222222222222222222222222"},
        ],
    }


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


def test_long_close_selects_seller_address() -> None:
    matches = _public_trade_matches(
        _signal("LONG"),
        [_row()],
        window_ms=5_000,
        max_price_bps=D("5"),
    )
    assert set(matches) == {"0x2222222222222222222222222222222222222222"}


def test_short_close_selects_buyer_address() -> None:
    matches = _public_trade_matches(
        _signal("SHORT"),
        [_row()],
        window_ms=5_000,
        max_price_bps=D("5"),
    )
    assert set(matches) == {"0x1111111111111111111111111111111111111111"}


def test_wrong_price_does_not_create_candidate() -> None:
    row = _row()
    row["px"] = "120"
    matches = _public_trade_matches(
        _signal("LONG"),
        [row],
        window_ms=5_000,
        max_price_bps=D("5"),
    )
    assert matches == {}


def test_candidate_must_beat_runner_up_on_matches_and_score() -> None:
    best = _candidate("0x1111111111111111111111111111111111111111", 6, "90")
    tied = _candidate("0x2222222222222222222222222222222222222222", 6, "60")
    close = _candidate("0x3333333333333333333333333333333333333333", 5, "80")
    clear = _candidate("0x4444444444444444444444444444444444444444", 4, "70")

    assert not candidate_is_unique((best, tied), min_score_gap=D("15"))
    assert not candidate_is_unique((best, close), min_score_gap=D("15"))
    assert candidate_is_unique((best, clear), min_score_gap=D("15"))


def test_requester_pays_fallback_is_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HLCOPY_ALLOW_REQUESTER_PAYS", raising=False)
    with pytest.raises(RuntimeError, match="requester-pays"):
        _aws_cp("s3://hl-mainnet-node-data/node_trades/hourly/20260801/1", tmp_path / "x")
