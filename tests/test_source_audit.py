from decimal import Decimal

from hlcopy.copyability.source_audit import audit_signals
from hlcopy.signals.invo import CopySignal

D = Decimal


def _signal(
    *,
    signal_id: str,
    direction: str,
    leverage: str,
    entry: str,
    exit: str,
    allocation: str,
    opened: int,
    closed: int,
    entry_sim: str | None = None,
    last_sim: str | None = None,
) -> CopySignal:
    return CopySignal(
        signal_id=signal_id,
        source="test",
        trader="bones",
        coin="BTC",
        direction=direction,
        source_leverage=D(leverage),
        allocation_fraction=D(allocation),
        entry_price=D(entry),
        exit_price=D(exit),
        opened_at_ms=opened,
        closed_at_ms=closed,
        entry_sim=D(entry_sim) if entry_sim is not None else None,
        last_sim=D(last_sim) if last_sim is not None else None,
        reason_closed="user_closed",
        liquidated=False,
        raw={},
    )


def test_audit_reconciles_trade_card_return_and_implied_equity():
    # 40x short: (65183 - 65049) / 65183 * 40 ~= 8.223%.
    signal = _signal(
        signal_id="bones-btc",
        direction="SHORT",
        leverage="40",
        entry="65183",
        exit="65049",
        allocation="0.01",
        opened=1_000,
        closed=2_000,
        entry_sim="1500",
        last_sim=str(D("1500") * (D("1") + (D("65183") - D("65049")) / D("65183") * D("40"))),
    )
    audit = audit_signals((signal,), starting_capital=D("10000"))
    assert audit.signals == 1
    assert audit.winners == 1
    assert audit.simulated_return_matches == 1
    assert audit.simulated_return_mismatches == 0
    assert audit.first_implied_equity == D("150000")
    assert audit.last_implied_equity == D("150000")
    assert audit.gross_realized_only_mirror.roi > D("0")
    assert audit.fee_adjusted_realized_only_mirror.roi > D("0")
    assert audit.fee_adjusted_realized_only_mirror.roi < audit.gross_realized_only_mirror.roi


def test_audit_flags_entry_sim_return_mismatch_without_rewriting_source_return():
    signal = _signal(
        signal_id="bad-sim",
        direction="LONG",
        leverage="10",
        entry="100",
        exit="101",
        allocation="0.01",
        opened=1_000,
        closed=2_000,
        entry_sim="100",
        last_sim="120",
    )
    audit = audit_signals((signal,))
    assert audit.source_win_rate == D("1")
    assert audit.simulated_return_matches == 0
    assert audit.simulated_return_mismatches == 1
    assert audit.mismatch_rows[0]["signal_id"] == "bad-sim"


def test_audit_tracks_concurrent_source_allocation():
    first = _signal(
        signal_id="a",
        direction="LONG",
        leverage="5",
        entry="100",
        exit="110",
        allocation="0.20",
        opened=1_000,
        closed=5_000,
    )
    second = _signal(
        signal_id="b",
        direction="SHORT",
        leverage="5",
        entry="100",
        exit="90",
        allocation="0.30",
        opened=2_000,
        closed=4_000,
    )
    audit = audit_signals((first, second))
    assert audit.max_concurrent_source_allocation == D("0.50")
