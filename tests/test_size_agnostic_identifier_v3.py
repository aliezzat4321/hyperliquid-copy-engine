from __future__ import annotations

import asyncio
import csv
import json
from decimal import Decimal

from hlcopy.resolver.size_agnostic_identifier import (
    _discovery_matches_without_size,
    _match_lifecycle_without_size,
    identify_wallet_from_csv_size_aware,
)
from hlcopy.resolver.sqd_position_aware import SqdFill
from hlcopy.signals.invo import CopySignal

D = Decimal
WALLET = "0x1111111111111111111111111111111111111111"


def _signal(*, signal_id: str = "trade-1") -> CopySignal:
    return CopySignal(
        signal_id=signal_id,
        source="generic_closed_trades_csv",
        trader="carmine",
        coin="BTC",
        direction="LONG",
        source_leverage=D("10"),
        allocation_fraction=D("0.10"),
        entry_price=D("100.5"),
        exit_price=D("110"),
        opened_at_ms=1_000_000,
        closed_at_ms=2_000_000,
        entry_sim=None,
        last_sim=None,
        reason_closed="",
        liquidated=False,
        raw={"entry_size": "10", "portfolio_id": "portfolio-carmine"},
    )


def _fill(
    *,
    direction: str,
    time_ms: int,
    px: str,
    sz: str,
    start_position: str,
    tid: str,
    oid: str,
) -> SqdFill:
    return SqdFill(
        block_number=1,
        user=WALLET,
        coin="BTC",
        px=D(px),
        sz=D(sz),
        side="A",
        direction=direction,
        time_ms=time_ms,
        oid=oid,
        closed_pnl=D("0"),
        tid=tid,
        start_position=D(start_position),
    )


def test_size_agnostic_discovery_requires_proven_final_flatten() -> None:
    signal = _signal()
    partial = _fill(
        direction="Close Long",
        time_ms=2_005_000,
        px="110",
        sz="1",
        start_position="2",
        tid="partial",
        oid="close-1",
    )
    final = _fill(
        direction="Close Long",
        time_ms=2_005_000,
        px="110",
        sz="2",
        start_position="2",
        tid="final",
        oid="close-2",
    )

    assert not _discovery_matches_without_size(
        signal,
        [partial],
        window_ms=30_000,
        max_price_bps=D("25"),
    )
    matches = _discovery_matches_without_size(
        signal,
        [partial, final],
        window_ms=30_000,
        max_price_bps=D("25"),
    )
    assert set(matches) == {WALLET}
    assert matches[WALLET].trade_id == "final-flatten:tid:final"


def test_size_agnostic_discovery_rejects_wrong_price() -> None:
    signal = _signal()
    wrong_price = _fill(
        direction="Close Long",
        time_ms=2_005_000,
        px="115",
        sz="2",
        start_position="2",
        tid="wrong-price",
        oid="close-2",
    )

    assert not _discovery_matches_without_size(
        signal,
        [wrong_price],
        window_ms=30_000,
        max_price_bps=D("25"),
    )


def test_size_agnostic_verification_reconstructs_full_split_lifecycle() -> None:
    signal = _signal()
    fills = [
        _fill(
            direction="Open Long",
            time_ms=1_003_000,
            px="100",
            sz="1",
            start_position="0",
            tid="open",
            oid="open-1",
        ),
        _fill(
            direction="Long > Long",
            time_ms=1_004_000,
            px="101",
            sz="1",
            start_position="1",
            tid="add",
            oid="open-2",
        ),
        _fill(
            direction="Close Long",
            time_ms=2_004_000,
            px="109",
            sz="0.5",
            start_position="2",
            tid="partial-close",
            oid="close-1",
        ),
        _fill(
            direction="Close Long",
            time_ms=2_005_000,
            px="110.333333333333333333",
            sz="1.5",
            start_position="1.5",
            tid="final-close",
            oid="close-2",
        ),
    ]

    evidence = _match_lifecycle_without_size(
        signal,
        fills,
        expected_close_offset_ms=5_000,
        close_time_tolerance_ms=25_000,
        close_price_tolerance_bps=D("35"),
        entry_time_tolerance_ms=300_000,
        entry_price_tolerance_bps=D("15"),
    )

    assert evidence.matched is True
    assert evidence.lifecycle_id is not None
    assert evidence.final_execution_id == "tid:final-close"
    assert evidence.reconstructed_size == D("2")
    assert evidence.entry_size_ratio_error is None
    assert evidence.close_size_ratio_error is None


def test_size_agnostic_verification_requires_stable_close_clock_offset() -> None:
    signal = _signal()
    fills = [
        _fill(
            direction="Open Long",
            time_ms=1_003_000,
            px="100.5",
            sz="2",
            start_position="0",
            tid="open",
            oid="open-1",
        ),
        _fill(
            direction="Close Long",
            time_ms=2_005_000,
            px="110",
            sz="2",
            start_position="2",
            tid="final-close",
            oid="close-1",
        ),
    ]

    evidence = _match_lifecycle_without_size(
        signal,
        fills,
        expected_close_offset_ms=25_000,
        close_time_tolerance_ms=25_000,
        close_price_tolerance_bps=D("35"),
        entry_time_tolerance_ms=300_000,
        entry_price_tolerance_bps=D("15"),
    )

    assert evidence.matched is False
    assert evidence.rejection_reason == "size_agnostic_lifecycle_tolerance_mismatch"


def test_size_agnostic_identifier_waits_for_20_independent_trades(tmp_path) -> None:
    evidence_path = tmp_path / "carmine.csv"
    with evidence_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "trade_id",
                "portfolio_id",
                "username",
                "ticker",
                "direction",
                "leverage",
                "entry_price",
                "closing_price",
                "entry_size",
                "opened_at",
                "closed_at",
            ],
        )
        writer.writeheader()
        for index, coin in enumerate(("BTC", "ETH", "SOL"), start=1):
            writer.writerow(
                {
                    "trade_id": f"trade-{index}",
                    "portfolio_id": "portfolio-carmine",
                    "username": "carmine",
                    "ticker": coin,
                    "direction": "LONG",
                    "leverage": "10",
                    "entry_price": str(100 + index),
                    "closing_price": str(101 + index),
                    "entry_size": "10",
                    "opened_at": f"2026-08-{index:02d}T00:00:00Z",
                    "closed_at": f"2026-08-{index:02d}T01:00:00Z",
                }
            )

    result = asyncio.run(
        identify_wallet_from_csv_size_aware(
            evidence_path,
            output_dir=tmp_path / "reports",
            expected_source_identity="portfolio-carmine",
        )
    )

    assert result.status == "UNRESOLVED"
    assert result.wallet is None
    assert result.candidate is None
    assert result.input_trades == 3
    assert result.report_path is not None
    report = json.loads((tmp_path / "reports" / result.report_path.split("/")[-1]).read_text())
    assert report["mode"] == "SIZE_AGNOSTIC_SEQUENCE"
    assert "requires at least 20" in report["unresolved_reason"]
