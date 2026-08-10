from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from hlcopy.resolver.matcher import (
    candidate_events,
    decide_resolution,
    evidence_events,
    score_candidate,
)
from hlcopy.resolver.source_registry import ExternalSourceRegistry, ExternalSourceSpec
from hlcopy.signals.invo import CopySignal

D = Decimal


def _signal(index: int, opened: int, closed: int, coin: str = "BTC") -> CopySignal:
    return CopySignal(
        signal_id=f"trade-{index}",
        source="invo_export",
        trader="bones",
        coin=coin,
        direction="SHORT" if index % 2 else "LONG",
        source_leverage=D("40"),
        allocation_fraction=D("0.01"),
        entry_price=D("65000") + D(index),
        exit_price=D("64990") + D(index),
        opened_at_ms=opened,
        closed_at_ms=closed,
        entry_sim=None,
        last_sim=None,
        reason_closed="user_closed",
        liquidated=False,
        raw={},
    )


def _fill(tid: int, event, *, time_shift: int = 100, price_shift: str = "0") -> dict:
    if event.action == "OPEN":
        direction = f"Open {event.direction.title()}"
    else:
        direction = f"Close {event.direction.title()}"
    return {
        "tid": tid,
        "oid": tid,
        "hash": f"0x{tid:064x}",
        "time": event.timestamp_ms + time_shift,
        "coin": event.coin,
        "side": "B" if event.direction == "LONG" else "A",
        "dir": direction,
        "px": str(event.price + D(price_shift)),
        "sz": "0.01",
        "startPosition": "0",
        "closedPnl": "0",
        "fee": "0",
        "feeToken": "USDC",
        "crossed": True,
        "builderFee": "0",
    }


def test_exact_multi_trade_fingerprint_verifies_unique_wallet():
    signals = tuple(
        _signal(i, 1_000_000 + i * 100_000, 1_050_000 + i * 100_000)
        for i in range(8)
    )
    evidence = evidence_events(signals)
    rows = [_fill(i + 1, event) for i, event in enumerate(evidence)]
    candidate = candidate_events("0x" + "1" * 40, rows)
    strong = score_candidate(
        address="0x" + "1" * 40,
        evidence=evidence,
        candidate=candidate,
        time_tolerance_ms=5_000,
        price_tolerance_bps=D("5"),
    )
    weak = score_candidate(
        address="0x" + "2" * 40,
        evidence=evidence,
        candidate=(),
        time_tolerance_ms=5_000,
        price_tolerance_bps=D("5"),
    )
    decision = decide_resolution((strong, weak))
    assert decision.status == "VERIFIED"
    assert decision.verified_address == "0x" + "1" * 40
    assert strong.matched_events == 16
    assert strong.match_ratio == D("1")


def test_ambiguous_runner_up_never_verifies():
    signals = tuple(
        _signal(i, 1_000_000 + i * 100_000, 1_050_000 + i * 100_000)
        for i in range(8)
    )
    evidence = evidence_events(signals)
    first_rows = [_fill(i + 1, event, time_shift=100) for i, event in enumerate(evidence)]
    second_rows = [_fill(i + 101, event, time_shift=120) for i, event in enumerate(evidence)]
    first = score_candidate(
        address="0x" + "1" * 40,
        evidence=evidence,
        candidate=candidate_events("0x" + "1" * 40, first_rows),
        time_tolerance_ms=5_000,
        price_tolerance_bps=D("5"),
    )
    second = score_candidate(
        address="0x" + "2" * 40,
        evidence=evidence,
        candidate=candidate_events("0x" + "2" * 40, second_rows),
        time_tolerance_ms=5_000,
        price_tolerance_bps=D("5"),
    )
    ranked = tuple(sorted((first, second), key=lambda item: item.score, reverse=True))
    decision = decide_resolution(ranked)
    assert decision.status == "UNRESOLVED"
    assert "AMBIGUOUS_RUNNER_UP" in decision.reason_codes


def test_external_source_registry_is_separate_from_wallet_registry(tmp_path: Path):
    registry = ExternalSourceRegistry(tmp_path / "external_sources.json")
    registry.init()
    source = registry.add(
        ExternalSourceSpec(
            id="bones",
            label="Bones",
            adapter="invo_closed_trades_csv",
            evidence_path="/data/bones.csv",
        )
    )
    assert registry.get("bones") == source
    assert source.adapter == "invo_closed_trades_csv"
