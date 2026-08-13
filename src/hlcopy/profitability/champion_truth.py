from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

REQUIRED_TRUTH_LAYERS = (
    "round_trip_fee_accounting",
    "continuous_mtm",
    "funding",
    "maintenance_margin",
    "liquidation_survival",
    "safe_leverage",
)


@dataclass(frozen=True, slots=True)
class ChampionTruth:
    validated: bool
    status: str
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "validated_champion": self.validated,
            "validation_status": self.status,
            "validation_blockers": list(self.blockers),
            "required_truth_layers": list(REQUIRED_TRUTH_LAYERS),
        }


def evaluate_champion_truth(truth: Mapping[str, object]) -> ChampionTruth:
    """Fail closed unless every required profitability/risk truth layer is explicit.

    A truth layer passes only when its value is exactly ``True``. Missing, false,
    string-ish, inferred, proxy, or unknown values remain blocking. This function does
    not decide whether a strategy is profitable; it only decides whether the evidence
    is complete enough for the term ``validated champion`` to be used.
    """
    blockers = tuple(name for name in REQUIRED_TRUTH_LAYERS if truth.get(name) is not True)
    if blockers:
        return ChampionTruth(
            validated=False,
            status="BLOCKED_INCOMPLETE_PROFITABILITY_OR_PATH_RISK_TRUTH",
            blockers=blockers,
        )
    return ChampionTruth(
        validated=True,
        status="VALIDATED_CHAMPION_TRUTH_COMPLETE",
        blockers=(),
    )
