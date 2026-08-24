from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from hlcopy.profitability.causal_selective_live_cli import _allowed, _evidence_cutoff_ns
from hlcopy.profitability.position_copy import CopyFillEvent
from hlcopy.shadow.selective_policy import EffectivePolicyStore, SelectivePolicy, SelectiveRule


def _event(received_at_ns: int, coin: str = "BTC") -> CopyFillEvent:
    return CopyFillEvent(
        lane="WIDE",
        wallet_id="w",
        wallet_address="0xabc",
        coin=coin,
        exchange_ts_ms=1,
        received_at_ns=received_at_ns,
        tid=1,
        leader_start=Decimal("0"),
        leader_after=Decimal("1"),
        leader_delta=Decimal("1"),
        source_price=Decimal("100"),
    )


def _store() -> EffectivePolicyStore:
    rule = SelectiveRule(
        policy_id="p1",
        effective_from_ns=200,
        training_end_ns=100,
        wallet_address="0xabc",
        state="SHADOW_ONLY",
        coin="BTC",
    )
    policy = SelectivePolicy(
        policy_id="p1",
        generated_at_ns=150,
        effective_from_ns=200,
        training_end_ns=100,
        research_only=True,
        rules=(rule,),
    )
    return EffectivePolicyStore((policy,))


def test_selective_shadow_never_uses_policy_before_activation() -> None:
    store = _store()
    assert _allowed(store, _event(199)) is False
    assert _allowed(store, _event(200)) is True


def test_selective_shadow_requires_wallet_coin_match() -> None:
    store = _store()
    assert _allowed(store, _event(300, coin="ETH")) is False


def test_selective_evidence_cutoff_reads_fail_closed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cutoff = tmp_path / "cutoff.txt"
    cutoff.write_text("123456789\n", encoding="utf-8")
    monkeypatch.setenv("HLCOPY_SELECTIVE_EVIDENCE_CUTOFF_NS_FILE", str(cutoff))
    assert _evidence_cutoff_ns() == 123456789


def test_selective_evidence_cutoff_requires_configured_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing.txt"
    monkeypatch.setenv("HLCOPY_SELECTIVE_EVIDENCE_CUTOFF_NS_FILE", str(missing))
    with pytest.raises(SystemExit, match="cutoff file missing"):
        _evidence_cutoff_ns()
