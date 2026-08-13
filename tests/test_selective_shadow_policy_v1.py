from decimal import Decimal

import pytest

from hlcopy.shadow.selective_policy import (
    EffectivePolicyStore,
    SelectivePolicy,
    SelectiveRule,
)

D = Decimal


def _policy(
    policy_id: str,
    *,
    generated: int,
    effective: int,
    training_end: int,
    state: str = "SHADOW_ONLY",
    coin: str | None = "XYZ:NBIS",
    max_notional: str | None = "5000",
) -> SelectivePolicy:
    rule = SelectiveRule(
        policy_id=policy_id,
        effective_from_ns=effective,
        training_end_ns=training_end,
        wallet_address="0xabc",
        state=state,
        coin=coin,
        direction="LONG",
        action="REDUCE",
        max_notional_usd=D(max_notional) if max_notional is not None else None,
        reason_codes=("OOS_PROMOTED",),
    )
    return SelectivePolicy(
        policy_id=policy_id,
        generated_at_ns=generated,
        effective_from_ns=effective,
        training_end_ns=training_end,
        research_only=True,
        rules=(rule,),
    )


def test_future_policy_cannot_change_past_decision() -> None:
    old = _policy("p1", generated=90, effective=100, training_end=80, state="SHADOW_ONLY")
    new = _policy("p2", generated=190, effective=200, training_end=180, state="SKIP")
    store = EffectivePolicyStore((old, new))

    before_new = store.decide(
        decision_time_ns=150,
        wallet_address="0xAbC",
        coin="XYZ:NBIS",
        direction="LONG",
        action="REDUCE",
        notional_usd=D("1000"),
    )
    after_new = store.decide(
        decision_time_ns=250,
        wallet_address="0xabc",
        coin="XYZ:NBIS",
        direction="LONG",
        action="REDUCE",
        notional_usd=D("1000"),
    )

    assert before_new.policy_id == "p1"
    assert before_new.state == "SHADOW_ONLY"
    assert after_new.policy_id == "p2"
    assert after_new.state == "SKIP"


def test_no_policy_before_effective_time_fails_closed() -> None:
    store = EffectivePolicyStore((_policy("p1", generated=90, effective=100, training_end=80),))

    decision = store.decide(
        decision_time_ns=99,
        wallet_address="0xabc",
        coin="XYZ:NBIS",
        direction="LONG",
        action="REDUCE",
        notional_usd=D("1000"),
    )

    assert decision.state == "SKIP"
    assert decision.reason == "NO_EFFECTIVE_POLICY"


def test_training_data_must_end_strictly_before_policy_is_effective() -> None:
    with pytest.raises(ValueError, match="training_end_ns"):
        _policy("bad", generated=90, effective=100, training_end=100)


def test_policy_cannot_be_generated_after_its_effective_time() -> None:
    with pytest.raises(ValueError, match="generated_at_ns"):
        _policy("bad", generated=101, effective=100, training_end=80)


def test_specific_rule_beats_wallet_default_and_capacity_fails_closed() -> None:
    default = SelectiveRule(
        policy_id="p1",
        effective_from_ns=100,
        training_end_ns=80,
        wallet_address="0xabc",
        state="SKIP",
    )
    nbis = SelectiveRule(
        policy_id="p1",
        effective_from_ns=100,
        training_end_ns=80,
        wallet_address="0xabc",
        state="SHADOW_ONLY",
        coin="XYZ:NBIS",
        direction="LONG",
        action="REDUCE",
        max_notional_usd=D("5000"),
    )
    store = EffectivePolicyStore(
        (
            SelectivePolicy(
                policy_id="p1",
                generated_at_ns=90,
                effective_from_ns=100,
                training_end_ns=80,
                research_only=True,
                rules=(default, nbis),
            ),
        )
    )

    allowed = store.decide(
        decision_time_ns=150,
        wallet_address="0xabc",
        coin="XYZ:NBIS",
        direction="LONG",
        action="REDUCE",
        notional_usd=D("5000"),
    )
    oversized = store.decide(
        decision_time_ns=150,
        wallet_address="0xabc",
        coin="XYZ:NBIS",
        direction="LONG",
        action="REDUCE",
        notional_usd=D("5001"),
    )

    assert allowed.state == "SHADOW_ONLY"
    assert allowed.rule is nbis
    assert oversized.state == "SKIP"
    assert oversized.rule is default


def test_duplicate_policy_ids_are_rejected() -> None:
    p1 = _policy("same", generated=90, effective=100, training_end=80)
    p2 = _policy("same", generated=190, effective=200, training_end=180)

    with pytest.raises(ValueError, match="duplicate policy_id"):
        EffectivePolicyStore((p1, p2))
