from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

D = Decimal

_ALLOWED_STATES = {"COPY", "SKIP", "SHADOW_ONLY"}


@dataclass(frozen=True, slots=True)
class SelectiveRule:
    policy_id: str
    effective_from_ns: int
    training_end_ns: int
    wallet_address: str
    state: str
    coin: str | None = None
    direction: str | None = None
    action: str | None = None
    max_notional_usd: Decimal | None = None
    reason_codes: tuple[str, ...] = ()

    def matches(
        self,
        *,
        wallet_address: str,
        coin: str,
        direction: str,
        action: str,
        notional_usd: Decimal,
    ) -> bool:
        if self.wallet_address != wallet_address.lower():
            return False
        if self.coin is not None and self.coin != coin:
            return False
        if self.direction is not None and self.direction != direction:
            return False
        if self.action is not None and self.action != action:
            return False
        if self.max_notional_usd is not None and notional_usd > self.max_notional_usd:
            return False
        return True

    @property
    def specificity(self) -> int:
        return sum(value is not None for value in (self.coin, self.direction, self.action))


@dataclass(frozen=True, slots=True)
class SelectivePolicy:
    policy_id: str
    generated_at_ns: int
    effective_from_ns: int
    training_end_ns: int
    research_only: bool
    rules: tuple[SelectiveRule, ...]

    def __post_init__(self) -> None:
        if not self.policy_id:
            raise ValueError("policy_id is required")
        if not self.research_only:
            raise ValueError("selective policy must remain research_only")
        if self.training_end_ns >= self.effective_from_ns:
            raise ValueError("training_end_ns must be strictly before effective_from_ns")
        if self.generated_at_ns > self.effective_from_ns:
            raise ValueError("generated_at_ns cannot be after effective_from_ns")
        for rule in self.rules:
            if rule.policy_id != self.policy_id:
                raise ValueError("rule policy_id mismatch")
            if rule.effective_from_ns != self.effective_from_ns:
                raise ValueError("rule effective_from_ns mismatch")
            if rule.training_end_ns != self.training_end_ns:
                raise ValueError("rule training_end_ns mismatch")
            if rule.state not in _ALLOWED_STATES:
                raise ValueError(f"unsupported rule state: {rule.state}")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    policy_id: str | None
    rule: SelectiveRule | None
    state: str
    reason: str


class EffectivePolicyStore:
    """Immutable point-in-time policy resolver for prospective shadow decisions."""

    def __init__(self, policies: tuple[SelectivePolicy, ...]) -> None:
        ordered = tuple(sorted(policies, key=lambda p: (p.effective_from_ns, p.policy_id)))
        seen: set[str] = set()
        for policy in ordered:
            if policy.policy_id in seen:
                raise ValueError(f"duplicate policy_id: {policy.policy_id}")
            seen.add(policy.policy_id)
        self._policies = ordered

    @property
    def policies(self) -> tuple[SelectivePolicy, ...]:
        return self._policies

    def policy_at(self, decision_time_ns: int) -> SelectivePolicy | None:
        eligible = [p for p in self._policies if p.effective_from_ns <= decision_time_ns]
        return eligible[-1] if eligible else None

    def decide(
        self,
        *,
        decision_time_ns: int,
        wallet_address: str,
        coin: str,
        direction: str,
        action: str,
        notional_usd: Decimal,
    ) -> PolicyDecision:
        policy = self.policy_at(decision_time_ns)
        if policy is None:
            return PolicyDecision(None, None, "SKIP", "NO_EFFECTIVE_POLICY")

        matches = [
            rule
            for rule in policy.rules
            if rule.matches(
                wallet_address=wallet_address,
                coin=coin,
                direction=direction,
                action=action,
                notional_usd=notional_usd,
            )
        ]
        if not matches:
            return PolicyDecision(policy.policy_id, None, "SKIP", "NO_MATCHING_RULE")

        # Most-specific rule wins. Stable tuple order breaks ties deterministically.
        winner = max(enumerate(matches), key=lambda item: (item[1].specificity, -item[0]))[1]
        return PolicyDecision(policy.policy_id, winner, winner.state, "RULE_MATCH")


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else D(str(value))


def policy_from_dict(payload: dict[str, Any]) -> SelectivePolicy:
    policy_id = str(payload["policy_id"])
    effective_from_ns = int(payload["effective_from_ns"])
    training_end_ns = int(payload["training_end_ns"])
    rules = tuple(
        SelectiveRule(
            policy_id=policy_id,
            effective_from_ns=effective_from_ns,
            training_end_ns=training_end_ns,
            wallet_address=str(raw["wallet_address"]).lower(),
            state=str(raw["state"]).upper(),
            coin=str(raw["coin"]) if raw.get("coin") is not None else None,
            direction=(
                str(raw["direction"]).upper() if raw.get("direction") is not None else None
            ),
            action=str(raw["action"]).upper() if raw.get("action") is not None else None,
            max_notional_usd=_optional_decimal(raw.get("max_notional_usd")),
            reason_codes=tuple(str(x) for x in raw.get("reason_codes") or ()),
        )
        for raw in payload.get("rules") or ()
    )
    return SelectivePolicy(
        policy_id=policy_id,
        generated_at_ns=int(payload["generated_at_ns"]),
        effective_from_ns=effective_from_ns,
        training_end_ns=training_end_ns,
        research_only=bool(payload.get("research_only", True)),
        rules=rules,
    )


def load_policy_store(path: Path) -> EffectivePolicyStore:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_policies = payload.get("policies") if isinstance(payload, dict) else None
    if not isinstance(raw_policies, list):
        raise ValueError("policy store must contain a policies list")
    return EffectivePolicyStore(tuple(policy_from_dict(item) for item in raw_policies))
